"""jokemark — the model bake-off for the quip stage.

Runs every model/parameter set in a TOML matrix against every line of the
packaged overheard corpus, pulls whatever the server is missing, and renders one
Markdown report with a row per line per model.

Deliberately transport-free in the same way as :mod:`dankbot.responder`: the
benchmark takes a ``client_factory`` (``ModelSpec -> ChatClient``) so tests use a
fake and never pull or chat. Only :mod:`dankbot.cli` wires in the real client.

A run may also be handed a **display sink** (the CLI passes ``PadDisplay.show``
when a touchy-pad happens to be attached): every reply that is not a
``/notfunny`` skip is then drawn as it arrives, using the same parsed colour
runs ``dank-bot listen`` shows. The device is entirely optional — no sink means
"report only", and a sink that fails must never cost the bake-off its run.

Every run is written twice: a Markdown report for a human
(:func:`render_markdown`) and a JSON sidecar with the same data, replies kept
untruncated (:func:`render_json`). That JSON is also the input to
:func:`playback` — replays a recorded run to the log and to an optional pad, a
second per quip so it can be read, without asking any model anything.

The corpus and the matrix live in ``dankbot/assets/`` and are read through
:mod:`importlib.resources`, so an installed wheel behaves exactly like a source
checkout. The matrix is TOML (stdlib :mod:`tomllib`, no dependency) rather than
JSON so it can carry comments explaining each experiment. See
``docs/plans/jokemark.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import Protocol

from .commands import Run
from .config import (
    DEFAULT_CTX,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIN_P,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
)
from .display import DisplaySink
from .responder import Reply, Responder
from .server import DankbotError

log = logging.getLogger(__name__)

#: Directory (inside the import package) holding the packaged assets.
ASSETS = "assets"
#: The default model/parameter matrix and overheard corpus.
MATRIX_ASSET = "jokemark-models.toml"
CORPUS_ASSET = "overheard.txt"

#: The only matrix schema version this code understands.
MATRIX_VERSION = 1

#: The only JSON report schema version this code understands (``jokemark.json``).
REPORT_VERSION = 1

#: How long ``--playback`` leaves a quip up on the pad before the next one.
PLAYBACK_PAUSE_S = 5.0

#: Keys a matrix entry may carry (``defaults`` may carry all but model/label).
_ENTRY_KEYS = frozenset(
    {"model", "label", "temperature", "min_p", "ctx", "max_tokens", "native", "timeout"}
)
_PARAM_KEYS = _ENTRY_KEYS - {"model", "label"}

#: Report-only keys a JSON model entry carries besides label/model/params.
_REPORT_MODEL_KEYS = frozenset({"pulled", "error", "results"})


class JokemarkError(ValueError):
    """A malformed model matrix or overheard corpus."""


@dataclass(frozen=True)
class ModelSpec:
    """One row of the matrix: a model plus the sampler it is run with."""

    label: str
    model: str
    temperature: float = DEFAULT_TEMPERATURE
    min_p: float = DEFAULT_MIN_P
    ctx: int = DEFAULT_CTX
    max_tokens: int = DEFAULT_MAX_TOKENS
    native: bool = False
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class LineResult:
    """One cell of the report: the reply (or the error) for one corpus line."""

    line: str
    reply: Reply | None
    elapsed_s: float
    error: str | None = None


@dataclass(frozen=True)
class ModelResult:
    """One matrix entry's whole bank of lines."""

    spec: ModelSpec
    results: tuple[LineResult, ...]
    error: str | None = None
    pulled: bool = False


@dataclass(frozen=True)
class Report:
    """A parsed ``jokemark.json`` — everything ``--playback`` needs to replay."""

    models: tuple[ModelResult, ...]
    lines: tuple[str, ...]
    url: str = ""
    corpus_name: str = ""
    persona: str = ""
    native: bool = False
    generated: str = ""


class ChatBackend(Protocol):
    """What ``run_benchmark`` needs from a client (satisfied by ``ChatClient``)."""

    def has_model(self, name: str) -> bool: ...

    def pull_model(self, name: str) -> None: ...

    def chat(self, messages: list[dict[str, str]]) -> str: ...


def packaged_text(name: str) -> str:
    """Read an asset (matrix or corpus) from the installed package."""
    return (resources.files("dankbot") / ASSETS / name).read_text(encoding="utf-8")


def load_lines(text: str, *, limit: int | None = None) -> tuple[str, ...]:
    """The corpus lines, trimmed, blanks dropped, duplicates rejected.

    *limit* keeps only the first N lines (a quick smoke run). Duplicates are an
    error because the report keys a row by its line.
    """
    if limit is not None and limit <= 0:
        raise JokemarkError("--limit must be a positive integer")
    lines: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in seen:
            raise JokemarkError(f"duplicate overheard line: {line!r}")
        seen.add(line)
        lines.append(line)
        if limit is not None and len(lines) >= limit:
            break
    if not lines:
        raise JokemarkError("the overheard corpus is empty")
    return tuple(lines)


def _derive_label(model: str) -> str:
    """A report label from a model path: leaf name, no tag, no ``-gguf`` suffix."""
    leaf = model.rsplit("/", 1)[-1].split(":", 1)[0].lower().strip()
    leaf = leaf.removesuffix("-gguf")
    return leaf or model.lower()


def _checked_params(
    raw: object, *, where: str, skip: frozenset[str] = frozenset()
) -> dict[str, object]:
    """Validate the sampler keys of a ``defaults`` object or one model entry."""
    if not isinstance(raw, dict):
        raise JokemarkError(f"{where}: expected an object")
    unknown = set(raw) - _PARAM_KEYS - set(skip)
    if unknown:
        raise JokemarkError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")

    out: dict[str, object] = {}
    for key in ("temperature", "min_p", "timeout"):
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise JokemarkError(f"{where}: {key!r} must be a number")
        number = float(value)
        if key == "temperature" and number < 0:
            raise JokemarkError(f"{where}: 'temperature' must not be negative")
        if key == "min_p" and not 0.0 <= number <= 1.0:
            raise JokemarkError(f"{where}: 'min_p' must be between 0 and 1")
        if key == "timeout" and number <= 0:
            raise JokemarkError(f"{where}: 'timeout' must be positive")
        out[key] = number
    for key in ("ctx", "max_tokens"):
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise JokemarkError(f"{where}: {key!r} must be an integer")
        if value <= 0:
            raise JokemarkError(f"{where}: {key!r} must be positive")
        out[key] = value
    if "native" in raw:
        if not isinstance(raw["native"], bool):
            raise JokemarkError(f"{where}: 'native' must be true or false")
        out["native"] = raw["native"]
    return out


def load_matrix(
    text: str, *, native: bool = False, timeout: float = DEFAULT_TIMEOUT
) -> tuple[ModelSpec, ...]:
    """Parse and validate a model matrix (TOML: ``version`` / ``defaults`` / ``models``).

    *native* and *timeout* are the CLI-level baseline; a matrix ``defaults``
    object or an individual entry may override either.

    Validation is strict on purpose: a typo'd key would otherwise silently run a
    different experiment than the one written down.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise JokemarkError(f"matrix is not valid TOML: {exc}") from exc
    unknown = set(data) - {"version", "defaults", "models"}
    if unknown:
        raise JokemarkError(f"unknown matrix keys: {', '.join(sorted(unknown))}")
    version = data.get("version")
    if version != MATRIX_VERSION:
        raise JokemarkError(f"unsupported matrix version {version!r} (expected {MATRIX_VERSION})")

    merged: dict[str, object] = {
        "temperature": DEFAULT_TEMPERATURE,
        "min_p": DEFAULT_MIN_P,
        "ctx": DEFAULT_CTX,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "native": native,
        "timeout": timeout,
    }
    merged.update(_checked_params(data.get("defaults", {}), where="defaults"))

    entries = data.get("models")
    if not isinstance(entries, list) or not entries:
        raise JokemarkError("matrix must have a non-empty 'models' list")

    specs: list[ModelSpec] = []
    labels: set[str] = set()
    for index, entry in enumerate(entries, 1):
        where = f"model #{index}"
        if not isinstance(entry, dict):
            raise JokemarkError(f"{where}: expected an object")
        unknown_entry = set(entry) - _ENTRY_KEYS
        if unknown_entry:
            raise JokemarkError(f"{where}: unknown keys: {', '.join(sorted(unknown_entry))}")
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip():
            raise JokemarkError(f"{where}: 'model' must be a non-empty string")
        label = entry.get("label")
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise JokemarkError(f"{where}: 'label' must be a non-empty string")
        resolved_label = label.strip() if label is not None else _derive_label(model)
        if resolved_label in labels:
            raise JokemarkError(f"{where}: duplicate label {resolved_label!r}")
        labels.add(resolved_label)

        params = dict(merged)
        params.update(_checked_params(entry, where=where, skip=frozenset({"model", "label"})))
        specs.append(ModelSpec(label=resolved_label, model=model.strip(), **params))
    return tuple(specs)


def run_benchmark(
    specs: Sequence[ModelSpec],
    lines: Sequence[str],
    *,
    persona: str,
    client_factory: Callable[[ModelSpec], ChatBackend],
    pull: bool,
    on_result: Callable[[ModelResult], None] | None = None,
    display: DisplaySink | None = None,
    log: logging.Logger = log,
) -> list[ModelResult]:
    """Run every spec against every line, one model at a time.

    A missing model is pulled first (unless *pull* is false, in which case the
    bank is recorded as an error). The first :class:`DankbotError` inside a bank
    aborts that bank only — a down server should not cost the remaining models
    their turn. *on_result* is called as each bank completes, so the CLI can
    still render a partial report after Ctrl-C.

    *display*, when given, receives each non-skip reply's colour runs as the
    reply arrives (the same ``Reply.runs`` ``dank-bot listen`` shows); it is
    optional and a failing sink is logged, never fatal.
    """
    results: list[ModelResult] = []
    for spec in specs:
        client = client_factory(spec)
        pulled = False
        error: str | None = None
        line_results: list[LineResult] = []

        try:
            if not client.has_model(spec.model):
                if not pull:
                    error = "not pulled (--no-pull)"
                else:
                    log.info("pulling %s (this can take a while)", spec.model)
                    client.pull_model(spec.model)
                    pulled = True
        except DankbotError as exc:
            error = str(exc)
            log.warning("%s: %s", spec.label, error)

        if error is None:
            responder = Responder(client.chat, model=spec.model, persona=persona)
            for index, line in enumerate(lines, 1):
                started = time.monotonic()
                try:
                    reply = responder.respond(line)
                except DankbotError as exc:
                    failed_after = time.monotonic() - started
                    error = str(exc)
                    line_results.append(
                        LineResult(line=line, reply=None, elapsed_s=failed_after, error=error)
                    )
                    log.warning("%s stopped at line %d: %s", spec.label, index, error)
                    break
                elapsed = time.monotonic() - started
                line_results.append(LineResult(line=line, reply=reply, elapsed_s=elapsed))
                if display is not None and not reply.skip:
                    _show(display, reply)
                log.info("%s %d/%d", spec.label, index, len(lines))

        result = ModelResult(
            spec=spec,
            results=tuple(line_results),
            error=error,
            pulled=pulled,
        )
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def _show(display: DisplaySink, reply: Reply) -> bool:
    """Draw one quip on the optional display — a broken pad is not fatal.

    ``False`` means nothing was drawn: no pad is attached, or the one that was
    has gone away. Playback uses that to skip the reading pause instead of
    waiting on a screen nobody can see.
    """
    try:
        drawn = display(reply.runs)
    except Exception:  # a bad display must not stop the bake-off
        log.exception("display update failed")
        return False
    return bool(drawn) if drawn is not None else True


def _cell(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def _code_cell(text: str) -> str:
    """Escape for a backtick-wrapped table cell (raw model output)."""
    return _cell(text.replace("`", "\\`"))


def _shorten(text: str, limit: int = 300) -> str:
    """Length-capped text with an ellipsis when it was cut (newlines kept)."""
    stripped = text.strip()
    return stripped if len(stripped) <= limit else stripped[: limit - 1] + "…"


def _snippet(text: str, limit: int = 64) -> str:
    """The first line of *text*, capped — for the report header."""
    first = text.splitlines()[0] if text.strip() else ""
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _persona_digest(persona: str) -> str:
    """A short stable fingerprint of the persona, for both report headers."""
    return hashlib.sha256(persona.encode("utf-8")).hexdigest()[:8]


def _duration(seconds: Sequence[float], pick: Callable[[Sequence[float]], float]) -> str:
    return f"{pick(seconds):.1f} s" if seconds else "—"


def _summary_row(index: int, result: ModelResult, line_count: int) -> str:
    spec = result.spec
    replies = sum(1 for row in result.results if row.reply is not None and not row.reply.skip)
    skips = sum(1 for row in result.results if row.reply is not None and row.reply.skip)
    errors = line_count - sum(1 for row in result.results if row.error is None)
    times = [row.elapsed_s for row in result.results]
    median = _duration(times, statistics.median)
    slowest = _duration(times, max)
    return (
        f"| {index} | {_cell(spec.label)} | {_cell(spec.model)} "
        f"| {spec.temperature:g} | {spec.min_p:g} | {spec.ctx} | {spec.max_tokens} "
        f"| {replies} | {skips} | {errors} | {median} | {slowest} |"
    )


def _detail_row(index: int, line: str, result: LineResult | None) -> str:
    if result is None:
        return f"| {index} | {_cell(line)} | — | — | — |"
    if result.error is not None:
        raw = f"`{_code_cell(_shorten(result.error))}`"
        return f"| {index} | {_cell(line)} | — *(error)* | {raw} | {result.elapsed_s:.1f} |"
    reply = result.reply
    if reply is None:  # defensive: a result without a reply or an error
        return f"| {index} | {_cell(line)} | — | — | {result.elapsed_s:.1f} |"
    raw_text = reply.raw or reply.text
    raw = f"`{_code_cell(_shorten(raw_text))}`" if raw_text else "—"
    if reply.skip:
        quip = "— *(/notfunny)*"
    else:
        quip = _cell(reply.text) or "—"
    return f"| {index} | {_cell(line)} | {quip} | {raw} | {result.elapsed_s:.1f} |"


def render_markdown(
    results: Sequence[ModelResult],
    lines: Sequence[str],
    *,
    url: str,
    persona: str,
    corpus_name: str,
    native: bool,
    now: datetime | None = None,
) -> str:
    """Render the whole run as one Markdown report (summary + a section each)."""
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    digest = _persona_digest(persona)

    out: list[str] = [
        f"# jokemark — {stamp}",
        "",
        f"Server `{url}` · corpus `{corpus_name}` ({len(lines)} lines)",
        f"· persona `{_snippet(persona)}` (sha256:{digest})",
        f"· native: {'yes' if native else 'no'}",
        "",
        "## Summary",
        "",
        (
            "| # | label | model | temp | min-p | ctx | max | replies | skips | errors "
            "| median | slowest |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for index, result in enumerate(results, 1):
        out.append(_summary_row(index, result, len(lines)))
    out.append("")

    for index, result in enumerate(results, 1):
        spec = result.spec
        settings = (
            f"temperature {spec.temperature:g}, min_p {spec.min_p:g}, "
            f"ctx {spec.ctx}, max_tokens {spec.max_tokens}, "
            f"native {'on' if spec.native else 'off'}"
        )
        pulled = " · pulled just now" if result.pulled else ""
        out += [f"## {index}. {_cell(spec.label)}", ""]
        out += [f"`{spec.model}` — {settings}{pulled}", ""]
        if result.error is not None:
            out += [f"**Error:** {_cell(result.error)}", ""]
        out += ["| # | overheard | quip | raw | s |", "|---|---|---|---|---|"]
        for row_index, line in enumerate(lines):
            row = result.results[row_index] if row_index < len(result.results) else None
            out.append(_detail_row(row_index + 1, line, row))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_json(
    results: Sequence[ModelResult],
    lines: Sequence[str],
    *,
    url: str,
    persona: str,
    corpus_name: str,
    native: bool,
    now: datetime | None = None,
) -> str:
    """Render the whole run as JSON — the same data as Markdown, for machines.

    The shape mirrors the report: a header (server, corpus, persona, native),
    then one object per matrix entry with one object per line. Unlike the
    Markdown tables nothing is truncated: each reply keeps its full raw model
    output and its colour runs, so :func:`load_report` can replay it exactly.
    """
    stamp = now or datetime.now(UTC)
    payload = {
        "version": REPORT_VERSION,
        "generated": stamp.isoformat(timespec="seconds"),
        "url": url,
        "native": native,
        "persona": {
            "snippet": _snippet(persona),
            "sha256": _persona_digest(persona),
            "text": persona,
        },
        "corpus": {"name": corpus_name, "lines": list(lines)},
        "models": [_model_dict(result) for result in results],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _model_dict(result: ModelResult) -> dict[str, object]:
    """One matrix entry as a JSON object (its sampler included, for context)."""
    spec = result.spec
    return {
        "label": spec.label,
        "model": spec.model,
        "temperature": spec.temperature,
        "min_p": spec.min_p,
        "ctx": spec.ctx,
        "max_tokens": spec.max_tokens,
        "native": spec.native,
        "timeout": spec.timeout,
        "pulled": result.pulled,
        "error": result.error,
        "results": [_row_dict(row) for row in result.results],
    }


def _row_dict(row: LineResult) -> dict[str, object]:
    """One line's cell as a JSON object — untruncated, skip and runs included."""
    reply = row.reply
    return {
        "line": row.line,
        "reply": None if reply is None else reply.text,
        "skip": False if reply is None else reply.skip,
        "raw": None if reply is None else reply.raw,
        "runs": (
            []
            if reply is None
            else [{"text": run.text, "colour": run.colour} for run in reply.runs]
        ),
        "elapsed_s": round(row.elapsed_s, 3),
        "error": row.error,
    }


def load_report(text: str) -> Report:
    """Parse and validate a JSON report written by :func:`render_json`.

    Strict about the fields playback depends on (version, model entries, lines,
    reply/colour-run types) so a hand-edited or truncated file fails loudly
    instead of drawing half a quip. Unknown keys *inside* a model entry are
    rejected too (a typo'd sampler is a different run); extra top-level keys are
    ignored.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JokemarkError(f"report is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise JokemarkError("report: expected a JSON object")
    version = data.get("version")
    if version != REPORT_VERSION:
        raise JokemarkError(f"unsupported report version {version!r} (expected {REPORT_VERSION})")

    entries = data.get("models")
    if not isinstance(entries, list) or not entries:
        raise JokemarkError("report must have a non-empty 'models' list")
    models = tuple(_report_model(entry, index) for index, entry in enumerate(entries, 1))

    corpus = data.get("corpus")
    if not isinstance(corpus, dict):
        raise JokemarkError("report: 'corpus' must be an object")
    raw_lines = corpus.get("lines")
    if not isinstance(raw_lines, list) or not all(isinstance(line, str) for line in raw_lines):
        raise JokemarkError("report: 'corpus.lines' must be a list of strings")
    corpus_name = corpus.get("name", "")
    if not isinstance(corpus_name, str):
        raise JokemarkError("report: 'corpus.name' must be a string")

    return Report(
        models=models,
        lines=tuple(raw_lines),
        url=_report_text(data, "url"),
        corpus_name=corpus_name,
        persona=_report_persona(data.get("persona", "")),
        native=_report_flag(data, "native"),
        generated=_report_text(data, "generated"),
    )


def _report_text(data: dict, key: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise JokemarkError(f"report: {key!r} must be a string")
    return value


def _report_flag(data: dict, key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise JokemarkError(f"report: {key!r} must be true or false")
    return value


def _report_persona(raw: object) -> str:
    """The persona text: an object (our format), or a bare string (tolerant)."""
    if isinstance(raw, dict):
        value = raw.get("text") or raw.get("snippet") or ""
    elif isinstance(raw, str):
        value = raw
    else:
        raise JokemarkError("report: 'persona' must be an object")
    if not isinstance(value, str):
        raise JokemarkError("report: 'persona.text' must be a string")
    return value


def _report_model(raw: object, index: int) -> ModelResult:
    where = f"model #{index}"
    if not isinstance(raw, dict):
        raise JokemarkError(f"{where}: expected an object")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise JokemarkError(f"{where}: 'model' must be a non-empty string")
    label = raw.get("label")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise JokemarkError(f"{where}: 'label' must be a non-empty string")
    skip = frozenset({"model", "label"}) | _REPORT_MODEL_KEYS
    params = _checked_params(raw, where=where, skip=skip)
    pulled = raw.get("pulled", False)
    if not isinstance(pulled, bool):
        raise JokemarkError(f"{where}: 'pulled' must be true or false")
    error = raw.get("error")
    if error is not None and not isinstance(error, str):
        raise JokemarkError(f"{where}: 'error' must be a string or null")
    rows = raw.get("results", [])
    if not isinstance(rows, list):
        raise JokemarkError(f"{where}: 'results' must be a list")
    return ModelResult(
        spec=ModelSpec(
            label=label.strip() if label is not None else _derive_label(model),
            model=model.strip(),
            **params,  # type: ignore[arg-type]
        ),
        results=tuple(
            _report_row(row, model.strip(), f"{where} result #{row_index}")
            for row_index, row in enumerate(rows, 1)
        ),
        error=error,
        pulled=pulled,
    )


def _report_row(raw: object, model: str, where: str) -> LineResult:
    if not isinstance(raw, dict):
        raise JokemarkError(f"{where}: expected an object")
    line = raw.get("line")
    if not isinstance(line, str):
        raise JokemarkError(f"{where}: 'line' must be a string")
    elapsed = raw.get("elapsed_s", 0.0)
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        raise JokemarkError(f"{where}: 'elapsed_s' must be a number")
    error = raw.get("error")
    if error is not None and not isinstance(error, str):
        raise JokemarkError(f"{where}: 'error' must be a string or null")

    text = raw.get("reply")
    if text is None:  # a line that was attempted and failed
        return LineResult(line=line, reply=None, elapsed_s=float(elapsed), error=error)
    if not isinstance(text, str):
        raise JokemarkError(f"{where}: 'reply' must be a string or null")
    skip = raw.get("skip", False)
    if not isinstance(skip, bool):
        raise JokemarkError(f"{where}: 'skip' must be true or false")
    unparsed = raw.get("raw")
    if unparsed is None:
        unparsed = text
    if not isinstance(unparsed, str):
        raise JokemarkError(f"{where}: 'raw' must be a string or null")
    reply = Reply(
        source=line,
        text=text,
        skip=skip,
        model=model,
        runs=_report_runs(raw.get("runs"), text, where),
        raw=unparsed,
    )
    return LineResult(line=line, reply=reply, elapsed_s=float(elapsed), error=error)


def _report_runs(raw: object, text: str, where: str) -> tuple[Run, ...]:
    """The stored colour runs; a plain reply without them falls back to white."""
    if raw is None:
        return (Run(text),) if text else ()
    if not isinstance(raw, list):
        raise JokemarkError(f"{where}: 'runs' must be a list")
    runs: list[Run] = []
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise JokemarkError(f"{where}: run #{index} must be an object")
        run_text = entry.get("text")
        if not isinstance(run_text, str):
            raise JokemarkError(f"{where}: run #{index}: 'text' must be a string")
        if "colour" not in entry:
            runs.append(Run(run_text))
            continue
        colour = entry["colour"]
        if isinstance(colour, bool) or not isinstance(colour, int):
            raise JokemarkError(f"{where}: run #{index}: 'colour' must be an integer")
        runs.append(Run(run_text, colour))
    if runs:
        return tuple(runs)
    return (Run(text),) if text else ()


def playback(
    report: Report,
    *,
    emit: Callable[[ModelSpec, LineResult], None] | None = None,
    display: DisplaySink | None = None,
    pause_s: float = PLAYBACK_PAUSE_S,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Replay a recorded run — no server, no model, just what was saved.

    Every entry (reply, skip or error) goes to *emit* so the CLI can log it.
    Non-skip replies are also drawn on *display* when one is attached, followed
    by ``pause_s`` seconds of quiet so the quip can be read on the pad; nothing
    is paused when no sink actually drew (no pad, or a pad that just went away),
    because the log needs no reading time. Returns the number of replies replayed.
    """
    nap = time.sleep if sleep is None else sleep
    replayed = 0
    for model in report.models:
        for row in model.results:
            if emit is not None:
                emit(model.spec, row)
            reply = row.reply
            if reply is None or reply.skip:
                continue
            replayed += 1
            if display is not None and _show(display, reply) and pause_s > 0:
                nap(pause_s)
    return replayed
