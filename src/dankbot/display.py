"""Display out: show the quip on a touchy-pad (stage 3).

Two layouts, picked from the panel height (``docs/plans/lcdout.md``):

* a ``<= 32``-px-tall panel gets a full-panel **scrolling marquee** — the LED
  matrix layout (``docs/plans/ledout.md``);
* a taller panel gets one **multiline text field** that wraps to the panel width
  and ellipsizes when it runs out of vertical room — the LCD layout.

Both screens are built once per connection and saved; every quip afterwards is a
single ``SetPropertiesCmd`` batch — the label's ``text``, carrying LVGL
inline-recolour markup, plus its ``text_font`` pixel size so a long reply steps
down to a smaller compiled font instead of being ellipsized — never a screen
re-upload (``docs/plans/quipfit.md``).

This is the **only** module that imports ``touchy_pad``, so pulling dankbot out
to its own repo stays a one-file diff plus the PyPI switch — and the import is
*lazy* (inside the functions that need it) so the module can be imported and
unit-tested without the host library installed.

A pad is opened **lazily**, only when there is something to show, so a pad
plugged in later is picked up on the next message and a missing one is never
fatal. An idle timer blanks the display after ``clear_after`` seconds of quiet.

The panel's font is ASCII-only, so every quip is folded to ASCII on the way out
(:func:`to_ascii`, gated by :data:`STRIP_EMOJI` — on for now): emoji are dropped
rather than drawn as missing-glyph boxes. Flip that flag once the device font
grows emoji glyphs.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable, Sequence
from threading import Lock, Timer
from typing import Any

from .commands import Run

log = logging.getLogger(__name__)

#: A display sink: handed one quip's colour runs (``Reply.runs``).
#: :meth:`PadDisplay.show` is the real one — it answers ``False`` when nothing was
#: drawn; ``None`` in a loop means "show nothing".
DisplaySink = Callable[[tuple[Run, ...]], bool | None]

#: On-device screen + widget names — the contract between the saved screen and
#: the runtime property overrides. Deliberately *not* ``default`` so a later
#: ``touchy init`` / ``touchy screen demo`` cannot clobber it (or vice-versa).
SCREEN_NAME = "dankbot"
LABEL_ID = "dankbot_msg"

#: What the panel shows on connect, and the colour LVGL draws unmarked text in.
STARTUP_TEXT = "I'm dankbot"
WHITE = 0xFFFFFF

#: The label font sizes dankbot chooses between. The device renders only the
#: fonts its build compiled: 8 (LVGL's UNSCII bitmap face — the one legible face
#: on an 8-px matrix), 14 (Montserrat, the theme default), 16, 24 and 30. 14 is
#: skipped on purpose — it is barely smaller than 16, and 16 is glass-clear where
#: 14 is not — so dankbot's tiers are 8 / 16 / 24 / 30, the same set touchy-pad
#: publishes as ``FONT_SIZES`` (1.0.0 grew it with the 24-px Montserrat face,
#: ``CONFIG_LV_FONT_MONTSERRAT_24``). 24 is the step-down's **middle rung**: a
#: quip too long for 30 px keeps a bigger, clearer font than the old 30 → 16
#: jump. See ``docs/plans/quipfit.md``.
FONT_SIZES = (8, 16, 24, 30)

#: The LVGL style property that carries a label's font. An *int* value for this
#: name means a pixel size: touchy-pad routes it to
#: ``SetPropertyCmd.font_size_value`` (stage lb26) and the device maps it with
#: ``font_for_size()``. Any other value arm aimed at it is refused — LVGL's real
#: ``text_font`` value is a ``const lv_font_t *``, so an int would be read as a
#: font pointer.
FONT_PROPERTY = "text_font"

#: The two text layouts, plus ``auto`` — pick by panel height. ``auto`` is what
#: ``dankbot listen`` uses; the explicit modes are an escape hatch for a panel
#: the height rule classifies badly (e.g. a 64x64 LED matrix).
MODE_AUTO = "auto"
MODE_SCROLL = "scroll"
MODE_MULTILINE = "multiline"
DISPLAY_MODES = (MODE_AUTO, MODE_SCROLL, MODE_MULTILINE)

#: Taller than this ⇒ a multiline text field; at or below ⇒ a one-line marquee.
#: Every LED chain template the firmware ships is at most 32 rows tall
#: (``led-32x8``, ``led-32x16-chain``, ``led-32x32-chain`` …), while LCD panels
#: are 240–320 px tall (the ``jc4827w543`` is 272). See ``lcdout.md``.
PANEL_HEIGHT_THRESHOLD_PX = 32

#: Breathing room around the multiline text field, and the fewest lines of text
#: a chosen font must leave room for. Both only apply above the height rule's
#: threshold (the scrolling layout stays edge-to-edge).
PANEL_PADDING_PX = 8
MIN_PANEL_LINES = 2

#: Marquee speed for the scrolling label, in px/s (touchy-pad ≥ 0.3.8, the
#: release that carries stage LB19's ``Label.scroll_speed_px_s``). LVGL's own
#: default — ``LV_LABEL_DEF_SCROLL_SPEED`` in its ``lv_label.c`` — is 40 px/s,
#: which blurs past on an 8-px-tall matrix; half of it reads comfortably. Both
#: the host library and the firmware clamp this to 10..10000 px/s, and LVGL
#: still caps one traversal at 10 s, so a *very* long quip scrolls faster than
#: this floor (see ``docs/plans/ledout.md``).
DEFAULT_SCROLL_SPEED_PX_S = 30

#: Blank the display after this many seconds with no new reply (0 = never).
DEFAULT_CLEAR_AFTER_S = 120.0

#: The device stores a `text` override in a fixed buffer: nanopb rejects a
#: `SetPropertyCmd.string_value` longer than its `max_size`, and the firmware has
#: no way to report that cap. `max_size` is 512 *including* the NUL terminator
#: (stage LB17 raised it from 128), so 511 characters of rendered markup is the
#: most the device accepts — measured on hardware. Quips are truncated to fit.
MAX_OVERRIDE_CHARS = 511

#: Connect attempts before giving up until the next message. A bad *first*
#: response (a leftover frame in the device FIFO from a previously-killed host)
#: is a known transient that self-heals — see the parent repo's open-issues.md.
CONNECT_ATTEMPTS = 2


class DisplayError(RuntimeError):
    """The pad is reachable but can't be driven (e.g. a headless 0×0 panel)."""


def connect() -> Any:
    """Open a touchy-pad connection (USB, or the simulator via ``TOUCHY_SIM_URL``)."""
    from touchy_pad.api import touchy_open

    return touchy_open()


def font_for(height: int) -> int:
    """The largest *compiled* font size that fits *height* (the plan's Q6).

    A size with no compiled font renders at the theme default instead, so stay
    inside the set the firmware actually ships.
    """
    fits = [size for size in FONT_SIZES if size <= height]
    return max(fits) if fits else FONT_SIZES[0]


def resolve_mode(requested: str, height: int) -> str:
    """The layout for a panel: an explicit choice, else the height rule.

    ``MODE_SCROLL`` / ``MODE_MULTILINE`` win over the geometry. Anything else —
    ``auto``, an unknown env-var value — falls through to the rule, because a
    bad configuration must not stop a performance.
    """
    if requested in (MODE_SCROLL, MODE_MULTILINE):
        return requested
    return MODE_SCROLL if height <= PANEL_HEIGHT_THRESHOLD_PX else MODE_MULTILINE


def panel_inset(height: int) -> int:
    """Breathing room around the multiline text field, smaller on short panels."""
    return max(2, min(PANEL_PADDING_PX, height // 8))


def font_for_panel(height: int) -> int:
    """Largest compiled font that still leaves :data:`MIN_PANEL_LINES` of text.

    Unlike :func:`font_for` this keeps room for a *second* line, so a short LCD
    drops to 16 px rather than rendering one clipped line at 30 px.
    """
    inner = height - 2 * panel_inset(height)
    fits = [size for size in FONT_SIZES if size * MIN_PANEL_LINES <= inner]
    return max(fits) if fits else FONT_SIZES[0]


#: Average glyph advance as a fraction of the font's px size, for the Montserrat
#: and UNSCII faces the firmware compiles. Real mixed-case text runs at ~0.50–0.55
#: em; 0.62 is deliberately generous because this estimate only decides when to
#: step *down* a font size: a false "it fits" is a clipped quip, a false "it
#: doesn't" only costs a smaller — but still complete — quip.
GLYPH_ADVANCE_RATIO = 0.62

#: Line height as a multiple of the font px size (LVGL's Montserrat ≈ 1.19).
LINE_HEIGHT_RATIO = 1.2


def chars_that_fit(width: int, height: int, font: int) -> int:
    """How many characters the multiline field draws at *font* px.

    A rough, deliberately pessimistic estimate: the field's inner rectangle
    (:func:`panel_inset`) divided into glyph cells. It only has to be right about
    *which* compiled font the quip needs, and the tiers are far apart
    (30 → 24 → 16).
    """
    inset = panel_inset(height)
    cols = max(1, int((width - 2 * inset) // (font * GLYPH_ADVANCE_RATIO)))
    rows = max(1, int((height - 2 * inset) // (font * LINE_HEIGHT_RATIO)))
    return cols * rows


def font_for_text(width: int, height: int, chars: int, *, ceiling: int | None = None) -> int:
    """The largest compiled font whose field fits *chars*; the smallest if none do.

    *ceiling* is an explicitly configured size (:class:`PadDisplay`'s
    ``font_size=``): it stays the largest tier, so a pinned font is never
    exceeded. Sizes outside :data:`FONT_SIZES` are ignored rather than validated —
    the device only renders the compiled ones either way.
    """
    tiers = sorted(FONT_SIZES, reverse=True)
    if ceiling is not None:
        tiers = [size for size in tiers if size <= ceiling] or tiers
    for size in tiers:
        if chars_that_fit(width, height, size) >= chars:
            return size
    return tiers[-1]


#: Fold every quip to ASCII before it goes to the device (:func:`to_ascii`).
#: **On** because the panel's font is ASCII-only today, so an emoji draws as a
#: missing-glyph box. Set this to ``False`` once the device font has emoji
#: glyphs — nothing else needs to change.
STRIP_EMOJI = True

#: Typographic punctuation a small model reaches for, mapped to the ASCII the
#: panel font actually draws. Emoji have no sensible equivalent and are dropped.
_PUNCTUATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
)

#: Any run of whitespace — including the double space a dropped emoji leaves
#: behind, and a line break — becomes one space on the panel.
_SPACE_RUN_RE = re.compile(r"\s+")


def to_ascii(text: str) -> str:
    """Fold *text* down to the ASCII the panel's font can draw.

    The device font has no glyph beyond ASCII, so an emoji (``🤔``) would draw as
    a blank or a missing-glyph box. Accents are decomposed (``é`` → ``e``) and
    the typographic punctuation models like is mapped to ASCII (``don’t`` →
    ``don't``), so the meaning survives the fold; everything still outside ASCII
    — emoji, their skin-tone/joiner modifiers, symbols, CJK — is dropped. The
    leftovers are then squeezed to single spaces, because a dropped emoji leaves
    a double space behind.
    """
    folded = unicodedata.normalize("NFKD", text.translate(_PUNCTUATION))
    return _SPACE_RUN_RE.sub(" ", "".join(ch for ch in folded if ch.isascii()))


def _drawn_runs(runs: Sequence[Run]) -> list[tuple[int, str]]:
    """The runs as the panel will draw them: ASCII-folded, hash-free, edges trimmed.

    The one fold shared by :func:`to_markup` and :func:`visible_chars`, so the
    characters we *budget* for are exactly the characters we *send*.
    """
    texts: list[tuple[int, str]] = []
    for run in runs:
        text = (to_ascii(run.text) if STRIP_EMOJI else run.text).replace("#", "")
        if not texts:
            text = text.lstrip(" ")  # no blank space before the first drawn run
        if text:
            texts.append((run.colour, text))
    if texts:  # no trailing space either (e.g. right after a dropped emoji)
        colour, text = texts[-1]
        texts[-1] = (colour, text.rstrip(" "))
    return texts


def visible_chars(runs: Sequence[Run]) -> int:
    """Characters the panel actually draws — the markup's ``#RRGGBB …#`` is not text.

    A coloured run spends nine extra bytes on the wire (``#RRGGBB`` + space +
    closing ``#``) that draw nothing, so measuring ``len(to_markup(...))`` would
    shrink a quip the field has room for.
    """
    return sum(len(text) for _, text in _drawn_runs(runs))


def to_markup(runs: Sequence[Run], max_chars: int = MAX_OVERRIDE_CHARS) -> str:
    """Colour runs → LVGL inline-recolour text, truncated to *max_chars*.

    A non-white run becomes ``#RRGGBB text#`` — the space after the colour
    terminates the command and is not drawn, and the closing ``#`` reverts to the
    label's ``text_color`` — while a white run is emitted plainly. A literal
    ``#`` is dropped: it delimits a command and this LVGL version's ``##`` escape
    is dead code. Each run is folded to ASCII first (:func:`to_ascii`) while
    :data:`STRIP_EMOJI` is set, so an emoji never reaches the pad and never eats
    into the budget below.

    *max_chars* bounds the **rendered** length because the device keeps a
    ``text`` override in a fixed buffer and rejects anything longer (see
    :data:`MAX_OVERRIDE_CHARS`). Truncation happens on a run boundary, so the
    markup always stays well-formed.
    """
    parts: list[str] = []
    used = 0
    for colour, text in _drawn_runs(runs):
        opening, closing = ("", "") if colour == WHITE else (f"#{colour:06X} ", "#")
        room = max_chars - used - len(opening) - len(closing)
        if room <= 0:
            break
        body = text[:room]
        parts.append(f"{opening}{body}{closing}")
        used += len(opening) + len(body) + len(closing)
        if len(body) < len(text):
            break  # truncated — stop rather than opening another run
    return "".join(parts)


def _daemon_timer(delay_s: float, fn: Callable[[], None]) -> Timer:
    timer = Timer(delay_s, fn)
    timer.daemon = True
    return timer


def _is_refusal(exc: BaseException) -> bool:
    """True when the *device* answered ``INVALID_ARG`` — not a broken link.

    The pad applies a property batch entry-by-entry and answers ``INVALID_ARG``
    when an entry cannot be applied (e.g. a firmware that predates the
    ``text_font`` override). Transport failures raise other exceptions, and those
    must keep dropping the handle the way they always have.
    """
    from touchy_pad.api.errors import TouchyError

    return isinstance(exc, TouchyError) and exc.code_name == "INVALID_ARG"


def _safe_close(pad: Any) -> None:
    if pad is None:
        return
    try:
        pad.close()
    except Exception:
        log.debug("dankbot: closing the pad failed", exc_info=True)


class PadDisplay:
    """Owns the on-device text screen and the per-quip updates.

    The screen is a scrolling marquee on a short panel and a wrapped multiline
    text field on a tall one (:func:`resolve_mode`). ``pad`` may be injected
    (tests use a stub); otherwise a real pad is opened lazily on the first
    :meth:`show`. All pad calls are serialized by a lock, because :meth:`show`
    runs on the listener's reply worker while the idle-clear timer fires on its
    own thread.
    """

    def __init__(
        self,
        pad: Any | None = None,
        *,
        screen_name: str = SCREEN_NAME,
        clear_after: float = DEFAULT_CLEAR_AFTER_S,
        display_mode: str = MODE_AUTO,
        font_size: int | None = None,
        scroll_speed_px_s: int = DEFAULT_SCROLL_SPEED_PX_S,
        timer_factory: Callable[[float, Callable[[], None]], Timer] = _daemon_timer,
    ) -> None:
        self._pad = pad
        self._prepared = False
        self._screen_name = screen_name
        self._clear_after = float(clear_after)
        self._display_mode = display_mode
        self._font_size = font_size
        self._scroll_speed_px_s = scroll_speed_px_s
        self._timer_factory = timer_factory
        self._font_unsupported = False
        self._lock = Lock()
        self._timer: Timer | None = None
        self._size: tuple[int, int] = (0, 0)
        self._mode: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Show the startup banner (connecting lazily); ``False`` when there is no pad."""
        return self.show([Run(STARTUP_TEXT)])

    def close(self) -> None:
        """Stop the idle timer and release the pad (idempotent)."""
        with self._lock:
            self._cancel_timer()
            self._forget_pad()

    # -- the public verb ---------------------------------------------------

    def show(self, runs: Sequence[Run]) -> bool:
        """Render *runs* on the pad (empty ⇒ clear) and (re)arm the idle timer.

        ``False`` means nothing was drawn — no pad is attached, or the one that
        was is gone. Callers that treat the display as optional (``jokemark``)
        use the result to stop asking a machine with no pad; ``listen`` ignores
        it, because it wants a pad plugged in later to be picked up.
        """
        with self._lock:
            pad = self._open()
            if pad is None:
                return False
            if not self._write_quip(pad, runs):
                self._forget_pad()
                return False
            self._arm_timer()
            return True

    def _write_quip(self, pad: Any, runs: Sequence[Run]) -> bool:
        """Send the quip; ``False`` means the pad looks gone (drop the handle).

        One ``SetPropertiesCmd`` batch carries the text *and* the font size this
        quip needs (:func:`font_for_text`), so a long reply steps down to a
        smaller compiled font instead of being ellipsized by ``DOTS``.
        :func:`to_markup` keeps the string inside the device's buffer, so a
        refusal here means the link itself is broken rather than a bad payload.
        """
        try:
            lvgl = to_markup(runs)
            font = self._font_for_quip(runs)
            log.debug("dankbot: rendering quip at %dpx: %s", font, lvgl)
            self._write(pad, lvgl, font)
            return True
        except Exception as exc:  # noqa: BLE001 — a lost pad is not fatal
            log.warning("dankbot: lost the touchy-pad (%s) — will reconnect", exc)
            return False

    def _font_for_quip(self, runs: Sequence[Run]) -> int:
        """The font size this quip is drawn at.

        The scrolling layout cannot overflow — it scrolls forever — so the marquee
        keeps the one font its screen was built with. The multiline field has to
        *fit*, so its font steps down while the quip is long. The font is written
        with every quip rather than only on a change: overrides are sticky on the
        **device**, so a size left behind by an earlier host run would otherwise
        outlive a restart of dankbot.
        """
        width, height = self._size
        if self._mode != MODE_MULTILINE:
            return self._font_size or font_for(height)
        return font_for_text(width, height, visible_chars(runs), ceiling=self._font_size)

    # -- the pad -----------------------------------------------------------

    def _open(self) -> Any | None:
        """The pad, connecting + provisioning lazily; ``None`` (logged) on failure."""
        if self._pad is None:
            self._pad = self._connect()
            if self._pad is None:
                return None
        if not self._prepared or self._panel_changed():
            try:
                self._prepare(self._pad)
            except Exception as exc:  # noqa: BLE001 — a bad pad is not fatal
                log.warning("dankbot: found a touchy-pad but can't drive it (%s)", exc)
                self._forget_pad()
                return None
            self._prepared = True
            width, height = self._size
            info = getattr(self._pad, "board_info", None)
            board = getattr(info, "board_name", "") or "?"
            log.info(
                "dankbot: display ready on %dx%d (%s, %s, screen %r)",
                width,
                height,
                board,
                self._mode,
                self._screen_name,
            )
        return self._pad

    def _panel_changed(self) -> bool:
        """True when the attached pad reports a different panel than the saved screen."""
        info = getattr(self._pad, "board_info", None)
        width = int(getattr(info, "display_width", 0) or 0)
        height = int(getattr(info, "display_height", 0) or 0)
        return (width, height) != self._size

    def _connect(self) -> Any | None:
        """Open the pad, retrying once; ``None`` (logged) when there is none.

        A bad *first* response — a leftover frame in the device's bulk-IN FIFO
        from a host process that was killed mid-RPC — is a documented transient
        that self-heals on the next attempt (see the parent repo's
        ``docs/open-issues.md``), so one retry turns a confusing failure into a
        working connection.
        """
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                return connect()
            except Exception as exc:  # noqa: BLE001 — a failed connect is a retry reason
                if attempt + 1 < CONNECT_ATTEMPTS:
                    log.debug("dankbot: connect attempt %d failed (%s); retrying", attempt + 1, exc)
                    continue
                log.warning("dankbot: no touchy-pad to show this on (%s)", exc)
        return None

    def _prepare(self, pad: Any) -> None:
        """Read the panel, choose its layout, then save + load that screen."""
        from touchy_pad.paths import screen_path

        info = getattr(pad, "board_info", None)
        width = int(getattr(info, "display_width", 0) or 0)
        height = int(getattr(info, "display_height", 0) or 0)
        if width <= 0 or height <= 0:
            raise DisplayError(
                "the panel reports 0x0 — provision a display first, e.g. "
                "`touchy pref from-template led-32x8` for an LED matrix"
            )
        self._size = (width, height)
        self._mode = resolve_mode(self._display_mode, height)

        screen = self._build_screen(width, height, self._mode)
        pad.screen_save(screen)
        pad.screen_load(screen_path(self._screen_name))

    def _build_screen(self, width: int, height: int, mode: str) -> Any:
        """Build the one label screen for *mode* (the LED or the LCD layout)."""
        if mode == MODE_MULTILINE:
            return self._build_panel_screen(width, height)
        return self._build_scroll_screen(width, height)

    def _build_scroll_screen(self, width: int, height: int) -> Any:
        """One full-panel marquee — the LED-matrix layout (``ledout.md``)."""
        from touchy_pad.api import LongMode, Screen, absolute, label, rect, style

        screen = Screen(self._screen_name, layout=absolute())
        screen += label(
            LABEL_ID,
            text=STARTUP_TEXT,
            font_size=self._font_size or font_for(height),
            long_mode=LongMode.LONG_MODE_SCROLL_CIRCULAR,
            recolor=True,  # stage LB17: parse `#RRGGBB text#` runs
            scroll_speed_px_s=self._scroll_speed_px_s,  # stage LB19: half LVGL's 40 px/s
            rect=rect(0, 0, width, height),  # SCROLL_CIRCULAR needs a fixed box
            style=style(text_color=WHITE),
        )
        return screen

    def _build_panel_screen(self, width: int, height: int) -> Any:
        """One wrapped text field — the tall-panel layout (``lcdout.md``).

        ``LONG_MODE_DOTS`` wraps to the fixed width and replaces the last visible
        characters with dots when the text is taller than the box. ``WRAP`` would
        draw past the box, and the scroll modes disable wrapping entirely
        (``LV_TEXT_FLAG_EXPAND``), so ``DOTS`` is the one that behaves like a text
        field.
        """
        from touchy_pad.api import LongMode, Screen, TextAlign, absolute, label, rect, style

        inset = panel_inset(height)
        screen = Screen(self._screen_name, layout=absolute())
        screen += label(
            LABEL_ID,
            text=STARTUP_TEXT,
            font_size=self._font_size or font_for_panel(height),
            text_align=TextAlign.LEFT,
            long_mode=LongMode.LONG_MODE_DOTS,  # wrap, then dots on overflow
            recolor=True,  # the same `#RRGGBB text#` markup as the marquee
            rect=rect(inset, inset, width - 2 * inset, height - 2 * inset),
            style=style(text_color=WHITE),
        )
        return screen

    def _write(self, pad: Any, text: str, font: int | None = None) -> None:
        """One ``SetPropertiesCmd`` batch: the text, plus the font when *font* is given.

        A pad whose firmware predates the ``text_font`` override refuses the whole
        batch (``INVALID_ARG``). That is not a lost pad, so retry with the text
        alone — the quip still shows, at the screen's built font — and remember it
        for the rest of this connection rather than failing every quip.
        """
        from touchy_pad.api import build_property_override

        entries = [build_property_override(LABEL_ID, "text", text)]
        if font is not None and not self._font_unsupported:
            entries.append(build_property_override(LABEL_ID, FONT_PROPERTY, font))
        try:
            pad.set_properties(entries)
        except Exception as exc:
            if len(entries) == 1 or self._font_unsupported or not _is_refusal(exc):
                raise
            log.info(
                "dankbot: this pad's firmware has no %r override — showing the "
                "quip at the screen's own font",
                FONT_PROPERTY,
            )
            self._font_unsupported = True
            pad.set_properties(entries[:1])

    def _forget_pad(self) -> None:
        pad, self._pad = self._pad, None
        self._prepared = False
        self._font_unsupported = False  # a different pad may support it
        _safe_close(pad)

    # -- the idle clear ----------------------------------------------------

    def _arm_timer(self) -> None:
        self._cancel_timer()
        if self._clear_after > 0:
            self._timer = self._timer_factory(self._clear_after, self._clear)
            self._timer.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _clear(self) -> None:
        """Blank the marquee after a quiet spell — runs on the timer thread."""
        with self._lock:
            self._timer = None
            if self._pad is None:
                return
            try:
                self._write(self._pad, "")
            except Exception:
                log.debug("dankbot: idle clear failed", exc_info=True)
                self._forget_pad()
