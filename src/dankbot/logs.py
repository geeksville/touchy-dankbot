"""Terminal logging: ``HEARD`` / ``SKIP`` / ``REPLY`` lines, colourised with rich.

Log records still go to **stderr**, and they are rendered with `rich`_ so the
terminal mirrors the LED matrix: a quip is printed in the very colours
:mod:`dankbot.display` scrolls for it, which makes colour debugging possible
without looking at the pad.

Only the *presentation* changes. Every record's message is still the plain
``REPLY: Danger ahead`` / ``SKIP(too_short): uh`` / ``HEARD: …`` text, so
``--json`` consumers, ``grep`` and ``caplog`` are unaffected — and rich drops
the colours by itself when the output is a pipe or ``NO_COLOR`` is set. Each
line also leads with a local :data:`TIME_FORMAT` stamp (``[16:00:35]``), which
is what makes a latency that drifts over a long session visible. The one
addition to the message itself is the pipeline latency a reply can carry, as a
dim suffix that reads as metadata rather than as part of the quip: ``REPLY:
Danger ahead (45s latency)``, or ``… (45s latency: 30s queue + 15s model)`` once
the utterance actually waited in the reply queue (see :func:`format_latency` and
:data:`QUEUE_WAIT_EPS`).

The pieces:

* :func:`configure` — install the rich handler on the root logger (once per
  process; called from ``dankbot.cli.main``), replacing ``logging.basicConfig``;
* :func:`enable_debug` / :func:`ensure_verbosity` — the level changes behind the
  ``--debug`` flag;
* :func:`heard_line` / :func:`skip_line` / :func:`reply_line` — build the
  :class:`rich.text.Text` that a log call emits (``listen.log_emitter``).

See ``docs/plans/ledout.md`` for the colours themselves.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text

from .commands import Run

#: Tag styles for the three listen events. The pad can't say *which* of the three
#: it is, so the tag carries it: cyan = said, yellow = dropped, white = the quip.
HEARD_STYLE = "bold cyan"
SKIP_STYLE = "bold yellow"
REPLY_STYLE = "bold white"
#: Logger name, tag punctuation, and the utterance a ``SKIP`` threw away.
DIM_STYLE = "dim"

#: Root-logger level when nothing else sets one — what ``basicConfig`` used.
DEFAULT_LEVEL = logging.WARNING

_configured = False


def colour_style(colour: int) -> str:
    """``0xRRGGBB`` → rich's ``#rrggbb`` style: the very colour the LEDs show."""
    return f"#{colour & 0xFFFFFF:06x}"


def runs_text(runs: Sequence[Run]) -> Text:
    """A quip's colour runs as one styled :class:`~rich.text.Text`."""
    text = Text()
    for run in runs:
        text.append(run.text, style=colour_style(run.colour))
    return text


def format_latency(seconds: float) -> str:
    """A pipeline duration for the log: ``3.75`` → ``'3.8s'``, ``45.2`` → ``'45s'``.

    One decimal while it is a small number (where the fraction is worth
    reading), whole seconds once it is not — the same shape as the "45s
    latency" you would say out loud.
    """
    return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"


#: A reply-queue wait below this is scheduling jitter, not a backlog: the timing
#: suffix stays the short ``(45s latency)`` form, because with no queue the whole
#: figure is the model round trip anyway (see :func:`_append_latency`).
QUEUE_WAIT_EPS = 0.05


def _append_latency(line: Text, latency_s: float | None, queue_wait_s: float | None = None) -> None:
    """Add the timing suffix — dim, so it reads as metadata.

    ``(45s latency)`` normally. When the utterance actually waited in the reply
    queue the suffix splits the number, ``(45s latency: 30s queue + 15s model)``
    — which is what tells a backed-up pipeline from a slow model. With no real
    queue wait the whole figure *is* the model time, so the short form already
    says it and the breakdown would only add noise (``--fake``, a fast reply).
    """
    if latency_s is None:
        return
    suffix = f" ({format_latency(latency_s)} latency"
    if queue_wait_s is not None and queue_wait_s >= QUEUE_WAIT_EPS:
        # The model time is derived rather than measured here, so clamp it: the
        # two clocks can disagree by a tick on a fast path.
        model_s = max(0.0, latency_s - queue_wait_s)
        suffix += f": {format_latency(queue_wait_s)} queue + {format_latency(model_s)} model"
    line.append(suffix + ")", style=DIM_STYLE)


def heard_line(text: str) -> Text:
    """``HEARD: <text>`` — what the mic (or ``--fake``) picked up."""
    line = Text()
    line.append("HEARD: ", style=HEARD_STYLE)
    line.append(text)
    return line


def skip_line(
    reason: str,
    text: str,
    latency_s: float | None = None,
    queue_wait_s: float | None = None,
) -> Text:
    """``SKIP(<reason>): <text>`` — an utterance that never got a quip.

    *latency_s* is set only for a skip the *model* decided (``/notfunny``): it
    made the same round trip as a reply, so it reports the same timing. A gate
    skip never reached the model and has none. *queue_wait_s* splits that timing
    (see :func:`_append_latency`).
    """
    line = Text()
    line.append("SKIP", style=SKIP_STYLE)
    line.append(f"({reason}): ", style=DIM_STYLE)
    line.append(text, style=DIM_STYLE)
    _append_latency(line, latency_s, queue_wait_s)
    return line


def reply_line(
    runs: Sequence[Run],
    text: str = "",
    latency_s: float | None = None,
    queue_wait_s: float | None = None,
) -> Text:
    """``REPLY: <quip>`` — the quip in the colours the display will scroll.

    *text* is the fallback when there are no runs (the plain, command-free text).
    *latency_s* appends the pipeline latency (utterance heard → quip ready) as a
    dim suffix, so a slow or backed-up model stage is visible in the log;
    *queue_wait_s* splits it into the wait behind the reply worker and the model
    round trip (see :func:`_append_latency`).
    """
    line = Text()
    line.append("REPLY: ", style=REPLY_STYLE)
    line.append_text(runs_text(runs) if runs else Text(text))
    _append_latency(line, latency_s, queue_wait_s)
    return line


def playback_line(
    label: str, heard: str, runs: Sequence[Run] | None = None, text: str = ""
) -> Text:
    """``<label> · <heard> → REPLY: <quip>`` — one ``jokemark --playback`` entry.

    The context is dim so the quip stands out in its display colours. *runs* is
    ``None`` for a reply the model suppressed, which then reads
    ``SKIP(/notfunny)`` instead of a quip.
    """
    line = Text()
    line.append(f"{label} · {heard} → ", style=DIM_STYLE)
    if runs is None:
        line.append("SKIP(/notfunny)", style=SKIP_STYLE)
    else:
        line.append_text(reply_line(runs, text))
    return line


class _Handler(RichHandler):
    """A ``RichHandler`` that lets a :class:`~rich.text.Text` message keep its colour.

    ``RichHandler`` renders ``%(message)s``, which stringifies a ``Text`` back to
    uncoloured text; here a ``Text`` message is used as-is, so a quip's per-run
    colours survive. Such a line also skips the highlighter — model output is
    arbitrary text and must not be re-coloured as if it were a repr.
    """

    def render_message(self, record: logging.LogRecord, message: str) -> Text:
        if isinstance(record.msg, Text):
            # Our own pre-coloured line: no highlighter, no markup pass.
            body = record.msg
        else:
            # Anything else renders exactly as RichHandler would.
            body = super().render_message(record, message)  # type: ignore[assignment]
        line = Text()
        line.append(f"{record.name}: ", style=DIM_STYLE)
        line.append_text(body)
        return line


#: The timestamp each line leads with. Local time, to the second — enough to
#: watch latency drift over a long ``dankbot listen`` session without the noise
#: of a date on every line. Swap for ``"[%x %X]"`` to add the day.
TIME_FORMAT = "[%H:%M:%S]"


def make_handler(*, level: int = logging.NOTSET, console: Console | None = None) -> logging.Handler:
    """The rich handler — stderr by default, so stdout stays pipeable/``--json``.

    *level* defaults to ``NOTSET``: as with ``basicConfig``, it is the *root
    logger's* level (set by :func:`configure`) that decides what is emitted, so a
    ``dankbot`` logger raised to INFO (``dankbot listen``) still gets through.
    Every line is stamped with :data:`TIME_FORMAT`, and the stamp is repeated
    even on consecutive records in the same second (``omit_repeated_times`` off)
    — a blank timestamp on the second of a ``HEARD``/``REPLY`` pair reads like a
    missing field.
    """
    return _Handler(
        level=level,
        console=console or Console(stderr=True),
        show_time=True,
        log_time_format=TIME_FORMAT,
        omit_repeated_times=False,
        show_path=False,
        rich_tracebacks=True,
        # A quip may legitimately contain ``[...]``; never read it as markup.
        markup=False,
    )


def configure(level: int = DEFAULT_LEVEL) -> None:
    """Send all logging through the rich handler (once per process)."""
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    root.addHandler(make_handler())


def enable_debug() -> None:
    """``--debug``: let DEBUG records through, for the root and ``dankbot`` loggers.

    The rich handler sits on the root logger with level ``NOTSET``, so the root
    logger decides what is emitted; ``dankbot`` is pinned too because the
    commands normally raise it to INFO and a logger's own level caps what it
    passes on (a ``dankbot`` logger at INFO would hide its DEBUG records).
    """
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("dankbot").setLevel(logging.DEBUG)


def ensure_verbosity(level: int = logging.INFO) -> None:
    """Make the ``dankbot`` logger at least as verbose as *level*, never quieter.

    ``listen`` / ``jokemark`` call this for INFO because their ``HEARD`` /
    ``SKIP`` / ``REPLY`` records are informational. After ``--debug`` the root
    and ``dankbot`` loggers are already at DEBUG, so this must **not** raise
    them back to INFO — only lower a level (or leave it inheriting from root).
    """
    logger = logging.getLogger("dankbot")
    if logger.getEffectiveLevel() > level:
        logger.setLevel(level)
