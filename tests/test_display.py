"""The LED-out display: colour markup, the scrolling screen, lazy connect, idle clear.

Driven through a duck-typed fake pad, so no hardware is needed; the screen-shape
assertions additionally need the host library (``touchy_pad``), which the path
dependency installs. See ``docs/plans/ledout.md``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dankbot.commands import COLOURS, WHITE, Run
from dankbot.display import (
    DEFAULT_SCROLL_SPEED_PX_S,
    FONT_PROPERTY,
    FONT_SIZES,
    LABEL_ID,
    MAX_OVERRIDE_CHARS,
    MODE_AUTO,
    MODE_MULTILINE,
    MODE_SCROLL,
    STARTUP_TEXT,
    STRIP_EMOJI,
    PadDisplay,
    chars_that_fit,
    font_for,
    font_for_panel,
    font_for_text,
    panel_inset,
    resolve_mode,
    to_ascii,
    to_markup,
    visible_chars,
)


class FakePad:
    """A duck-typed stand-in for a ``touchy_pad.api.Touchy``."""

    def __init__(
        self, width: int = 32, height: int = 8, *, failures: int = 0, reject_font: bool = False
    ) -> None:
        self.board_info = SimpleNamespace(display_width=width, display_height=height)
        self.saved: list[object] = []
        self.loaded: list[str] = []
        self.batches: list[list[object]] = []
        self.closed = False
        self._failures = failures
        self._reject_font = reject_font

    def screen_save(self, screen):
        self.saved.append(screen)

    def screen_load(self, path):
        self.loaded.append(path)

    def set_properties(self, entries):
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("usb gone")
        if self._reject_font and any(e.WhichOneof("value") == "font_size_value" for e in entries):
            from touchy_pad.api.errors import TouchyError

            raise TouchyError(2, "INVALID_ARG")  # a firmware without `text_font`
        self.batches.append(list(entries))

    def close(self):
        self.closed = True

    def texts(self) -> list[str]:
        """The string-valued entries — the quips, without the font-size entries."""
        return [
            entry.string_value
            for batch in self.batches
            for entry in batch
            if entry.WhichOneof("value") == "string_value"
        ]

    def fonts(self) -> list[int]:
        """The ``text_font`` pixel sizes sent, one per quip."""
        return [
            entry.font_size_value
            for batch in self.batches
            for entry in batch
            if entry.WhichOneof("value") == "font_size_value"
        ]

    def entry_names(self, batch: int = 0) -> list[str]:
        """The property names of one batch, in order."""
        return [entry.property_name for entry in self.batches[batch]]


def saved_label(pad: FakePad, index: int = 0):
    """The dankbot label from the *index*th screen the fake pad saved.

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


# --- visible_chars (what actually has to fit) ------------------------------


def test_visible_chars_counts_the_text():
    assert visible_chars([Run("Time to toast!")]) == len("Time to toast!")


def test_visible_chars_ignores_the_colour_markup():
    runs = [Run("Look out "), Run("Danger!", COLOURS["red"])]
    assert visible_chars(runs) == len("Look out Danger!")
    assert len(to_markup(runs)) > visible_chars(runs)


def test_visible_chars_counts_the_drawn_text_after_the_fold():
    assert visible_chars([Run("  hi #tag \U0001f914 ")]) == len("hi tag")


# --- to_ascii (the device font is ASCII-only) -------------------------------


def test_to_ascii_keeps_plain_text():
    assert to_ascii("Time to toast!") == "Time to toast!"


def test_to_ascii_drops_emoji():
    assert to_ascii("🤔 I bet 💪") == " I bet "


def test_to_ascii_folds_typographic_punctuation():
    assert to_ascii("don’t — “stop” …") == 'don\'t - "stop" ...'


def test_to_ascii_decomposes_accents():
    assert to_ascii("café naïve") == "cafe naive"


def test_to_ascii_squeezes_the_space_a_dropped_emoji_leaves():
    assert to_ascii("toast 🍞 crunch") == "toast crunch"


def test_emoji_never_reach_the_device():
    # The bread quip from the field: emoji are dropped, not drawn as boxes.
    out = to_markup([Run("🤔 I bet the bread's been secretly training for the Iron Loaf! 💪")])
    assert out == "I bet the bread's been secretly training for the Iron Loaf!"


def test_emoji_in_a_coloured_run_are_dropped_too():
    assert to_markup([Run("🤔 Danger! 💪", COLOURS["red"])]) == "#FF0000 Danger!#"


def test_an_emoji_only_quip_sends_nothing():
    assert to_markup([Run("🤔💪")]) == ""


def test_dropped_emoji_do_not_eat_into_the_device_budget():
    # 20 emoji would halve the usable length if they were counted.
    assert to_markup([Run("🧇" * 20 + "x" * 600)], max_chars=64) == "x" * 64


# --- the STRIP_EMOJI switch --------------------------------------------------


def test_emoji_are_stripped_by_default():
    assert STRIP_EMOJI is True


def test_the_emoji_fold_can_be_switched_off(monkeypatch):
    # Once the device font has emoji glyphs, flipping STRIP_EMOJI keeps them.
    monkeypatch.setattr("dankbot.display.STRIP_EMOJI", False)
    assert to_markup([Run("🤔 I bet 💪")]) == "🤔 I bet 💪"
    assert to_markup([Run("🤔 Danger! 💪", COLOURS["red"])]) == "#FF0000 🤔 Danger! 💪#"


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
    [(272, 8, 30), (64, 8, 24), (48, 6, 16), (33, 4, 8)],
)
def test_panel_padding_and_font(height, inset, font):
    assert panel_inset(height) == inset
    assert font_for_panel(height) == font
    assert font_for_panel(height) in FONT_SIZES


# --- fitting a long quip ---------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "font", "capacity"),
    [
        (480, 272, 30, 168),  # the attached LCD: ~7 lines of 24 characters
        (480, 272, 24, 248),  # the middle rung: ~8 lines of 31 characters
        (480, 272, 16, 598),  # more than the 511-character protocol cap
        (128, 64, 24, 7),  # one 24-px line — what font_for_panel(64) builds
        (128, 64, 16, 22),
        (128, 48, 8, 69),
    ],
)
def test_chars_that_fit(width, height, font, capacity):
    assert chars_that_fit(width, height, font) == capacity


def test_a_smaller_font_fits_more():
    capacities = [chars_that_fit(480, 272, size) for size in sorted(FONT_SIZES, reverse=True)]
    assert capacities == sorted(capacities)


@pytest.mark.parametrize(
    ("chars", "font"),
    [
        (0, 30),
        (60, 30),
        (168, 30),  # the last quip 30 px holds on the LCD
        (169, 24),  # … so the next character steps down to 24, not straight to 16
        (200, 24),
        (248, 24),  # the last quip 24 px holds
        (249, 16),
        (511, 16),  # the protocol cap still fits at 16
        (600, 8),
    ],
)
def test_font_for_text_steps_down_for_a_long_quip(chars, font):
    assert font_for_text(480, 272, chars) == font


def test_font_for_text_never_leaves_the_compiled_sizes():
    for chars in (0, 1, 500, 5000):
        assert font_for_text(480, 272, chars) in FONT_SIZES
    assert font_for_text(32, 8, 5000) in FONT_SIZES


def test_the_tiers_are_sizes_the_firmware_compiles():
    """Our tiers must stay inside the host library's ``FONT_SIZES``.

    ``build_property_override(…, "text_font", size)`` raises ``ValueError`` for a
    size outside the set touchy-pad publishes, and that refusal would look like a
    lost pad (the quip would not render at all) rather than a smaller font. The
    device only renders the fonts its build compiled, so this is the one place
    the two lists meet — 24 px arrived with touchy-pad 1.0.0.
    """
    api = pytest.importorskip("touchy_pad.api")
    assert 24 in FONT_SIZES
    assert set(FONT_SIZES) <= set(api.FONT_SIZES)


def test_font_for_text_keeps_an_explicit_size_as_the_ceiling():
    assert font_for_text(480, 272, 10, ceiling=16) == 16  # never the 30-px tier
    assert font_for_text(480, 272, 5000, ceiling=16) == 8  # may still step below


# --- showing ---------------------------------------------------------------


def test_show_sends_one_text_override_and_builds_the_screen_once():
    pad = FakePad()
    PadDisplay(pad, clear_after=0).show([Run("Danger!", COLOURS["red"])])
    assert pad.texts() == ["#FF0000 Danger!#"]
    assert len(pad.saved) == 1
    assert pad.loaded == ["F:host/s/dankbot.pb"]


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


# --- the font tier per quip ------------------------------------------------


def test_a_short_quip_keeps_the_lcd_font():
    pad = FakePad(width=480, height=272)
    PadDisplay(pad, clear_after=0).show([Run("Short and sweet.")])
    assert pad.fonts() == [30]
    assert pad.texts() == ["Short and sweet."]


def test_a_long_quip_steps_down_to_the_middle_font():
    """169–248 drawn characters fit the LCD at 24 px, so they land there."""
    pad = FakePad(width=480, height=272)
    PadDisplay(pad, clear_after=0).show([Run("x" * 200)])
    assert pad.fonts() == [24]
    assert len(pad.saved) == 1  # a property override, not a screen rebuild


def test_a_very_long_quip_steps_past_the_middle_font():
    """Past 24 px's 248 characters the ladder continues to 16 — never straight
    from 30 to 8, and never off the ladder (the pad only has these fonts)."""
    pad = FakePad(width=480, height=272)
    PadDisplay(pad, clear_after=0).show([Run("x" * 300)])
    assert pad.fonts() == [16]


def test_the_text_and_the_font_are_one_batch():
    pad = FakePad(width=480, height=272)
    PadDisplay(pad, clear_after=0).show([Run("x" * 200)])
    assert len(pad.batches) == 1
    assert pad.entry_names() == ["text", FONT_PROPERTY]


def test_the_marquee_keeps_its_font_however_long_the_quip():
    pad = FakePad(width=32, height=8)
    PadDisplay(pad, clear_after=0).show([Run("x" * 400)])
    assert pad.fonts() == [8]
    assert len(pad.saved) == 1


def test_the_font_is_sent_every_quip_so_a_stale_override_cannot_stick():
    """Overrides are sticky on the device — a restart must not inherit a small font."""
    pad = FakePad(width=480, height=272)
    display = PadDisplay(pad, clear_after=0)
    display.show([Run("x" * 200)])
    display.show([Run("Short.")])
    assert pad.fonts() == [24, 30]


def test_an_explicit_font_size_is_never_exceeded():
    pad = FakePad(width=480, height=272)
    PadDisplay(pad, clear_after=0, font_size=16).show([Run("Short.")])
    assert pad.fonts() == [16]


def test_colour_markup_does_not_shrink_a_quip_that_fits():
    """160 drawn characters fit at 30 px even though the markup is over 168."""
    pad = FakePad(width=480, height=272)
    body = "x" * 80
    PadDisplay(pad, clear_after=0).show([Run(body), Run(body, COLOURS["red"])])
    assert visible_chars([Run(body), Run(body, COLOURS["red"])]) == 160
    assert pad.fonts() == [30]


def test_a_pad_without_the_font_override_still_shows_the_quip():
    """A pre-lb26 firmware refuses the batch — the text must still land."""
    pad = FakePad(width=480, height=272, reject_font=True)
    display = PadDisplay(pad, clear_after=0)
    assert display.show([Run("x" * 200)]) is True
    assert pad.texts() == ["x" * 200]
    assert pad.fonts() == []
    assert display._font_unsupported is True

    display.show([Run("x" * 200)])  # the session remembers — no second failure
    assert pad.texts() == ["x" * 200, "x" * 200]
    assert len(pad.batches) == 2


def test_connect_retries_once_on_a_transient_failure(monkeypatch):
    """A bad first response (stale device frame) self-heals on the retry."""
    from dankbot import display as display_mod

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
    from dankbot import display as display_mod

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
    from dankbot import display as display_mod

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
