"""Display out: show the quip on a touchy-pad (stage 3).

Two layouts, picked from the panel height (``docs/plans/lcdout.md``):

* a ``<= 32``-px-tall panel gets a full-panel **scrolling marquee** — the LED
  matrix layout (``docs/plans/ledout.md``);
* a taller panel gets one **multiline text field** that wraps to the panel width
  and ellipsizes when it runs out of vertical room — the LCD layout.

Both screens are built once per connection and saved; every quip afterwards is a
single ``SetPropertiesCmd`` batch — the label's ``text``, carrying LVGL
inline-recolour markup — never a screen re-upload.

This is the **only** module that imports ``touchy_pad``, so pulling voicebot out
to its own repo stays a one-file diff plus the PyPI switch — and the import is
*lazy* (inside the functions that need it) so the module can be imported and
unit-tested without the host library installed.

A pad is opened **lazily**, only when there is something to show, so a pad
plugged in later is picked up on the next message and a missing one is never
fatal. An idle timer blanks the display after ``clear_after`` seconds of quiet.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from threading import Lock, Timer
from typing import Any

from .commands import Run

log = logging.getLogger(__name__)

#: A display sink: handed one quip's colour runs (``Reply.runs``).
#: :meth:`PadDisplay.show` is the real one; ``None`` in a loop means "show nothing".
DisplaySink = Callable[[tuple[Run, ...]], None]

#: On-device screen + widget names — the contract between the saved screen and
#: the runtime property overrides. Deliberately *not* ``default`` so a later
#: ``touchy init`` / ``touchy screen demo`` cannot clobber it (or vice-versa).
SCREEN_NAME = "voicebot"
LABEL_ID = "voicebot_msg"

#: What the panel shows on connect, and the colour LVGL draws unmarked text in.
STARTUP_TEXT = "I'm voicebot"
WHITE = 0xFFFFFF

#: The label font sizes the firmware actually compiles (see the plan's Q6);
#: any other size silently falls back to the theme default.
FONT_SIZES = (8, 16, 30)

#: The two text layouts, plus ``auto`` — pick by panel height. ``auto`` is what
#: ``voicebot listen`` uses; the explicit modes are an escape hatch for a panel
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


def to_markup(runs: Sequence[Run], max_chars: int = MAX_OVERRIDE_CHARS) -> str:
    """Colour runs → LVGL inline-recolour text, truncated to *max_chars*.

    A non-white run becomes ``#RRGGBB text#`` — the space after the colour
    terminates the command and is not drawn, and the closing ``#`` reverts to the
    label's ``text_color`` — while a white run is emitted plainly. A literal
    ``#`` is dropped: it delimits a command and this LVGL version's ``##`` escape
    is dead code.

    *max_chars* bounds the **rendered** length because the device keeps a
    ``text`` override in a fixed buffer and rejects anything longer (see
    :data:`MAX_OVERRIDE_CHARS`). Truncation happens on a run boundary, so the
    markup always stays well-formed.
    """
    parts: list[str] = []
    used = 0
    for run in runs:
        text = run.text.replace("#", "")
        if not text:
            continue
        opening, closing = ("", "") if run.colour == WHITE else (f"#{run.colour:06X} ", "#")
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


def _safe_close(pad: Any) -> None:
    if pad is None:
        return
    try:
        pad.close()
    except Exception:
        log.debug("voicebot: closing the pad failed", exc_info=True)


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

        :func:`to_markup` keeps the string inside the device's buffer, so a
        refusal here means the link itself is broken rather than a bad payload.
        """
        try:
            self._write(pad, to_markup(runs))
            return True
        except Exception as exc:  # noqa: BLE001 — a lost pad is not fatal
            log.warning("voicebot: lost the touchy-pad (%s) — will reconnect", exc)
            return False

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
                log.warning("voicebot: found a touchy-pad but can't drive it (%s)", exc)
                self._forget_pad()
                return None
            self._prepared = True
            width, height = self._size
            info = getattr(self._pad, "board_info", None)
            board = getattr(info, "board_name", "") or "?"
            log.info(
                "voicebot: display ready on %dx%d (%s, %s, screen %r)",
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
                    log.debug(
                        "voicebot: connect attempt %d failed (%s); retrying", attempt + 1, exc
                    )
                    continue
                log.warning("voicebot: no touchy-pad to show this on (%s)", exc)
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

    def _write(self, pad: Any, text: str) -> None:
        """One ``text`` property override — the whole per-quip cost."""
        from touchy_pad.api import build_property_override

        pad.set_properties([build_property_override(LABEL_ID, "text", text)])

    def _forget_pad(self) -> None:
        pad, self._pad = self._pad, None
        self._prepared = False
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
                log.debug("voicebot: idle clear failed", exc_info=True)
                self._forget_pad()
