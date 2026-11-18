"""The ``voicebot`` command line.

Four commands:

* ``voicebot reply`` — read overheard lines (``--line`` / ``-f FILE`` / stdin)
  and print a witty reply for each (commands stripped; ``/notfunny`` suppresses).
* ``voicebot models`` — list the models the server has pulled.
* ``voicebot jokemark`` — the model bake-off: every model/params in a TOML
  matrix against the packaged overheard corpus, rendered to a Markdown report
  plus a JSON sidecar; ``--playback FILE`` replays that JSON instead of
  talking to the server.
* ``voicebot listen`` — mic → Moonshine → gate → reply, scrolled on the pad.

Config resolution is *flag → env → default*, implemented via click's
``default=`` + ``envvar=`` (see :mod:`voicebot.config`). The chat client is
looked up as the module global ``ChatClient`` so tests can monkeypatch it.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TextIO

import click

from . import __version__, logs
from .audio import AudioIn, FakeTranscriptSource, read_lines
from .config import (
    DEFAULT_CTX,
    DEFAULT_DEBOUNCE_S,
    DEFAULT_DISPLAY_MODE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIN_DURATION_S,
    DEFAULT_MIN_P,
    DEFAULT_MIN_WORDS,
    DEFAULT_MODEL,
    DEFAULT_MODEL_ARCH,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    DEFAULT_URL,
    Settings,
)
from .display import DEFAULT_CLEAR_AFTER_S, DISPLAY_MODES, SCREEN_NAME, PadDisplay
from .gate import UtteranceGate
from .jokemark import (
    CORPUS_ASSET,
    MATRIX_ASSET,
    JokemarkError,
    LineResult,
    ModelResult,
    ModelSpec,
    load_lines,
    load_matrix,
    load_report,
    packaged_text,
    playback,
    render_json,
    render_markdown,
    run_benchmark,
)
from .listen import Listener, json_emitter
from .persona import DEFAULT_PERSONA
from .responder import Reply, Responder
from .server import ChatClient, VoicebotError

log = logging.getLogger(__name__)

#: The LLM/server options shared by ``reply`` and ``listen`` (flag → env →
#: default, via click). Applied by :func:`_llm_options` so the two commands
#: cannot drift apart.
_LLM_OPTIONS = [
    click.option("--url", default=DEFAULT_URL, envvar="VOICEBOT_URL", show_default=True),
    click.option("--model", default=DEFAULT_MODEL, envvar="VOICEBOT_MODEL", show_default=True),
    click.option(
        "--persona",
        default=DEFAULT_PERSONA,
        envvar="VOICEBOT_PERSONA",
        help="System prompt (persona / comedic mechanism).",
    ),
    click.option(
        "--persona-file",
        type=click.File("r", encoding="utf-8"),
        default=None,
        help="Read the persona from FILE (wins over --persona).",
    ),
    click.option(
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        envvar="VOICEBOT_TEMP",
        show_default=True,
    ),
    click.option(
        "--min-p",
        "min_p",
        type=float,
        default=DEFAULT_MIN_P,
        envvar="VOICEBOT_MIN_P",
        show_default=True,
    ),
    click.option("--ctx", type=int, default=DEFAULT_CTX, envvar="VOICEBOT_CTX", show_default=True),
    click.option(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        envvar="VOICEBOT_MAX_TOKENS",
        show_default=True,
    ),
    click.option(
        "--native",
        is_flag=True,
        default=False,
        envvar="VOICEBOT_NATIVE",
        help="Use Ollama's native /api/chat (exact sampler control).",
    ),
    click.option(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        envvar="VOICEBOT_TIMEOUT",
        show_default=True,
    ),
]


def _llm_options(fn):
    """Attach the shared :data:`_LLM_OPTIONS` decorators to *fn*."""
    for decorator in reversed(_LLM_OPTIONS):
        fn = decorator(fn)
    return fn


def _build_responder(
    *,
    url: str,
    model: str,
    persona: str,
    persona_file: TextIO | None,
    temperature: float,
    min_p: float,
    ctx: int,
    max_tokens: int,
    native: bool,
    timeout: float,
) -> Responder:
    """A :class:`Responder` wired to a live :class:`ChatClient`."""
    if persona_file is not None:
        persona = persona_file.read()
    settings = Settings(
        url=url,
        model=model,
        temperature=temperature,
        min_p=min_p,
        ctx=ctx,
        max_tokens=max_tokens,
        native=native,
        timeout=timeout,
    )
    client = ChatClient(settings)
    return Responder(client.chat, model=model, persona=persona)


def _make_display(screen_name: str, clear_after_s: float, display_mode: str) -> PadDisplay:
    """The touchy-pad display, startup banner already shown.

    Displaying is part of ``listen`` — there is no flag to turn it off.
    :class:`~voicebot.display.PadDisplay` only reaches for ``touchy_pad`` when it
    actually draws, and a missing / unplugged pad is a logged warning rather than
    a crash, so the pad may be plugged in at any time.
    """
    display = PadDisplay(
        screen_name=screen_name, clear_after=clear_after_s, display_mode=display_mode
    )
    display.start()  # the "I'm voicebot" banner (connects lazily)
    return display


def _optional_display(display_mode: str) -> PadDisplay | None:
    """A pad for ``jokemark``, or ``None`` when no touchy-pad is attached.

    ``listen`` reaches for the pad on every message so one plugged in later is
    picked up; a bake-off is a batch job whose real output is the report, so this
    probes once at the start. ``clear_after=0`` keeps the last quip up and avoids
    an idle timer on a run that can take an hour.
    """
    display = PadDisplay(screen_name=SCREEN_NAME, clear_after=0, display_mode=display_mode)
    if display.start():
        return display
    display.close()
    return None


def _read_asset_or_file(path: Path | None, asset: str) -> str:
    """Read a jokemark input: the given file, else the packaged asset."""
    return path.read_text(encoding="utf-8") if path is not None else packaged_text(asset)


def _settings_for(spec: ModelSpec, url: str) -> Settings:
    """The :class:`Settings` one matrix entry's bank runs with."""
    return Settings(
        url=url,
        model=spec.model,
        temperature=spec.temperature,
        min_p=spec.min_p,
        ctx=spec.ctx,
        max_tokens=spec.max_tokens,
        native=spec.native,
        timeout=spec.timeout,
    )


@click.group()
@click.version_option(__version__, prog_name="voicebot")
def main() -> None:
    """Witty one-liners about overheard conversation, via a remote LLM."""
    logs.configure()


@main.command()
@click.option("--line", default=None, help="A single overheard line to respond to (one-shot).")
@click.option(
    "-f",
    "--file",
    "file",
    type=click.File("r", encoding="utf-8"),
    default=None,
    help="Read overheard lines from FILE (one per line). Defaults to stdin.",
)
@_llm_options
@click.option(
    "--json", "json_out", is_flag=True, default=False, help="Emit one JSON object per input line."
)
def reply(
    line: str | None,
    file: TextIO | None,
    url: str,
    model: str,
    persona: str,
    persona_file: TextIO | None,
    temperature: float,
    min_p: float,
    ctx: int,
    max_tokens: int,
    native: bool,
    timeout: float,
    json_out: bool,
) -> None:
    """Ask the LLM for a witty reply to each overheard line."""
    responder = _build_responder(
        url=url,
        model=model,
        persona=persona,
        persona_file=persona_file,
        temperature=temperature,
        min_p=min_p,
        ctx=ctx,
        max_tokens=max_tokens,
        native=native,
        timeout=timeout,
    )
    try:
        for source in _input_lines(line, file):
            _emit(responder.respond(source), json_out)
    except VoicebotError as exc:
        click.echo(f"voicebot: {exc}", err=True)
        raise SystemExit(1) from exc


@main.command()
@click.option(
    "--fake",
    "fake",
    type=click.File("r", encoding="utf-8"),
    default=None,
    help="Replay utterances from FILE instead of the mic (no audio, no model).",
)
@click.option(
    "--device",
    default=None,
    envvar="VOICEBOT_MIC",
    help="Capture device (PortAudio index or name substring). Default: auto-detect the mic array.",
)
@click.option(
    "--model-arch",
    "model_arch",
    default=DEFAULT_MODEL_ARCH,
    envvar="VOICEBOT_MODEL_ARCH",
    show_default=True,
    help="STT model size: tiny|base|small|medium (all streaming).",
)
@click.option(
    "--min-words",
    "min_words",
    type=int,
    default=DEFAULT_MIN_WORDS,
    envvar="VOICEBOT_MIN_WORDS",
    show_default=True,
    help="Skip utterances with fewer than N content words (filler stripped).",
)
@click.option(
    "--min-duration",
    "min_duration_s",
    type=float,
    default=DEFAULT_MIN_DURATION_S,
    envvar="VOICEBOT_MIN_DURATION",
    show_default=True,
    help="Skip utterances shorter than N seconds (when the duration is known).",
)
@click.option(
    "--debounce",
    "debounce_s",
    type=float,
    default=DEFAULT_DEBOUNCE_S,
    envvar="VOICEBOT_DEBOUNCE",
    show_default=True,
    help="Suppress an identical repeat of the previous utterance within N seconds.",
)
@click.option(
    "--screen-name",
    "screen_name",
    default=SCREEN_NAME,
    show_default=True,
    help="On-device screen name used for the display (F:host/s/<name>.pb).",
)
@click.option(
    "--clear-after",
    "clear_after_s",
    type=float,
    default=DEFAULT_CLEAR_AFTER_S,
    envvar="VOICEBOT_CLEAR_AFTER",
    show_default=True,
    help="Blank the display after N seconds with no new reply (0 = never).",
)
@click.option(
    "--display-mode",
    "display_mode",
    type=click.Choice(DISPLAY_MODES),
    default=DEFAULT_DISPLAY_MODE,
    envvar="VOICEBOT_DISPLAY_MODE",
    show_default=True,
    help="Panel layout: auto (<= 32 px scrolls, taller wraps), scroll, or multiline.",
)
@_llm_options
@click.option(
    "--json", "json_out", is_flag=True, default=False, help="Emit one JSON event per line."
)
def listen(
    fake: TextIO | None,
    device: str | None,
    model_arch: str,
    min_words: int,
    min_duration_s: float,
    debounce_s: float,
    screen_name: str,
    clear_after_s: float,
    display_mode: str,
    url: str,
    model: str,
    persona: str,
    persona_file: TextIO | None,
    temperature: float,
    min_p: float,
    ctx: int,
    max_tokens: int,
    native: bool,
    timeout: float,
    json_out: bool,
) -> None:
    """Listen, transcribe, log a witty reply per utterance, and show it on the pad."""
    if not json_out:
        logging.getLogger("voicebot").setLevel(logging.INFO)
    responder = _build_responder(
        url=url,
        model=model,
        persona=persona,
        persona_file=persona_file,
        temperature=temperature,
        min_p=min_p,
        ctx=ctx,
        max_tokens=max_tokens,
        native=native,
        timeout=timeout,
    )
    gate = UtteranceGate(min_words=min_words, min_duration_s=min_duration_s, debounce_s=debounce_s)
    source = (
        FakeTranscriptSource(read_lines(fake))
        if fake is not None
        else AudioIn(device=device, model_arch=model_arch)
    )
    display = _make_display(screen_name, clear_after_s, display_mode)
    listener = Listener(
        responder,
        gate,
        emit=json_emitter if json_out else None,
        display=display.show,
        inline=fake is not None,
    )
    try:
        listener.run(source)
    except VoicebotError as exc:
        click.echo(f"voicebot: {exc}", err=True)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        click.echo("voicebot: stopped.", err=True)
    finally:
        display.close()


@main.command()
@click.option("--url", default=DEFAULT_URL, envvar="VOICEBOT_URL", show_default=True)
@click.option("--native", is_flag=True, default=False, envvar="VOICEBOT_NATIVE")
@click.option(
    "--timeout", type=float, default=DEFAULT_TIMEOUT, envvar="VOICEBOT_TIMEOUT", show_default=True
)
def models(url: str, native: bool, timeout: float) -> None:
    """List the models the server has pulled."""
    settings = Settings(url=url, model="", native=native, timeout=timeout)
    try:
        for name in ChatClient(settings).list_models():
            click.echo(name)
    except VoicebotError as exc:
        click.echo(f"voicebot: {exc}", err=True)
        raise SystemExit(1) from exc


@main.command()
@click.option(
    "--models",
    "models_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="TOML model matrix (default: the packaged jokemark-models.toml).",
)
@click.option(
    "-f",
    "--lines",
    "lines_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Overheard corpus (default: the packaged overheard.txt).",
)
@click.option(
    "-o",
    "--out",
    "out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("jokemark.md"),
    show_default=True,
    help="Markdown report to write (a .json sidecar goes next to it).",
)
@click.option(
    "--playback",
    "playback_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Replay a jokemark JSON report instead of talking to the server.",
)
@click.option("--url", default=DEFAULT_URL, envvar="VOICEBOT_URL", show_default=True)
@click.option(
    "--native",
    is_flag=True,
    default=False,
    envvar="VOICEBOT_NATIVE",
    help="Use Ollama's /api/chat instead of /v1 (a matrix entry may override).",
)
@click.option(
    "--timeout",
    type=float,
    default=DEFAULT_TIMEOUT,
    envvar="VOICEBOT_TIMEOUT",
    show_default=True,
    help="Per-request chat timeout in seconds.",
)
@click.option(
    "--pull/--no-pull",
    "pull",
    default=True,
    show_default=True,
    help="Fetch models the server does not have yet.",
)
@click.option("--limit", type=int, default=None, help="Only the first N corpus lines.")
@click.option(
    "--display-mode",
    "display_mode",
    type=click.Choice(DISPLAY_MODES),
    default=DEFAULT_DISPLAY_MODE,
    envvar="VOICEBOT_DISPLAY_MODE",
    show_default=True,
    help="Layout for a touchy-pad that happens to be attached (auto, scroll, multiline).",
)
@click.option(
    "--persona",
    default=DEFAULT_PERSONA,
    envvar="VOICEBOT_PERSONA",
    help="System prompt (persona / comedic mechanism).",
)
@click.option(
    "--persona-file",
    type=click.File("r", encoding="utf-8"),
    default=None,
    help="Read the persona from FILE (wins over --persona).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the resolved matrix and corpus size, then exit.",
)
def jokemark(
    models_file: Path | None,
    lines_file: Path | None,
    out: Path,
    playback_file: Path | None,
    url: str,
    native: bool,
    timeout: float,
    pull: bool,
    limit: int | None,
    display_mode: str,
    persona: str,
    persona_file: TextIO | None,
    dry_run: bool,
) -> None:
    """Bake off the quip stage: every model/params against every overheard line."""
    if playback_file is not None:  # replay a saved run — no server involved
        _replay(playback_file, display_mode)
        return
    if persona_file is not None:
        persona = persona_file.read()
    try:
        specs = load_matrix(
            _read_asset_or_file(models_file, MATRIX_ASSET), native=native, timeout=timeout
        )
        lines = load_lines(_read_asset_or_file(lines_file, CORPUS_ASSET), limit=limit)
    except JokemarkError as exc:
        click.echo(f"voicebot: {exc}", err=True)
        raise SystemExit(1) from exc

    if dry_run:
        click.echo(f"{len(specs)} models × {len(lines)} lines")
        for index, spec in enumerate(specs, 1):
            click.echo(
                f"  {index}. {spec.label} — {spec.model} "
                f"(temp {spec.temperature:g}, min_p {spec.min_p:g}, ctx {spec.ctx}, "
                f"max_tokens {spec.max_tokens}, native {'on' if spec.native else 'off'})"
            )
        return

    json_out = out.with_suffix(".json")
    if json_out == out:
        click.echo("voicebot: --out cannot end in .json — that is the sidecar", err=True)
        raise SystemExit(1)

    logging.getLogger("voicebot").setLevel(logging.INFO)
    display = _optional_display(display_mode)  # None when no pad is attached
    completed: list[ModelResult] = []
    interrupted = False
    try:
        results = run_benchmark(
            specs,
            lines,
            persona=persona,
            client_factory=lambda spec: ChatClient(_settings_for(spec, url)),
            pull=pull,
            on_result=completed.append,
            display=display.show if display is not None else None,
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        interrupted = True
        results = completed
    finally:
        if display is not None:
            display.close()

    markdown = render_markdown(
        results,
        lines,
        url=url,
        persona=persona,
        corpus_name=lines_file.name if lines_file is not None else CORPUS_ASSET,
        native=native,
    )
    payload = render_json(
        results,
        lines,
        url=url,
        persona=persona,
        corpus_name=lines_file.name if lines_file is not None else CORPUS_ASSET,
        native=native,
    )
    json_out = out.with_suffix(".json")
    for path, content in ((out, markdown), (json_out, payload)):
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            click.echo(f"voicebot: cannot write {path}: {exc}", err=True)
            raise SystemExit(1) from exc
        click.echo(f"wrote {path}")
    if interrupted:  # pragma: no cover - interactive stop
        click.echo("voicebot: interrupted — partial report written.", err=True)
        raise SystemExit(130)
    if not any(row.reply is not None for model in results for row in model.results):
        click.echo("voicebot: no model produced a reply.", err=True)
        raise SystemExit(1)


def _replay(path: Path, display_mode: str) -> None:
    """Replay a saved jokemark JSON: log each quip and draw it on an attached pad.

    No server, no model: the JSON already holds everything the run produced, so
    the developer can re-watch a bake-off (or a favourite model's bank) on the
    desk robot. Each drawn quip stays up for one second before the next.
    """
    try:
        report = load_report(path.read_text(encoding="utf-8"))
    except (OSError, JokemarkError) as exc:
        click.echo(f"voicebot: {exc}", err=True)
        raise SystemExit(1) from exc

    logging.getLogger("voicebot").setLevel(logging.INFO)
    display = _optional_display(display_mode)  # None when no pad is attached
    try:
        replayed = playback(
            report,
            emit=_log_playback,
            display=display.show if display is not None else None,
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        click.echo("voicebot: stopped.", err=True)
        return
    finally:
        if display is not None:
            display.close()
    click.echo(f"replayed {replayed} replies from {path} (no model calls)")


def _log_playback(spec: ModelSpec, row: LineResult) -> None:
    """One playback entry to the log: the overheard line, then the quip (or skip)."""
    reply = row.reply
    if reply is None:
        log.warning("%s · %s — %s", spec.label, row.line, row.error or "no reply recorded")
    elif reply.skip:
        log.info(logs.playback_line(spec.label, row.line))
    else:
        log.info(logs.playback_line(spec.label, row.line, reply.runs, reply.text))


def _input_lines(line: str | None, file: TextIO | None) -> list[str]:
    """The overheard lines to answer: ``--line``, else *file*, else stdin.

    Blank lines are dropped; surrounding whitespace is trimmed.
    """
    if line is not None:
        return [line]
    stream = file if file is not None else sys.stdin
    return [stripped for raw in stream if (stripped := raw.strip())]


def _emit(reply: Reply, json_out: bool) -> None:
    """Print a reply — suppressed ones stay silent unless ``--json``."""
    if json_out:
        click.echo(json.dumps(reply.as_dict()))
    elif not reply.skip:
        click.echo(reply.text)


if __name__ == "__main__":  # pragma: no cover
    main()
