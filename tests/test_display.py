"""The LED-out display: colour markup, the scrolling screen, lazy connect, idle clear.

Driven through a duck-typed fake pad, so no hardware is needed; the screen-shape
assertions additionally need the host library (``touchy_pad``), which the path
dependency installs. See ``docs/plans/ledout.md``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from voicebot.commands import COLOURS, WHITE, Run
from voicebot.display import (
    DEFAULT_SCROLL_SPEED_PX_S,
    FONT_SIZES,
    LABEL_ID,
    MAX_OVERRIDE_CHARS,
    MODE_AUTO,
    MODE_MULTILINE,
    MODE_SCROLL,
    STARTUP_TEXT,
    PadDisplay,
    font_for,
    font_for_panel,
    panel_inset,
    resolve_mode,
    to_markup,
)


class FakePad:
    """A duck-typed stand-in for a ``touchy_pad.api.Touchy``."""

    def __init__(self, width: int = 32, height: int = 8, *, failures: int = 0) -> None:
        self.board_info = SimpleNamespace(display_width=width, display_height=height)
        self.saved: list[object] = []
        self.loaded: list[str] = []
        self.batches: list[list[object]] = []
        self.closed = False
        self._failures = failures

    def screen_save(self, screen):
        self.saved.append(screen)

    def screen_load(self, path):
        self.loaded.append(path)

    def set_properties(self, entries):
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("usb gone")
        self.batches.append(list(entries))

    def close(self):
        self.closed = True

    def texts(self) -> list[str]:
        return [entry.string_value for batch in self.batches for entry in batch]


def saved_label(pad: FakePad, index: int = 0):
    """The voicebot label from the *index*th screen the fake pad saved.

    Needs the host library for the protobuf round-trip, so callers use
    ``pytest.importorskip`` first (this helper does it too, harmlessly).
    """
    pytest.importorskip("touchy_pad._proto")
    layer = pad.saved[index].to_proto().active
    return getattr(layer, layer.WhichOneof("kind")).layout.children[0]


# --- to_markup -------------------------------------------------------------


def test_white_text_is_plain():
    assert to_markup([Run("Time to toast!")]) == "Time to toast!"


def test_a_coloured_run_is_wrapped():
    assert to_markup([Run("Danger!", COLOURS["red"])]) == "#FF0000 Danger!#"


def test_mixed_runs():
    assert to_markup([Run("Look out ", WHITE), Run("Danger!", COLOURS["red"])]) == (
        "Look out #FF0000 Danger!#"
    )


def test_hashes_are_dropped():
    assert to_markup([Run("a #tag b", COLOURS["blue"])]) == "#0000FF a tag b#"


def test_no_runs_is_empty():
    assert to_markup([]) == ""


def test_markup_is_truncated_to_the_device_cap():
    assert len(to_markup([Run("x" * 2000)])) == MAX_OVERRIDE_CHARS


def test_a_coloured_run_is_truncated_within_budget():
    out = to_markup([Run("x" * 2000, COLOURS["red"])])
    assert len(out) == MAX_OVERRIDE_CHARS
    assert out.startswith("#FF0000 ")
    assert out.endswith("#")


def test_markup_takes_a_smaller_budget():
    assert len(to_markup([Run("x" * 2000)], max_chars=64)) == 64


# --- font_for --------------------------------------------------------------


def test_font_for_picks_the_largest_that_fits():
    assert (font_for(8), font_for(16), font_for(30), font_for(272), font_for(12)) == (
        8,
        16,
        30,
        30,
        8,
    )


# --- the layout policy -----------------------------------------------------


@pytest.mark.parametrize(
    ("height", "expected"),
    [
        (8, MODE_SCROLL),
        (16, MODE_SCROLL),
        (32, MODE_SCROLL),
        (33, MODE_MULTILINE),
        (64, MODE_MULTILINE),
        (272, MODE_MULTILINE),
    ],
)
def test_auto_mode_picks_by_panel_height(height, expected):
    assert resolve_mode(MODE_AUTO, height) == expected


def test_an_explicit_mode_wins_over_the_height_rule():
    assert resolve_mode(MODE_SCROLL, 272) == MODE_SCROLL
    assert resolve_mode(MODE_MULTILINE, 8) == MODE_MULTILINE


def test_an_unknown_mode_falls_back_to_the_height_rule():
    assert resolve_mode("bogus", 8) == MODE_SCROLL
    assert resolve_mode("bogus", 272) == MODE_MULTILINE


@pytest.mark.parametrize(
    ("height", "inset", "font"),
    [(272, 8, 30), (64, 8, 16), (48, 6, 16), (33, 4, 8)],
)
def test_panel_padding_and_font(height, inset, font):
    assert panel_inset(height) == inset
    assert font_for_panel(height) == font
    assert font_for_panel(height) in FONT_SIZES


# --- showing ---------------------------------------------------------------


def test_show_sends_one_text_override_and_builds_the_screen_once():
    pad = FakePad()
    PadDisplay(pad, clear_after=0).show([Run("Danger!", COLOURS["red"])])
    assert pad.texts() == ["#FF0000 Danger!#"]
    assert len(pad.saved) == 1
    assert pad.loaded == ["F:host/s/voicebot.pb"]


def test_start_shows_the_banner():
    pad = FakePad()
    PadDisplay(pad, clear_after=0).start()
    assert pad.texts() == [STARTUP_TEXT]


def test_the_saved_screen_is_a_scrolling_recolor_label():
    proto = pytest.importorskip("touchy_pad._proto")
    pad = FakePad(width=32, height=8)
    PadDisplay(pad, clear_after=0).show([Run("hi")])

    layer = pad.saved[0].to_proto().active
    label = getattr(layer, layer.WhichOneof("kind")).layout.children[0]
    assert label.id == LABEL_ID
    assert label.WhichOneof("kind") == "label"
    assert label.label.recolor is True
    assert label.label.text == STARTUP_TEXT
    assert label.label.long_mode == proto.LongMode.LONG_MODE_SCROLL_CIRCULAR
    assert label.label.scroll_speed_px_s == DEFAULT_SCROLL_SPEED_PX_S
    assert (label.rect.w, label.rect.h) == (32, 8)
    assert any(style.font_size == 8 for style in label.styles)


def test_a_tall_panel_gets_a_wrapped_multiline_label():
    proto = pytest.importorskip("touchy_pad._proto")
    pad = FakePad(width=480, height=272)
    PadDisplay(pad, clear_after=0).show([Run("hi")])

    label = saved_label(pad)
    assert label.id == LABEL_ID
    assert label.WhichOneof("kind") == "label"
    assert label.label.recolor is True
    assert label.label.text == STARTUP_TEXT
    assert label.label.long_mode == proto.LongMode.LONG_MODE_DOTS
    assert label.label.text_align == proto.TextAlign.LEFT
    assert label.label.scroll_speed_px_s == 0
    assert (label.rect.x, label.rect.y, label.rect.w, label.rect.h) == (8, 8, 464, 256)
    assert any(style.font_size == 30 for style in label.styles)


def test_scroll_can_be_forced_on_a_tall_panel():
    proto = pytest.importorskip("touchy_pad._proto")
    pad = FakePad(width=480, height=272)
    PadDisplay(pad, clear_after=0, display_mode=MODE_SCROLL).show([Run("hi")])

    label = saved_label(pad)
    assert label.label.long_mode == proto.LongMode.LONG_MODE_SCROLL_CIRCULAR
    assert (label.rect.w, label.rect.h) == (480, 272)


def test_multiline_can_be_forced_on_a_short_panel():
    proto = pytest.importorskip("touchy_pad._proto")
    pad = FakePad(width=32, height=8)
    PadDisplay(pad, clear_after=0, display_mode=MODE_MULTILINE).show([Run("hi")])

    assert saved_label(pad).label.long_mode == proto.LongMode.LONG_MODE_DOTS


def test_swapping_the_panel_rebuilds_the_screen_in_the_new_mode():
    proto = pytest.importorskip("touchy_pad._proto")
    led = FakePad(width=32, height=8)
    display = PadDisplay(led, clear_after=0)
    display.show([Run("a")])
    assert display._mode == MODE_SCROLL

    display._forget_pad()  # the LED matrix is unplugged
    lcd = FakePad(width=480, height=272)
    display._pad = lcd  # the LCD is plugged in
    display.show([Run("b")])

    assert display._mode == MODE_MULTILINE
    assert saved_label(lcd).label.long_mode == proto.LongMode.LONG_MODE_DOTS
    assert led.closed is True


def test_a_changed_panel_size_is_noticed_without_a_reconnect():
    proto = pytest.importorskip("touchy_pad._proto")
    display = PadDisplay(FakePad(width=32, height=8), clear_after=0)
    display.show([Run("a")])

    lcd = FakePad(width=480, height=272)
    display._pad = lcd  # the same handle slot now reports the LCD
    display.show([Run("b")])

    assert display._mode == MODE_MULTILINE
    assert saved_label(lcd).label.long_mode == proto.LongMode.LONG_MODE_DOTS


def test_the_marquee_speed_is_configurable():
    """Stage LB19's `scroll_speed_px_s` is the knob that slows the marquee."""
    pytest.importorskip("touchy_pad._proto")
    pad = FakePad(width=32, height=8)
    PadDisplay(pad, clear_after=0, scroll_speed_px_s=12).show([Run("hi")])

    layer = pad.saved[0].to_proto().active
    label = getattr(layer, layer.WhichOneof("kind")).layout.children[0]
    assert label.label.scroll_speed_px_s == 12


def test_the_screen_is_not_rebuilt_per_quip():
    pad = FakePad()
    display = PadDisplay(pad, clear_after=0)
    display.show([Run("a")])
    display.show([Run("b")])
    assert len(pad.saved) == 1
    assert pad.texts() == ["a", "b"]


def test_connect_retries_once_on_a_transient_failure(monkeypatch):
    """A bad first response (stale device frame) self-heals on the retry."""
    from voicebot import display as display_mod

    calls: list[int] = []

    def flaky_connect():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("device returned NOT_FOUND (3)")
        return FakePad()

    monkeypatch.setattr(display_mod, "connect", flaky_connect)
    display = display_mod.PadDisplay(clear_after=0)
    display.show([Run("hi")])
    assert len(calls) == 2
    assert display._pad is not None


def test_connect_gives_up_after_the_allowed_attempts(monkeypatch, caplog):
    from voicebot import display as display_mod

    calls: list[int] = []

    def always_fail():
        calls.append(1)
        raise RuntimeError("no pad")

    monkeypatch.setattr(display_mod, "connect", always_fail)
    with caplog.at_level("WARNING"):
        display_mod.PadDisplay(clear_after=0).show([Run("hi")])
    assert len(calls) == display_mod.CONNECT_ATTEMPTS
    assert "no touchy-pad" in caplog.text


def test_a_headless_panel_is_reported_and_skipped(caplog):
    pad = FakePad(width=0, height=0)
    with caplog.at_level("WARNING"):
        PadDisplay(pad, clear_after=0).show([Run("hi")])
    assert pad.saved == [] and pad.batches == []
    assert "0x0" in caplog.text


def test_show_and_start_report_whether_a_pad_was_drawn():
    """``jokemark`` uses this to skip a machine with no pad — without retrying."""
    assert PadDisplay(FakePad(), clear_after=0).show([Run("hi")]) is True


def test_show_and_start_are_false_without_a_pad(monkeypatch):
    from voicebot import display as display_mod

    def no_pad():
        raise RuntimeError("no pad")

    monkeypatch.setattr(display_mod, "connect", no_pad)
    display = display_mod.PadDisplay(clear_after=0)
    assert display.show([Run("hi")]) is False
    assert display.start() is False


def test_a_lost_pad_is_dropped_and_reconnects_next_time():
    pad = FakePad(failures=1)  # the write fails ⇒ the handle is dropped
    display = PadDisplay(pad, clear_after=0)
    display.show([Run("first")])
    assert display._pad is None
    assert pad.closed is True
    display._pad = pad  # the user plugs it back in
    display.show([Run("second")])
    assert pad.texts() == ["second"]


# --- the idle clear --------------------------------------------------------


def test_clear_after_zero_disarms_the_timer():
    display = PadDisplay(FakePad(), clear_after=0)
    display.show([Run("hi")])
    assert display._timer is None


def test_showing_arms_the_timer_and_clearing_blanks_the_marquee():
    pad = FakePad()
    display = PadDisplay(pad, clear_after=120)
    display.show([Run("hi")])
    assert display._timer is not None
    display._clear()
    assert pad.texts() == ["hi", ""]
    assert display._timer is None


def test_close_stops_the_timer_and_releases_the_pad():
    pad = FakePad()
    display = PadDisplay(pad, clear_after=120)
    display.show([Run("hi")])
    display.close()
    assert pad.closed is True
    assert display._timer is None
