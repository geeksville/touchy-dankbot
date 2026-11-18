"""Terminal logging: rich colours whose plain text stays exactly as before."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
from rich.console import Console
from rich.logging import RichHandler

from dankbot import logs
from dankbot.commands import COLOURS, WHITE, Run
from dankbot.listen import KIND_REPLY, ListenEvent, log_emitter

TEST_LOGGER = "dankbot.test_logs"


@pytest.fixture
def console() -> Console:
    """A recording console that pretends to be a truecolor terminal."""
    return Console(record=True, force_terminal=True, color_system="truecolor", width=200)


def _emit(console: Console, log_it: Callable[[logging.Logger], None]) -> tuple[str, str]:
    """Run *log_it* through the rich handler → ``(plain text, ANSI)``."""
    logger = logging.getLogger(TEST_LOGGER)
    handler = logs.make_handler(level=logging.INFO, console=console)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        log_it(logger)
    finally:
        logger.removeHandler(handler)
    ansi = console.export_text(styles=True, clear=False)
    return console.export_text(styles=False), ansi


def test_colour_style_is_the_display_colour():
    assert logs.colour_style(COLOURS["red"]) == "#ff0000"
    assert logs.colour_style(0x00FFAA) == "#00ffaa"
    assert logs.colour_style(WHITE) == "#ffffff"


def test_runs_text_keeps_run_order_and_colours():
    text = logs.runs_text((Run("Danger", COLOURS["red"]), Run(" ahead", COLOURS["blue"])))
    assert str(text) == "Danger ahead"
    assert [(s.start, s.end, s.style) for s in text.spans] == [
        (0, 6, "#ff0000"),
        (6, 12, "#0000ff"),
    ]


def test_the_three_lines_keep_their_plain_text():
    assert str(logs.heard_line("the bread is done baking")) == "HEARD: the bread is done baking"
    assert str(logs.skip_line("too_short", "uh")) == "SKIP(too_short): uh"
    assert str(logs.reply_line((Run("Danger!", COLOURS["red"]),))) == "REPLY: Danger!"
    # No runs (a plain reply) still renders the fallback text.
    assert str(logs.reply_line((), "no runs")) == "REPLY: no runs"


def test_format_latency_reads_in_seconds():
    assert logs.format_latency(0.42) == "0.4s"
    assert logs.format_latency(3.75) == "3.8s"
    assert logs.format_latency(9.94) == "9.9s"
    assert logs.format_latency(10.0) == "10s"
    assert logs.format_latency(45.2) == "45s"
    assert logs.format_latency(103.6) == "104s"


def test_a_reply_line_carries_the_pipeline_latency_as_dim_metadata():
    line = logs.reply_line((Run("Danger", COLOURS["red"]),), latency_s=45.2)
    assert str(line) == "REPLY: Danger (45s latency)"
    assert line.spans[-1].style == logs.DIM_STYLE  # metadata, not part of the quip
    assert (line.spans[-1].start, line.spans[-1].end) == (13, len(str(line)))


def test_a_skip_line_carries_a_latency_only_when_the_model_ran():
    assert str(logs.skip_line("too_short", "uh", latency_s=0.3)) == (
        "SKIP(too_short): uh (0.3s latency)"
    )
    assert str(logs.skip_line("notfunny", "meh", latency_s=12.0)) == (
        "SKIP(notfunny): meh (12s latency)"
    )


def test_the_playback_line_keeps_the_context_and_the_quip():
    line = logs.playback_line("one", "the light is red", (Run("Danger", COLOURS["red"]),))
    assert str(line) == "one · the light is red → REPLY: Danger"
    assert line.spans[0].style == logs.DIM_STYLE
    assert any(span.style == "#ff0000" for span in line.spans)
    # A suppressed reply has no runs and says so instead of a quip.
    assert str(logs.playback_line("one", "meh")) == "one · meh → SKIP(/notfunny)"


def test_the_handler_prints_the_quip_in_its_display_colours(console):
    runs = (Run("Danger", COLOURS["red"]), Run(" ahead", COLOURS["blue"]))
    plain, ansi = _emit(console, lambda logger: logger.info(logs.reply_line(runs)))
    assert f"{TEST_LOGGER}: REPLY: Danger ahead" in plain
    assert "38;2;255;0;0" in ansi  # red, exactly 0xFF0000
    assert "38;2;0;0;255" in ansi  # blue, exactly 0x0000FF


def test_plain_records_are_rendered_with_the_logger_name(console):
    plain, _ = _emit(console, lambda logger: logger.warning("no touchy-pad here (%s)", "boom"))
    assert "WARNING" in plain
    assert f"{TEST_LOGGER}: no touchy-pad here (boom)" in plain


def test_square_brackets_in_a_quip_stay_literal(console):
    plain, _ = _emit(console, lambda logger: logger.info(logs.reply_line((Run("[red]hi", WHITE),))))
    assert "[red]hi" in plain


def test_the_listen_emitter_logs_the_quip_as_colour_runs(caplog):
    event = ListenEvent(
        KIND_REPLY,
        text="the light is red",
        reply="Danger ahead",
        model="m",
        runs=(Run("Danger", COLOURS["red"]), Run(" ahead", WHITE)),
    )
    with caplog.at_level(logging.INFO, logger="dankbot.listen"):
        log_emitter(event)
    record = caplog.records[-1]
    assert record.getMessage() == "REPLY: Danger ahead"
    assert [(s.start, s.end, s.style) for s in record.msg.spans] == [
        (0, 7, logs.REPLY_STYLE),
        (7, 13, "#ff0000"),
        (13, 19, "#ffffff"),
    ]


def test_the_listen_emitter_reports_a_reply_latency(caplog):
    event = ListenEvent(
        KIND_REPLY,
        text="the light is red",
        reply="Danger ahead",
        model="m",
        runs=(Run("Danger", COLOURS["red"]), Run(" ahead", WHITE)),
        latency_s=45.2,
    )
    with caplog.at_level(logging.INFO, logger="dankbot.listen"):
        log_emitter(event)
    assert caplog.records[-1].getMessage() == "REPLY: Danger ahead (45s latency)"


def test_configure_installs_one_handler(monkeypatch):
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    monkeypatch.setattr(logs, "_configured", False)
    try:
        logs.configure()
        first = list(root.handlers)
        logs.configure()  # second call must be a no-op
        assert root.handlers == first
        assert len(first) == len(saved_handlers) + 1
        assert isinstance(first[-1], RichHandler)
        assert first[-1].level == logging.NOTSET  # the root logger's level filters
        assert root.level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_enable_debug_lowers_the_root_and_dankbot_loggers():
    root = logging.getLogger()
    dankbot = logging.getLogger("dankbot")
    saved_levels = root.level, dankbot.level
    try:
        logs.enable_debug()
        assert root.level == logging.DEBUG
        assert dankbot.getEffectiveLevel() == logging.DEBUG
    finally:
        root.setLevel(saved_levels[0])
        dankbot.setLevel(saved_levels[1])


def test_enable_debug_lets_debug_records_through(caplog):
    root = logging.getLogger()
    dankbot = logging.getLogger("dankbot")
    saved_levels = root.level, dankbot.level
    try:
        logs.enable_debug()
        dankbot.debug("deep detail")
    finally:
        root.setLevel(saved_levels[0])
        dankbot.setLevel(saved_levels[1])
    assert caplog.records[-1].levelno == logging.DEBUG
    assert "deep detail" in caplog.text


def test_ensure_verbosity_lifts_info_but_never_hides_debug():
    root = logging.getLogger()
    dankbot = logging.getLogger("dankbot")
    saved_levels = root.level, dankbot.level
    try:
        root.setLevel(logging.WARNING)
        dankbot.setLevel(logging.NOTSET)
        logs.ensure_verbosity(logging.INFO)  # listen / jokemark without --debug
        assert dankbot.getEffectiveLevel() == logging.INFO

        logs.enable_debug()
        logs.ensure_verbosity(logging.INFO)  # ... and the same after --debug
        assert dankbot.getEffectiveLevel() == logging.DEBUG
    finally:
        root.setLevel(saved_levels[0])
        dankbot.setLevel(saved_levels[1])
