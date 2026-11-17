"""jokemark — the model bake-off for the quip stage.

Runs every model/parameter set in a JSON matrix against every line of the
packaged overheard corpus, pulls whatever the server is missing, and renders one
Markdown report with a row per line per model.

Deliberately transport-free in the same way as :mod:`voicebot.responder`: the
benchmark takes a ``client_factory`` (``ModelSpec -> ChatClient``) so tests use a
fake and never pull or chat. Only :mod:`voicebot.cli` wires in the real client.

The corpus and the matrix live in ``voicebot/assets/`` and are read through
:mod:`importlib.resources`, so an installed wheel behaves exactly like a source
checkout. See ``docs/plans/jokemark.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import Protocol

from .config import (
    DEFAULT_CTX,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIN_P,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
)
from .responder import Reply, Responder
from .server import VoicebotError

log = logging.getLogger(__name__)

#: Directory (inside the import package) holding the packaged assets.
ASSETS = "assets"
#: The default model/parameter matrix and overheard corpus.
MATRIX_ASSET = "jokemark-models.json"
CORPUS_ASSET = "overheard.txt"

#: The only matrix schema version this code understands.
MATRIX_VERSION = 1

#: Keys a matrix entry may carry (``defaults`` may carry all but model/label).
_ENTRY_KEYS = frozenset(
    {"model", "label", "temperature", "min_p", "ctx", "max_tokens", "native", "timeout"}
)
_PARAM_KEYS = _ENTRY_KEYS - {"model", "label"}


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


class ChatBackend(Protocol):
    """What ``run_benchmark`` needs from a client (satisfied by ``ChatClient``)."""

    def has_model(self, name: str) -> bool: ...

    def pull_model(self, name: str) -> None: ...

    def chat(self, messages: list[dict[str, str]]) -> str: ...


def packaged_text(name: str) -> str:
    """Read an asset (matrix or corpus) from the installed package."""
    return (resources.files("voicebot") / ASSETS / name).read_text(encoding="utf-8")


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
    """Parse and validate a model matrix.

    *native* and *timeout* are the CLI-level baseline; a matrix ``defaults``
    object or an individual entry may override either.

    Validation is strict on purpose: a typo'd key would otherwise silently run a
    different experiment than the one written down.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JokemarkError(f"matrix is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise JokemarkError("matrix must be a JSON object with version/defaults/models")
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
    log: logging.Logger = log,
) -> list[ModelResult]:
    """Run every spec against every line, one model at a time.

    A missing model is pulled first (unless *pull* is false, in which case the
    bank is recorded as an error). The first :class:`VoicebotError` inside a bank
    aborts that bank only — a down server should not cost the remaining models
    their turn. *on_result* is called as each bank completes, so the CLI can
    still render a partial report after Ctrl-C.
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
        except VoicebotError as exc:
            error = str(exc)
            log.warning("%s: %s", spec.label, error)

        if error is None:
            responder = Responder(client.chat, model=spec.model, persona=persona)
            for index, line in enumerate(lines, 1):
                started = time.monotonic()
                try:
                    reply = responder.respond(line)
                except VoicebotError as exc:
                    failed_after = time.monotonic() - started
                    error = str(exc)
                    line_results.append(
                        LineResult(line=line, reply=None, elapsed_s=failed_after, error=error)
                    )
                    log.warning("%s stopped at line %d: %s", spec.label, index, error)
                    break
                elapsed = time.monotonic() - started
                line_results.append(LineResult(line=line, reply=reply, elapsed_s=elapsed))
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
    digest = hashlib.sha256(persona.encode("utf-8")).hexdigest()[:8]

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
