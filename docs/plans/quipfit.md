# dank-bot — plan: quip fit (step the font down instead of clipping the quip)

**Where this sits.** This extends the **multiline** layout from
[`lcdout.md`](lcdout.md) (Stage 3, *LCD out*). That plan built one wrapped text
field per panel with `LONG_MODE_DOTS`: it wraps to the panel width and
ellipsizes whatever does not fit vertically. On the attached `jc4827w543`
(480×272) a 30-px field holds roughly **170** characters — and the model
regularly writes twice that. This plan is `lcdout.md`'s deferred item *"font
shrinking per reply"* (and its risks R2/R4), made possible by a touchy-pad
feature that landed while the plan was being written.

> **Status: DONE (2026-11-18).** Built against touchy-pad **stage lb26**
> (upstream `main` then; released as 0.4.0): `SetPropertyCmd.font_size_value`
> plus the `text_font` runtime override. `display.py` fits the quip and sends the
> font in the **same** property batch as the text — no second screen, no screen
> re-upload, no new CLI flag. `just test` **250 passed / 2 skipped**;
> `just lint` clean. Verified on the attached `jc4827w543` (protocol 15): a
> 200-character quip goes out as `fontSizeValue: FONT_SIZE_16` and the device logs
> `font_size=16 on 'dankbot_msg'`; the next short quip goes back to
> `FONT_SIZE_30`. What remains is the human judgement — does 16 px read well
> across the desk?

## The problem

Observed on the real LCD (2026-11-18), a reply to the toaster persona:

```
DEBUG dankbot.display: rendering quip: #FF0000 tilts head# "Ah, the age-old question:
sodium or potassium? I theorize they're discussing the periodic table of elements, not
that boring white crystalline substance humans sprinkle on their grub."
DEBUG touchy_pad.api.client.rpc: rpc -> {"setProperties": {"props": [{"widgetId":
"dankbot_msg", "propertyName": "text", "stringValue": "<207 chars>"}]}}
```

207 characters of markup (~197 drawn) at 30 px in a 464×256 field is ~9 lines;
the field holds ~7, so `LONG_MODE_DOTS` ate the tail. The persona asks for <100
characters, but `DEFAULT_MAX_TOKENS = 120` allows ~400–500 *characters* and
models do not obey length guidance reliably — asking for shorter replies is not
a display fix.

## The knob: `text_font` (touchy-pad stage lb26)

The original plan ruled this out: `SetPropertyCmd` had no font value, and LVGL's
`text_font` style property is a `const lv_font_t *`, so an integer would be read
as a **font pointer**. touchy-pad lb26 fixed exactly that:

* **Wire** — `SetPropertyCmd.value` gained `font_size_value` (an int pixel size).
* **Host** — `build_property_override(id, "text_font", 16)` maps an int for this
  one property name to that arm and validates it against `FONT_SIZES`
  (`8, 14, 16, 30`); `FontSize` is the same list as an enum. A plain int is fine.
* **Device** — `widget_property.cpp::apply_font_size()` maps the pixel size with
  `font_for_size()` and calls `lv_obj_set_style_text_font(obj, font, 0)`; `0`
  drops the local font (back to the theme's 14 px). Any *other* value arm aimed
  at `text_font` is refused with a warning — the footgun is guarded, not just
  documented.
* It works on a label, a button and a span group — and a `recolor=True` label
  *is* a span group on the device.
* Overrides are **sticky and session-scoped**: they survive screen reloads and
  page swaps until the device reboots or the entry is removed.

**Floor.** Both sides need lb26. An older host library sends the entry as
`int_value`, which lb26 firmware refuses (the quip still shows, at full size);
pre-lb26 firmware would treat that int as a font pointer — which is why the
feature exists. The floor is therefore 0.4.0, the first release that carries
lb26; `AGENTS.md`, `pyproject.toml` and the `just use-touchy-pypi` comment say so.

## Goal / vision

```
  Reply.runs ──► visible_chars(runs) ──► font_for_text(w, h, chars)
                     │                            │
                     │                 30 px fits? ── yes ──► 30
                     │                            │ no
                     │                            ▼
                     │                          16 (598 chars on the LCD)
                     ▼                            │
        one SetPropertiesCmd batch:               ▼
          [ text = "#FF0000 tilts head# …",  text_font = 16 ]
```

One label, one widget id, one RPC per quip — the text *and* the font it needs.
Nothing is re-uploaded, and the scroll layout is untouched.

## Approach (as built)

### 1. A fit estimate from geometry (pure)

```python
GLYPH_ADVANCE_RATIO = 0.62   # em; real mixed-case text runs 0.50–0.55 — generous on purpose
LINE_HEIGHT_RATIO = 1.2      # LVGL's Montserrat line height

def chars_that_fit(width: int, height: int, font: int) -> int:
    inset = panel_inset(height)
    cols = max(1, int((width - 2 * inset) // (font * GLYPH_ADVANCE_RATIO)))
    rows = max(1, int((height - 2 * inset) // (font * LINE_HEIGHT_RATIO)))
    return cols * rows


def font_for_text(width, height, chars, *, ceiling=None) -> int:
    tiers = sorted(FONT_SIZES, reverse=True)          # 30, 16, 8
    if ceiling is not None:
        tiers = [size for size in tiers if size <= ceiling] or tiers
    for size in tiers:
        if chars_that_fit(width, height, size) >= chars:
            return size
    return tiers[-1]
```

The estimate is deliberately pessimistic: it only decides when to step *down*,
so a false "it fits" is a clipped quip while a false "it doesn't" costs a
smaller — but complete and still legible — quip. The tiers are far apart, which
makes a few percent of error harmless.

Capacity (pinned by tests):

| Panel | Inset | Field | Font | Capacity |
|---|---|---|---|---|
| 480×272 (the LCD) | 8 | 464×256 | **30** | 168 chars |
| 480×272 | 8 | 464×256 | **16** | 598 chars — more than `MAX_OVERRIDE_CHARS` (511) |
| 480×272 | 8 | 464×256 | **8** | 2418 chars (never needed) |
| 128×64 | 8 | 112×48 | 16 | 22 chars |
| 128×64 | 8 | 112×48 | 8 | 110 chars |
| 128×48 | 6 | 116×36 | 8 | 69 chars |

So on the LCD the **16-px tier always fits anything the protocol can carry**,
and the ellipsis disappears for the panel in hand. 14 px (the theme default) is
skipped as a tier: it is barely smaller than 16 and 16 reads better.

### 2. Count what is *drawn*, not the markup

`to_markup` wraps coloured runs in `#RRGGBB …#`, which draws no glyphs — a
coloured run costs 9 invisible characters. The fold that builds the markup was
factored into `_drawn_runs()`, which both `to_markup` and `visible_chars()` use,
so the length we budget for is exactly the text we send.

### 3. One batch per quip

```python
def _font_for_quip(self, runs) -> int:
    width, height = self._size
    if self._mode != MODE_MULTILINE:          # a marquee scrolls forever — never shrinks
        return self._font_size or font_for(height)
    return font_for_text(width, height, visible_chars(runs), ceiling=self._font_size)

def _write(self, pad, text, font=None):
    entries = [build_property_override(LABEL_ID, "text", text)]
    if font is not None:
        entries.append(build_property_override(LABEL_ID, FONT_PROPERTY, font))
    pad.set_properties(entries)               # one RPC
```

* `_write_quip` logs `rendering quip at 16px: …` so the choice is visible in
  `--debug`.
* The font is sent **every** quip, not only when it changes: overrides are
  sticky on the *device*, so a size left behind by an earlier dank-bot run would
  otherwise survive a restart. (The idle clear sends text only.)
* An explicit `PadDisplay(font_size=…)` becomes the ceiling, so a pinned size is
  never exceeded — but the quip may still step *below* it when it has to.
* Scroll mode sends the panel's one font (`font_for(height)`) with every quip
  too, which also neutralises a stale override left by another host.
* A pad whose firmware predates lb26 answers `INVALID_ARG` for the whole batch.
  That is a *refusal*, not a lost pad — `_is_refusal()` checks the wire code — so
  the quip is retried without the font entry (it still shows, at the screen's own
  font) and the connection remembers for the rest of the session. A transport
  failure still drops the handle and reconnects, exactly as before.

## Alternatives considered

| Option | Why not (or when) |
|---|---|
| **One saved screen per font tier**, switched with `screen_load` | The original plan, written before lb26. Correct but heavier: two screen files, a naming rule, and a screen rebuild to change a font. Rejected once a single property override could do it. |
| **Fixed 150-character threshold** | Matches the request literally but is panel-blind: an 800×480 panel would shrink quips that fit, a 128×48 one would still clip. The derived estimate costs the same code and adapts. Kept as the fallback if the estimate proves noisy. |
| **`transform_scale_x` / `transform_scale_y`** | Wire-legal int style props, but scaled text is resampled (soft at 16 px) and costs CPU per glyph on the ESP32-S3. Unnecessary now that a real font exists. |
| **Always 16 px in multiline mode** | Guarantees fit with no state at all, but throws away the 30-px text `lcdout.md` chose for the common short quip. |
| **Host-side pagination** | Still the follow-up for panels where even 8 px overflows (a postage-stamp LCD); it keeps the big font and shows everything, at the cost of page state and a timer. |
| **Add a font-size property to touchy-pad** | This is what happened (lb26). The plan's job became using it. |

## File & component layout (as built)

| File | Change |
|---|---|
| `src/dankbot/display.py` | `FONT_PROPERTY = "text_font"`; `GLYPH_ADVANCE_RATIO`, `LINE_HEIGHT_RATIO`; `chars_that_fit`, `font_for_text`, `visible_chars`, `_drawn_runs` (shared fold); `PadDisplay._font_for_quip`; `_write` gained the optional font entry. Still the only `touchy_pad` importer, and still lazy. |
| `tests/test_display.py` | `FakePad.texts()` filters on the value oneof; new `fonts()` / `entry_names()` helpers; pure fit tests + per-quip font tests. |
| `tests/test_display_sim.py` | Two sim tests: a long quip lands `font_size == 16` on the widget, a short one does not. |
| `README.md` | The display section explains the step-down and the ~170/500-character budgets. |
| `AGENTS.md`, `pyproject.toml`, `justfile` | The touchy-pad floor now names stage lb26 (`font_size_value`), not just 0.3.8. |
| `docs/plans/lcdout.md` | *Deferred* item, R2 and R4 point here. |

No new config, no new CLI flag, no new screen name.

## Phased implementation

| Phase | Deliverable | Status |
|---|---|---|
| **F1** | Pure policy: `chars_that_fit`, `font_for_text`, `visible_chars`, the two ratio constants. | **Done** — unit tests pin the capacity table, monotonicity, the ceiling, and that a size is always in `FONT_SIZES`. |
| **F2** | One batch: `_font_for_quip` + the `text_font` entry in `_write`; scroll mode unchanged. | **Done** — fake-pad tests (30 → 16 → 30, one batch, one screen) and sim tests. |
| **F3** | Docs: this plan, `lcdout.md` pointers, README, version-floor notes. | **Done.** |
| **F4** | Hardware pass on the real `jc4827w543` and an LED matrix. | **Done on the wire** — the 200-character quip is accepted as `FONT_SIZE_16` (device log: `font_size=16 on 'dankbot_msg'`), the short quip returns to 30 px, and the marquee path is untouched. Left: eyeball 16 px across the desk, and re-check on an LED matrix. |

## Testing strategy (as built)

Network-free and hardware-free; the fake pad is the duck-typed one in
`tests/test_display.py`, and the sim speaks the real wire protocol.

* **Pure** — `chars_that_fit` (168 / 598 / 22 / 69 for the panels above), strictly
  increasing as the font shrinks, `font_for_text` stepping 30 → 16 → 8 with the
  ceiling respected, `visible_chars` ignoring `#RRGGBB …#` and following the
  ASCII fold.
* **Fake pad** — a short quip keeps 30 px; 200 characters step to 16 px with the
  screen saved **once**; text and font arrive in **one** batch, in that order;
  the 32×8 marquee stays at 8 px for a 400-character quip; a long-then-short
  pair sends `[16, 30]` (the sticky-override guard); `font_size=16` is never
  exceeded; 160 drawn characters in coloured markup stay at 30 px even though
  the markup is 169 bytes; a pad that refuses `text_font` still shows the quip
  (the retry is text-only) and stops asking, while a transport failure with a
  font entry in the batch still drops the handle.
* **Simulator** — `test_a_long_quip_shrinks_the_font_on_the_lcd` (the widget's
  materialised style is 16) and `test_a_short_quip_keeps_the_full_size_font`.
  The sim now honours `text_font`, so this *does* prove the shrink end to end.
* **Hardware (F4)** — the only place legibility and the real glyph metrics can
  be judged.

## Risks / open questions

| # | Risk | Mitigation |
|---|---|---|
| Q1 | **The capacity estimate is wrong for real glyphs** (caps, `W`/`M`, long words). | 0.62 em is ~15 % generous and the tiers are 30 → 16, so an error must be large to matter. F4 measures it; tune the two constants — no structural change. |
| Q2 | **A sticky font outlives a restart** and a short quip inherits 16 px. | The font is sent with *every* quip (not just on change), so the first quip after a restart corrects it. The idle clear leaves the size alone (empty text fits everywhere). |
| Q3 | **Does `text_font` resolve on a `recolor` label?** (On the device it is a span group, and label-only properties like `long_mode` do not resolve on it.) | lb26 applies the font to the widget object and documents labels, buttons *and* span groups; the sim's materialised `font_size` style is asserted in `test_a_long_quip_shrinks_the_font_on_the_lcd`. F4 confirms on glass. |
| Q4 | **The floor moved**: a host library older than lb26 sends the entry as `int_value` (refused by new firmware, read as a pointer by old firmware). | A pre-lb26 firmware is handled: the refusal is retried without the font (`_is_refusal`), so the quip still shows at full size and the pad is not dropped. The firmware reports `0.3.12` with *or* without lb26, so a version check could not tell them apart anyway — the fallback is the guard. `AGENTS.md`, `pyproject.toml` and the `just use-touchy-pypi` comment record the requirement; 0.4.0 is the first release that carries it. |
| Q5 | **14 px is compiled but never chosen.** | Deliberate: it is 2 px smaller than 16 (a few more characters per line) and less legible. If F4 says 16 px overflows where 14 would fit, adding `14` to `FONT_SIZES` is a one-line change — the fit code iterates the tuple. |
| Q6 | **Tiny LCDs still overflow at 8 px.** | `font_for_text` returns the smallest tier and `DOTS` remains the visible cue; pagination is the recorded follow-up. Multiline on a postage-stamp panel is best-effort by design. |
| Q7 | **The real fix may be shorter replies** (`--max-tokens`, a stricter persona prompt). | Complementary, not a substitute: the display must cope with the 511 characters the protocol allows. Worth a separate TODO. |
| Q8 | **Long/short quips alternating flip 30 ↔ 16 px.** | Each flip is one int in the same batch — no extra RPC and no screen rebuild, so there is nothing to debounce. If F4 shows it reads as a flicker, a hysteresis band is a small addition. |

## Acceptance criteria (definition of done)

1. On the 480×272 `jc4827w543`, a ~200-character quip is drawn **in full** (no
   ellipsis) at 16 px; a short quip still draws at 30 px. *(F4)*
2. A quip still costs **one** `SetPropertiesCmd` batch and **zero** screen
   uploads; the batch carries `text` then `text_font`. *(tests)*
3. The LED-matrix path is unchanged: one `SCROLL_CIRCULAR` screen at
   `font_for(height)`, the marquee font sent with each quip, the existing proto
   test untouched. *(tests)*
4. The size is measured on the **drawn** text, so colour markup never shrinks a
   quip that fits. *(test)*
5. `chars_that_fit` / `font_for_text` / `visible_chars` are pure and tested;
   `just test` **250 passed / 2 skipped** and `just lint` clean. *(done)*
6. README/`lcdout.md` describe the step-down, and `AGENTS.md` / `pyproject.toml`
   record the lb26 floor. *(done)*

## Relationship to the other plans / docs

* [`lcdout.md`](lcdout.md) — the multiline layout this refines; its *Deferred*
  entry "font shrinking per reply" and risks R2/R4 point here.
* [`ledout.md`](ledout.md) — the marquee path, deliberately untouched (a
  scrolling label cannot overflow).
* [`initial.md`](initial.md), [`audioin.md`](audioin.md) — upstream stages;
  unaffected. The persona's "<100 characters" guidance stays: it helps, but this
  plan is what makes the display robust when the model ignores it.
