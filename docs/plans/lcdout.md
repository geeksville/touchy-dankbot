# touchy-dankbot — plan: LCD out (one display, two text layouts)

**Where this sits.** This extends `docs/plans/ledout.md` (Stage 3, *LED out*).
That plan built `display.py` around a **single** full-panel scrolling label —
the right shape for an 8- or 32-px-tall LED matrix, and a poor one for the LCD
pad the desk robot now has attached. This plan keeps that path byte-for-byte and
adds a second layout for *tall* panels, chosen automatically from
`pad.board_info`.

> **Status: DONE (2026-11-17).** Implemented L1–L5: `display.py` resolves the
> layout from `board_info`, builds either the marquee or the multiline field,
> re-prepares when the panel changes, and `--display-mode` /
> `DANKBOT_DISPLAY_MODE` overrides the rule. `just lint` clean;
> `just test` **121 passed / 2 skipped** (fake-pad tests for both modes plus a
> simulator run at 32×8 and 480×272).
>
> Verified on the attached LCD: `PadDisplay` reported
> `display ready on 480x272 (jc4827w543, multiline, ...)`, the firmware built the
> label with `text_align=1 long_mode=1 recolor=1` (DOTS), and a multi-colour quip
> landed via one `text` override. The LED path's existing 32×8 screen test is
> unchanged and green; a physical LED-matrix swap is still a manual check.
>
> Observed hardware (2026-11-17, `touchy board-info`):
> `jc4827w543`, display **480×272**, firmware `0.3.9.dev540+e84d522`,
> protocol 14, touchable, USB. The LED path was verified on the real 8×8 matrix
> (`docs/plans/ledout.md`, status block); 32×8 is the documented default panel.
> Both must keep working.
>
> **See also:** [`quipfit.md`](quipfit.md) — a long quip steps down to a smaller
> compiled font tier instead of being ellipsized; it supersedes the *Deferred*
> item "font shrinking per reply" below.

## Goal / vision

```
  [ dankbot listen ]
     mic → gate → responder ──► Reply { runs }            (stage 4a / 1+2)
                                      │
                                      ▼
                          PadDisplay.show(runs)          (stage 3)
                                      │
                ┌─────────────────────┴─────────────────────┐
                │ height ≤ 32 px                            │ height > 32 px
                ▼                                           ▼
       one-line marquee                            one multiline text field
       (LONG_MODE_SCROLL_CIRCULAR)                 (LONG_MODE_DOTS: wrap + …)
       LED matrix / strip                          LCD / OLED touchpad
```

One `PadDisplay`, one saved screen per panel, one `text` property override per
quip — on either display type. The only difference is the LVGL layout baked into
the screen the first time a panel is seen.

## What this plan covers

* A **layout policy**: `height <= 32` → scroll; `height > 32` → multiline.
* A **second screen builder** (`_build_panel_screen`) alongside the existing
  scrolling one (`_build_scroll_screen`), sharing `LABEL_ID`, `to_markup`,
  `recolor=True`, the startup banner and the idle clear.
* A per-connection re-check so swapping an LED pad for the LCD pad rebuilds the
  screen in the other mode.
* A `--display-mode {auto,scroll,multiline}` escape hatch
  (`DANKBOT_DISPLAY_MODE`) for the odd panel the height rule gets wrong.
* Tests for both geometries (fake pad + one simulator run per size) and a
  hardware pass on the attached `jc4827w543` **and** an LED matrix.
* Doc/persona wording that stops promising a scrolling LED.

## Non-goals

* **Vertical scrolling** as a first cut. touchy-pad has no scrollable container
  widget (`Widget.kind` is `button`/`label`/`slider`/`toggle`/`image`/`arc`/
  `spacer`/`checkbox`/`trackpad`/`log`/`image_button`/`fps`/`force_render`/
  layout/`widget_ref`), and LVGL's scroll long-modes deliberately **disable**
  wrapping (`LV_TEXT_FLAG_EXPAND`), so they cannot give a wrapped field that
  scrolls vertically. A tall panel gets wrapping + a bottom ellipsis instead
  (see *Overflow*); a pagination follow-up is sketched but **not** in this plan.
* Touch handling on the LCD (tap to dismiss, swipe, menus) — the display stays
  an output-only stage for now.
* Per-reply fonts, colours or animations beyond the existing `/color/NAME` runs.
* A new dependency, a second touchy-pad feature, or a firmware change. This plan
  is host-side only.

## Choosing the mode

### The rule

| Panel height | Mode | Label behaviour |
|---|---|---|
| `<= 32` px | **scroll** | `LONG_MODE_SCROLL_CIRCULAR`, whole-panel rect, `scroll_speed_px_s` |
| `> 32` px | **multiline** | `LONG_MODE_DOTS` with a fixed, inset rect: wrap to width, dots when the text is too tall |

Why 32 px: every LED chain template the firmware ships is one *row group* tall —
`led-32x8`, `led-64x8-chain`, `led-96x8-chain`, `led-32x16-chain`,
`led-32x32-chain` (32 rows max) — while LCD panels are 240–320 px tall
(`jc4827w543` = 272). 32 is the boundary the owner proposed and it separates the
two hardware families cleanly. A 32-row LED panel still scrolls; that is the
right default for a matrix, and `--display-mode` covers the exception.

The rule is **purely geometric**: no board-name allowlist, no magic strings.
`board_info.board_name` and `is_touchable` are logged for diagnosis, never used
to branch (a touchable LCD and a non-touchable LED panel both exist).

### Detection and overrides

```python
# display.py
MODE_AUTO = "auto"
MODE_SCROLL = "scroll"
MODE_MULTILINE = "multiline"
DISPLAY_MODES = (MODE_AUTO, MODE_SCROLL, MODE_MULTILINE)

PANEL_HEIGHT_THRESHOLD_PX = 32

def resolve_mode(requested: str, height: int) -> str:
    """The layout for a panel: an explicit choice, else the height rule."""
    if requested != MODE_AUTO:
        return requested
    return MODE_SCROLL if height <= PANEL_HEIGHT_THRESHOLD_PX else MODE_MULTILINE
```

* `PadDisplay(..., display_mode=MODE_AUTO)`; `--display-mode` is a click
  `Choice(DISPLAY_MODES)`, `envvar="DANKBOT_DISPLAY_MODE"`, default
  `DEFAULT_DISPLAY_MODE = MODE_AUTO` in `config.py` (the repo's flag → env →
  default convention).
* An unknown value is impossible through the CLI (click validates), but
  `resolve_mode` still treats anything unexpected as `auto` rather than raising —
  a bad env var must not stop a performance.
* Log the decision once per connection:
  `dankbot: display ready on 480x272 (jc4827w543, multiline, screen 'dankbot')`.

### Re-check on reconnect

`_prepare` runs on the first show after a connect. Today the result is cached in
`self._prepared` and `self._size`; this plan also caches `self._mode` and
re-prepares when a *new* connection reports a different `(width, height)` than
the cached one. That is the swap case:

1. `dankbot listen` starts with the LCD attached → multiline screen saved.
2. The LCD is unplugged; a write fails → `_forget_pad()` (existing behaviour).
3. An LED matrix is plugged in; the next quip reconnects, sees `32x8`, rebuilds
   and saves the scrolling screen, then writes the quip.

`_forget_pad` already clears `_prepared`; the size/mode comparison is the extra
guard for a reconnect that returns a *different* panel without an intervening
failure (two pads attached, a re-plug between shows).

## The two screen shapes

Both screens use `Screen(SCREEN_NAME, layout=absolute())`, the same `LABEL_ID`,
`style(text_color=WHITE)` and `recolor=True`.

### Scroll mode (unchanged)

```python
screen += label(
    LABEL_ID,
    text=STARTUP_TEXT,
    font_size=self._font_size or font_for(height),
    long_mode=LongMode.LONG_MODE_SCROLL_CIRCULAR,
    recolor=True,
    scroll_speed_px_s=self._scroll_speed_px_s,
    rect=rect(0, 0, width, height),        # SCROLL_CIRCULAR needs a fixed box
    style=style(text_color=WHITE),
)
```

No changes to constants, speed, font mapping or tests. A `--display-mode scroll`
on a tall panel deliberately reproduces today's LCD behaviour (a 30 px one-line
marquee) for anyone who prefers it.

### Multiline mode (new)

```python
inset = panel_inset(height)                # 8 px on a 272-px panel, less on tiny ones
screen += label(
    LABEL_ID,
    text=STARTUP_TEXT,
    font_size=self._font_size or font_for_panel(height),
    text_align=TextAlign.LEFT,             # a text field reads top-left
    long_mode=LongMode.LONG_MODE_DOTS,     # wrap + ellipsis on vertical overflow
    recolor=True,
    rect=rect(inset, inset, width - 2 * inset, height - 2 * inset),
    style=style(text_color=WHITE),
)
```

* **`LONG_MODE_DOTS`, not `WRAP`.** LVGL's own enum comments (`lv_label.h`):
  `WRAP` = "keep the object width, wrap lines longer than object width and
  **expand the object height**"; `DOT` = "keep the size and write dots at the
  end if the text is too long". With a fixed rect, `WRAP` can draw past the box;
  `DOTS` wraps to the width and, when the result is taller than the box, replaces
  the last visible characters with three literal `.` characters
  (`LV_LABEL_DOT_NUM` in `lv_label.c`), which is exactly "a multiline text field
  with wrapping".
* **No `scroll_speed_px_s`.** The scroll modes are the only ones it affects;
  passing it on a `DOTS` label would only add a confusing field to the proto.
* **`text_align=LEFT`.** `AUTO` follows LVGL's default and is left in practice,
  but an explicit value keeps the layout stable across firmware themes and shows
  up in the proto assertions.
* **Absolute layout + explicit rect.** Under `absolute()` the `grow_x`/`grow_y`
  hints are ignored, so the box is stated in pixels, exactly like the scroll
  path. The inset keeps glyphs off the bezel.

### Font and padding

```python
FONT_SIZES = (8, 16, 30)          # the sizes the firmware actually compiles
PANEL_PADDING_PX = 8
MIN_PANEL_LINES = 2

def panel_inset(height: int) -> int:
    """Breathing room around the text field, smaller on short panels."""
    return max(2, min(PANEL_PADDING_PX, height // 8))

def font_for_panel(height: int) -> int:
    """Largest compiled font that still leaves MIN_PANEL_LINES of text."""
    inner = height - 2 * panel_inset(height)
    fits = [size for size in FONT_SIZES if size * MIN_PANEL_LINES <= inner]
    return max(fits) if fits else FONT_SIZES[0]
```

| Panel | Inset | Inner height | Font | Lines at that font |
|---|---|---|---|---|
| 480×272 (the LCD) | 8 | 256 | **30** | ~8 |
| 128×64 | 8 | 48 | **16** | 3 |
| 128×48 | 6 | 36 | **16** | 2 |
| 480×33 (`multiline` forced) | 4 | 25 | **8** | 3 |

`font_for` (scroll mode) stays as it is: the largest compiled size that fits
the *whole* height, because a one-line marquee has no second line to protect.
Only fonts compiled into the firmware render; a size outside `FONT_SIZES`
silently falls back to the theme default (the `ledout.md` Q6 lesson).

The persona asks for <100 characters. At 30 px on a 464-px-wide field an average
quip is roughly 3–5 lines (~120–180 px), so it sits comfortably inside 256 px.
Typical replies need **no** scrolling at all — the owner's expectation.

### Overflow, and why there is no vertical scroll yet

The device cap is unchanged: `SetPropertyCmd.string_value` is a 512-byte nanopb
buffer, so `MAX_OVERRIDE_CHARS = 511` rendered characters (see `ledout.md`). A
long reply is truncated by `to_markup` before it is sent; if the remaining text
is still taller than the label box, LVGL ellipsizes it with `DOTS`. Nothing is
silently swallowed without a visual cue.

**Why not `LONG_MODE_SCROLL`?** LVGL *does* animate a label vertically when its
text is taller than the box — but `lv_label_set_long_mode` sets `expand = 1` for
`SCROLL`, `SCROLL_CIRCULAR` and `CLIP`, which becomes `LV_TEXT_FLAG_EXPAND`,
i.e. "one line, never wrap". A scroll-mode label is therefore a single line: it
scrolls sideways, it does not become a wrapped text field. `DOTS` is the only
mode that both wraps (`expand = 0`) and keeps a fixed box. Real vertical
scrolling needs the pagination fallback below.

If hardware testing shows real quips being cut, the follow-ups — in order of
preference — are:

1. **Smaller font in multiline mode** (16 px gives 16 lines on the LCD). Cheap,
   still static, still one property write per quip. Would be a constant change,
   not a protocol change, and is now planned per quip in
   [`quipfit.md`](quipfit.md).
2. **Host-side pagination**: split the rendered text into pages that fit, then
   rotate `text` overrides on a timer (the idle-clear timer machinery already
   shows how). This is "vertical scrolling" without a scrollable widget, at the
   cost of a timer and page state. Deferred until observed to be necessary.

## The per-quip path (unchanged)

```python
def show(runs: Sequence[Run]) -> None:
    with self._lock:
        pad = self._open()                  # lazily connects + prepares
        if pad is None:
            return
        if not self._write_quip(pad, runs):
            self._forget_pad()
            return
        self._arm_timer()

def _write_quip(self, pad, runs) -> bool:
    try:
        self._write(pad, to_markup(runs))   # one `text` override
        return True
    except Exception as exc:                # a lost pad is not fatal
        log.warning("dankbot: lost the touchy-pad (%s) — will reconnect", exc)
        return False
```

* `to_markup` — the `/color/NAME` runs to `#RRGGBB text#` LVGL markup, with the
  511-character cap — is **mode-independent**; both label layouts have
  `recolor=True`, so colour works on the LCD exactly as on the matrix.
* `show([])` / the idle clear writes `""`, which blanks whichever layout is
  active. No mode-specific clearing.
* The startup banner (`"I'm dankbot"`), lazy connect, retry-once, drop-the-pad
  and reconnect behaviour are all shared.
* The lock still serialises the reply worker and the idle-clear timer; nothing
  about a second mode adds concurrency.

## Code changes

| File | Change |
|---|---|
| `src/dankbot/display.py` | **the whole feature.** New constants (`MODE_*`, `DISPLAY_MODES`, `PANEL_HEIGHT_THRESHOLD_PX`, `PANEL_PADDING_PX`, `MIN_PANEL_LINES`), `resolve_mode()`, `panel_inset()`, `font_for_panel()`; split `_prepare` into `_prepare` (read size → resolve mode → dispatch) + `_build_scroll_screen` + `_build_panel_screen`; cache `self._mode`; re-prepare when the reported size/mode changes; richer "display ready" log line. Keep `font_for`, `to_markup`, `PadDisplay.show` and the timer as they are. |
| `src/dankbot/config.py` | `DEFAULT_DISPLAY_MODE = "auto"` (module-level string, not part of `Settings` — the display is CLI-only, like `--screen-name`). |
| `src/dankbot/cli.py` | `--display-mode` on `listen` (`click.Choice(["auto", "scroll", "multiline"])`, `envvar="DANKBOT_DISPLAY_MODE"`, `show_default=True`); pass it through `_make_display` → `PadDisplay(display_mode=...)`; help text states the height rule. |
| `src/dankbot/persona.py` | "Your words will appear on a scrolling LED display" → "Your words will appear on a small display (an LED marquee or a small LCD panel)." Keep the <100-character guidance: it is what keeps the multiline field to a few lines. |
| `README.md` | The display section: describe both layouts, the height rule, the `DOTS` ellipsis, `--display-mode`; add the flag to the options table; stop saying the marquee is the only output. |
| `docs/plans/ledout.md` | One "See also" line at the top pointing here; the LED-side content stays authoritative and unchanged. |
| `tests/test_display.py` | Layout-policy unit tests + one screen-shape test per mode + the pad-swap rebuild test (see *Testing strategy*). |
| `tests/test_display_sim.py` | A second sim fixture at `display_size=(480, 272)` proving the multiline screen loads and a `text` override lands. |
| `tests/test_cli.py` | `FakeDisplay.__init__` gains `display_mode=None`; a test asserts the flag reaches the display and that the default is `auto`. |

## CLI and config

```text
--display-mode [auto|scroll|multiline]   DANKBOT_DISPLAY_MODE   auto
    auto       pick by panel height: <= 32 px scrolls, taller wraps
    scroll     force the one-line marquee (any height)
    multiline  force the wrapped text field (any height)
```

Flag → env → default, like every other option. `--screen-name` still names the
single saved screen (`F:host/s/dankbot.pb`); the mode is a property of *that
panel's* build, not of the name, so swapping pads re-saves the same screen with
the other layout.

## Phased implementation

Each phase is independently verifiable and leaves `just test` / `just lint`
green. L1–L3 need no hardware; L4 is docs, and L5 is the first real-panel check.

| Phase | Deliverable | Verified by |
|---|---|---|
| **L1** | Pure policy in `display.py`: `MODE_*`, `resolve_mode`, `panel_inset`, `font_for_panel`, `DEFAULT_DISPLAY_MODE`. No behaviour change yet. | `just test` — parametrised unit tests (heights 8/16/32/33/48/64/272; explicit overrides win). |
| **L2** | Screen split + dispatch: `_build_scroll_screen` (moved verbatim) and `_build_panel_screen`; `_prepare` records `self._mode`; reconnect re-prepares on a size change. The LED path must stay byte-identical. | `just test` — existing scrolling assertions unchanged; new proto assertions for a 480×272 fake pad (DOTS, LEFT, inset rect, font 30); pad-swap test. |
| **L3** | Wiring: `--display-mode` + env, `_make_display(..., display_mode)`, updated `FakeDisplay`, log line. | `just test` (CLI flag forwarding + default), `just lint`. |
| **L4** | Docs/persona/README + the `ledout.md` cross-link. | Review; `just lint`; README quickstart reads correctly. |
| **L5** | Hardware + simulator verification. | `TOUCHY_SIM_URL` run at 480×272 and 32×8; on the real `jc4827w543`: `dankbot listen --fake sim-overheard.txt` then a live mic run; repeat on an LED matrix. Check colour, wrapping, ellipsis, idle clear and hot-swap. |

**Definition of done for the stage:** with the LCD attached, a multi-colour quip
appears as a wrapped text block; with the LED matrix attached, the same quip
still scrolls; neither needs a flag, and a runtime swap between them recovers on
the next quip.

## Decisions

| # | Decision | Consequence |
|---|---|---|
| 1 | **The mode is chosen from panel height, not board name or touch support.** `<= 32` scrolls, `> 32` wraps. | The owner's rule; works for unknown future panels; no allowlist to maintain. `board_name` / `is_touchable` are logged only. |
| 2 | **`auto` is the default, with `--display-mode` as an override.** | Zero-config for both current pads; a 64×64 LED matrix or a weird 32-px LCD can be forced either way without a code change. |
| 3 | **Multiline mode uses `LONG_MODE_DOTS`, not `WRAP`.** | Wrapping is preserved, but vertical overflow becomes a visible ellipsis instead of text drawn outside the bezel. |
| 4 | **No vertical scrolling in this plan.** | touchy-pad has no scrollable container and a label only scrolls horizontally. The owner's expectation — typical quips fit at the default font — makes this safe; the two fallbacks are recorded under *Overflow*. |
| 5 | **Two font maps.** `font_for(height)` for the marquee (largest that fits) and `font_for_panel(height)` for the text field (largest that leaves ≥2 lines). | The LCD gets 30 px; small LCDs drop to 16 px rather than rendering one clipped line. The LED path's mapping is untouched. |
| 6 | **Same screen name, same label id, same `text` override.** | The on-device contract (`LABEL_ID`, `to_markup`, 511-char cap) is unchanged; the mode is just how the screen was built. |
| 7 | **Re-prepare when the panel's size changes.** | Plugging the other display type in mid-run recovers on the next quip, which is the normal desk-robot workflow. |
| 8 | **Top-left text, inset from the bezel.** | LVGL's proto exposes horizontal `text_align` but no vertical alignment/justification; a text field reading from the top-left is the natural shape and needs no host-side measurement. |

## Deferred (explicitly not in this plan)

* **Vertical scrolling / pagination** — the fallback if real quips overflow (see
  *Overflow*).
* **A compact header/status layout** for the LCD (e.g. a `HEARD` line + quip), or
  reusing the spare space for an FPS/log widget. The pad is a quip display today.
* **Touch gestures** — tap to dismiss, swipe for history. The LCD reports
  touches; wiring them is a separate stage.
* **Font shrinking per reply** to make every quip fit — now done in
  [`quipfit.md`](quipfit.md): the quip's drawn length picks a compiled font and
  a `text_font` property override steps the label down.
* **Using `is_touchable` as a second signal** for mode selection. Height is the
  rule; touch is context for logs and for the deferred gesture work.

## Testing strategy

Everything is network-free and hardware-free by default, like the rest of
dankbot.

* **`tests/test_display.py`**
  * `resolve_mode` table: `(8, auto) → scroll`, `(16, auto) → scroll`,
    `(32, auto) → scroll`, `(33, auto) → multiline`, `(64, auto) → multiline`,
    `(272, auto) → multiline`; `(272, scroll) → scroll`, `(8, multiline) →
    multiline`, `(272, "bogus") → multiline` (unknown treated as auto).
  * `panel_inset` / `font_for_panel`: the table above (272→8/30, 64→8/16,
    48→6/16, 33→4/8); font always in `FONT_SIZES`.
  * **Scroll screen test unchanged** (32×8 fake pad): `SCROLL_CIRCULAR`,
    `scroll_speed_px_s`, full-panel rect, font 8 — the regression guard.
  * **New multiline screen test** (480×272 fake pad): `DOTS`, `text_align=LEFT`,
    rect `(8, 8, 464, 256)`, font 30, `recolor is True`, no scroll speed;
    startup banner text present.
  * `--display-mode` override at the `PadDisplay` level (force scroll on 480×272,
    force multiline on 32×8).
  * **Pad swap**: show on a `FakePad(32, 8)`, drop the handle as if unplugged,
    attach a `FakePad(480, 272)`, show again → two screens saved, the second one
    multiline (and vice versa).
  * Existing markup, connect-retry, lost-pad, headless-0×0 and idle-clear tests
    stay green.
* **`tests/test_display_sim.py`** — the existing 32×8 fixture plus a 480×272 one:
  after `show([Run("Danger!", red)])` the sim's label has the marked-up text, and
  the active screen's label is not `SCROLL_CIRCULAR`. Uses
  `SimServer(display_size=(480, 272))`; no hardware.
* **`tests/test_cli.py`** — `--display-mode multiline` reaches
  `FakeDisplay.display_mode`; omitted → `auto`; `--display-mode nope` exits
  non-zero (click `Choice`).
* **`just lint`** — ruff format + check.
* **Manual, on hardware (L5)** — with the real `jc4827w543`:
  `dankbot listen --fake sim-overheard.txt` (colour + wrapping + idle clear),
  then a live mic run; then repeat with an LED matrix attached, including
  swapping one for the other while `listen` is running. The simulator is
  advisory (it mirrors `long_mode` only as a word-wrap toggle), so the panel is
  the source of truth for glyph fitting.

## Risks / open questions

| # | Risk | Mitigation |
|---|---|---|
| R1 | **32 px is the wrong boundary for some panel** (e.g. a 64×64 LED matrix that should marquee, or a 32-px-tall LCD that should wrap). | `--display-mode` forces either layout; the rule stays a one-line constant (`PANEL_HEIGHT_THRESHOLD_PX`) if it needs tuning. |
| R2 | **`DOTS` ellipsizes a quip that would have fitted with a smaller font.** | Addressed by [`quipfit.md`](quipfit.md): the quip's drawn length picks a smaller compiled font, sent as a `text_font` override in the same batch as the text. |
| R3 | **The device's `WRAP`/`DOTS` rendering differs from LVGL's upstream comment** (firmware may clamp the label box differently). | L5 checks on hardware; the proto round-trip is pinned by tests regardless. If `DOTS` misbehaves, the fallback is `WRAP` (wraps, then hard-clips at the fixed box) — **not** `CLIP`, which sets `expand = 1` and so does not wrap at all. |
| R4 | **Vertical overflow is silently cut on a very long model reply.** | The persona + `MAX_OVERRIDE_CHARS` bound length; `DOTS` shows a cue. [`quipfit.md`](quipfit.md) steps down a font tier so the common long quip fits outright; pagination remains the follow-up for tiny panels. |
| R5 | **A pad swap is not noticed** because the same `Touchy` handle keeps reporting the old size. | A real swap means a new USB open (or a new process); the size re-check covers the lazy-reconnect path. If a stale handle is ever seen, `_forget_pad` on any write failure already covers it. |
| R6 | **Two pads attached** — `touchy_open()` picks the first, so the mode follows whichever it picks. | Existing limitation (`ledout.md` R5); one pad at a time is the supported setup. |
| R7 | **The screen name is shared between modes**, so a stale `dankbot.pb` built for the other panel exists until the next `_prepare`. | `_prepare` always saves before loading, so the active screen is always freshly built for the panel in hand. |
| R8 | **The sim doesn't render glyph fitting**, so tests can pass while the real LCD ellipsizes. | Sim tests assert the proto (mode, rect, alignment), hardware checks the pixels; that split is deliberate. |

## Acceptance criteria (definition of done)

1. With no flags on the attached 480×272 `jc4827w543`, `dankbot listen` shows
   the startup banner and each quip as a **left-aligned, wrapped, multi-colour
   text block**, and the idle clear blanks it.
2. With no flags on an 8-/32-px-tall LED matrix, the same run behaves exactly as
   today: one `SCROLL_CIRCULAR` marquee at `DEFAULT_SCROLL_SPEED_PX_S`.
3. `--display-mode scroll` forces the marquee on the LCD; `--display-mode
   multiline` forces wrapping on the matrix; `DANKBOT_DISPLAY_MODE` does the
   same via the environment.
4. A quip is still **one** `text` property override; the screen is saved once per
   connection and re-saved only when the panel size/mode changes.
5. Unplugging one panel and plugging in the other recovers on the next quip,
   logging the new size and mode.
6. `just test` (network-free) and `just lint` are green; there is one fake-pad
   screen assertion per mode and one sim run per geometry.
7. The README, persona and flags describe both layouts; `ledout.md` links here.

## Relationship to the other plans / docs

* `docs/plans/ledout.md` — the original LED-out stage; this plan extends its
  `display.py`/`PadDisplay` design and leaves every LED-side decision intact.
* `docs/plans/audioin.md`, `docs/plans/initial.md` — the stages feeding this one;
  unaffected.
* `docs/hardware.md` — add the LCD board (`jc4827w543`, 480×272) to the display
  notes when L4 lands.
* `README.md` → *The display (on the touchy-pad)* — the user-facing summary of
  the two layouts.