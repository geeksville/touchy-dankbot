---
name: touchy-pad
description: >-
  Drive Touchy-Pad hardware from a host program: ESP32 LCD touchpads,
  StreamDeck-style macro keypads, LED-matrix and signage panels. Use when the
  user asks to add Touchy-Pad (the `touchy_pad` Python API, the Rust crate, or
  the `touchy` CLI), scroll text on an LED matrix, build a key/macro pad or
  touchpad gestures, or talk to a pad over USB / HTTP / JSON — not for generic
  LVGL, gpiozero, or other LED libraries.
---

# Touchy-Pad

The pad is a **dumb renderer**: your host program authors a protobuf widget tree
(LVGL objects), uploads it over USB / UART / WiFi-HTTP, and the device draws it
and reports touches back. The host does the thinking — the pad has no internet
access and no knowledge of your data.

This skill is project-agnostic: copy this directory into any project's
`.agents/skills/` to reuse it.

## First: fetch the live hints

Do not trust model memory for this SDK: its wire versions and API move. Before
writing code, fetch the project's agent cheat-sheet:

```sh
curl -sS https://raw.githubusercontent.com/geeksville/touchy-pad/refs/heads/main/llms.txt
```

(Or fetch that URL with your web tool.) It is the authoritative digest —
install, mental model, DSL, layout rules, actions, simulator, gotchas — and it
links deeper docs. This file is orientation plus the recipes that come up most;
go back to `llms.txt` when a detail matters.

Deeper docs are plain files under the same base URL,
`https://raw.githubusercontent.com/geeksville/touchy-pad/main/`:

| File | Read it for |
|---|---|
| `docs/python-api.md` | full Python API: screen DSL, scroll speed, macros, events, property overrides |
| `docs/rust-api.md` | the async Rust crate |
| `docs/network-api.md` | HTTP/HTTPS + JSON endpoint, mTLS, WiFi provisioning |
| `docs/simulator.md` | simulator modes |
| `docs/host-api.md` | wire protocol, framing, endpoints, filesystem paths |
| `docs/hardware.md`, `docs/installing.md` | boards, flashing |
| `docs/ui.md`, `docs/user-widgets.md` | layout / UI design intent |
| `docs/design.md` | why things are the way they are |
| `app/examples/demo.py` | a fully commented worked example |
| `AGENTS.md`, `proto/` | contributing to touchy-pad itself; the wire schema |

## Install and connect

```sh
pip install touchy-pad          # host library + the `touchy` CLI
pip install 'touchy-pad[sim]'   # + PySide6 window for the simulator
```

Two supported host APIs, both consumed as **published packages** — do not clone
the touchy-pad repo unless you are changing its firmware, wire protocol or the
libraries themselves:

- Python: `touchy_pad.api` (every authoring name is re-exported there).
- Rust: the `touchy-pad` crate.

`touchy_pad._proto`, `touchy_pad.sim`, `touchy_pad.cli` and the generated
`_proto/*` modules are internal. In a Poetry project pin it normally, e.g.
`touchy-pad = ">=0.3.8"`.

```python
from touchy_pad.api import touchy_open, touchy_get_pad_ids

print("attached:", touchy_get_pad_ids())     # stable serials, e.g. ["t0a1b2c3d4e5f"]

with touchy_open() as pad:                   # serial="..." to pick one of several
    info = pad.board_info
    print(info.board_name, info.display_width, info.display_height,
          "touch:", info.is_touchable, "multitouch:", info.is_multitouch,
          "serial:", info.serial)
```

Resolution order for `touchy_open(serial=None, *, url=None)`: `url=` /
`TOUCHY_URL` (HTTP(S)) → native USB → `uart:<path>` → `TOUCHY_SIM_URL` →
in-process simulator. It raises `IncompatibleFirmwareError` if the pad's
firmware is too old. `Touchy` is a context manager; leaving the `with` stops the
poller thread and closes the transport. **Re-open after re-plugging** — libusb
caches its device list, so a long-lived process may not see a new pad on a stale
handle.

Read the panel size from `pad.board_info`; never hard-code it.
`display_width == 0` means the panel has not been provisioned yet (normal on a
fresh LED board).

## Quickstart — one complete program

```python
from touchy_pad.api import (
    Layer, grid, cell, label, button, style,
    host_action, macro_action, macros, hid_keys, protobuf, touchy_open,
)

def build_page() -> protobuf.Widget:
    page = Layer(layout=grid(cols=2, rows=2, gap=8))
    page += cell(
        label("title", text="Status panel", font_size=24,
              style=style(text_color=0xFFFFFF)),
        col=0, row=0, col_span=2, grow_x=1,
    )
    # Runs on the *device*: a real USB HID keystroke, no host needed.
    page += cell(
        button("copy", text="Ctrl+C",
               on_click=macro_action([macros.key_tap(hid_keys.KEY_C, hid_keys.CTRL)])),
        col=0, row=1, grow_x=1,
    )
    # Runs in *this* process: the device sends an LvEvent back to us.
    page += cell(
        button("ping", text="Ping host",
               on_click=host_action(on_event=lambda e: print("tapped", e.user_data))),
        col=1, row=1, grow_x=1,
    )

    widget = protobuf.Widget()      # Layer -> Widget: the unit the API uploads
    page.copy_into(widget)
    return widget

with touchy_open() as pad:
    pad.user_screen_save("panel", build_page())   # -> F:host/uscr/panel.pb
    pad.show_user_screen("panel")                 # front it, keep the chrome
    input("tapping… press enter to quit\n")
```

`host_action(on_event=...)` callbacks auto-register when the screen/page that
references them is uploaded. They run on the poller thread — keep them short and
lock shared state.

## Mental model

- **You author screens; the device stores them.** `pad.screen_save(screen)`
  writes `F:host/s/<name>.pb`; `pad.screen_load(path)` activates one.
- **The stock chrome + a user page is the usual path.** `F:host/s/default.pb`
  is a prev/next bar plus a body `widget_ref(id="page")`;
  `pad.user_screen_save(name, widget)` then `pad.show_user_screen(name)` points
  that ref at your UI without replacing the chrome.
- **Behaviour lives on the device.** `macro_action([...])` compiles to an
  on-device HID sequence that works with no host attached; `host_action(...)`
  sends an event back to your process. Both are just `Action` lists on a widget.
- **Paths are drive-prefixed:** `F:` persistent LittleFS, `R:` volatile PSRAM
  ramdisk, `T:` transient (PSRAM, or flash scratch on no-PSRAM boards). A bare
  `host/...` is wrong. Screens `F:host/s/`, user pages `F:host/uscr/`,
  standalone widgets `F:host/w/`.

## DSL cheat sheet

```python
from touchy_pad.api import *       # every authoring name below
from touchy_pad.api import macros  # a submodule — import it explicitly
```

| Area | Names |
|---|---|
| Layout | `absolute()`, `flex(flow, gap)`, `row(gap)`, `col(gap)`, `grid(cols, rows=0, gap=0)`, `cell(w, col, row, col_span=1, row_span=1, *, grow_x, grow_y)`, `rect(x, y, w, h)`, `grow(w, x=, y=)` |
| Widgets | `button(...)`, `label(...)`, `slider(...)`, `toggle(...)`, `checkbox(...)`, `image(...)`, `image_button(...)`, `arc(...)`, `spacer(id)`, `widget_ref(path, *, id)`, `trackpad(...)`, `log_line(id)`, `fps(id)`, `force_render(id)` |
| Styling | `style(bg_color, radius, border_w, pad, text_color, font_size, recolor, …)` — colours are `0xRRGGBB` ints; `for_state=` takes an `LvState` bitmask |
| Animations | `animation(*anim_track(...), duration_ms, path, repeat_count, reverse, …)` and `anim_track(prop, start, end, *, start_inverted, end_inverted)` live in `touchy_pad.api.screens`; `prop` is a `StyleProp` member |
| Actions | `host_action(code=None, *, on_event=cb)`, `macro_action(steps)`, `change_widget_ref_action(id, path)`, `next_widget_action(id, dir)`, `set_image_button_slot_action(id, pressed, path)` |
| Upload | `Screen(name, layout=, widgets=, *, top=, sys=, bottom=)`, `Layer(layout=, widgets=)`, `build_default_screen()`, `build_setup_screen_touchless()` |
| Lifecycle | `touchy_open`, `Touchy`, `TouchyClient`, `ImageCache`, `ImageSource`, `Color`, `Point`, `build_property_override`, `protobuf` |

`Touchy` methods: `screen_save`, `screen_load`, `user_screen_save`,
`widget_save`, `show_user_screen`, `file_save`, `file_delete`, `file_reset`,
`set_properties`, `set_image_button_slot`, `on_host_event`, `board_info`,
`client`. `TouchyClient` adds `screen_wake()`, `run_actions()`,
`set_backlight_level(0..100)`, `screen_sleep_timeout(ms)`, `get_preferences()`,
`set_preferences()`, `stream_events()` / `event_consume()`.

## Layout — the rules that bite

- **Everything is content-sized by default.** A button shrinks to its text; opt
  in to stretching with `grow_x` / `grow_y` (the #1 "why is it tiny?").
- Flex main axis is a *proportional* grow factor (`2` grows twice as fast as
  `1`); flex cross axis and grid treat any value `> 0` as "fill".
- Grid children centre unless they grow; a `grow_x=1` spacer between two buttons
  pushes them to opposite edges.
- `rect(...)` only bites under `absolute()` (or as a fixed size hint); `w`/`h`
  of `0` mean "size to content".
- **A scrolling label needs a fixed, narrower box** or `long_mode` never
  animates.
- To layer widgets, use a `1x1` grid and put several in the same `cell(...)` —
  touch goes to the last-drawn (topmost) object. `Screen` also has `add_top()` /
  `add_sys()` / `add_bottom()` for persistent layers.

## Live updates without re-uploading the screen

```python
from touchy_pad.api import Color, build_property_override

with touchy_open() as pad:
    pad.set_properties([
        build_property_override("welcome", "text", "New message"),
        build_property_override("box", "bg_color", Color(0xFF8800)),
        build_property_override("box", "x", 12),
        build_property_override("ticker", "anim_duration", 1000),
        build_property_override("welcome", "text", None),   # remove override
    ])
```

- `prop` is an LVGL property name (`"text"`, `"bg_color"`, `"x"`, `"w"`,
  `"opa"`, `"anim_duration"`, …). The value's Python type selects the wire
  payload; `Color` / `Point` wrappers are required — a bare `int` is always an
  integer prop. `None` removes the override.
- Overrides are **session-scoped (RAM only) and sticky**: they survive widget
  rebuilds and are accepted before the widget exists.
- One batch is one atomic RPC, repaints immediately, and counts as display
  activity — it wakes / keeps awake the backlight, so a few-Hz animation loop
  needs no explicit `screen_wake()`.
- The simulator only honours `x`/`y`/`w`/`h`, `bg_color`, `text`.

**Marquee / ticker recipe** (the most common "scroll a line" ask):

```python
from touchy_pad.api import LongMode, Screen, absolute, label, rect, style

w, h = pad.board_info.display_width, pad.board_info.display_height
screen = Screen("ticker", layout=absolute())
screen += label(
    "msg",
    text="I'm alive",
    rect=rect(0, 0, w, h),                    # fixed box: required for scrolling
    font_size=8,                              # only compiled sizes render as intended
    long_mode=LongMode.LONG_MODE_SCROLL_CIRCULAR,
    recolor=True,                             # parse #RRGGBB runs
    scroll_speed_px_s=30,                     # 10..10000, LVGL default is 40
    style=style(text_color=0xFFFFFF),
)
pad.screen_save(screen)
pad.screen_load("F:host/s/ticker.pb")
pad.set_properties([build_property_override("msg", "text", "New quip")])
```

- `scroll_speed_px_s` and `scroll_duration_ms` are mutually exclusive;
  `anim_duration` re-times it live.
- With `recolor=True`, LVGL parses `#RRGGBB text#` runs — and then a literal `#`
  in the text is swallowed. The `##` escape is dead code in this LVGL build:
  strip or replace `#` host-side.

## LED-matrix (touch-less) panels

LED boards (`esp32_s3_matrix`, `esp32_s3_devkitc_1`) have no touch panel, and a
fresh one reports `display_width == 0` and renders nothing. Configure it once
from the host:

```sh
touchy pref from-template led-32x8         # 32x8 WS2812B matrix on GPIO 4
touchy pref from-template led-96x8-chain   # 3 x 32x8 daisy-chained, tiled
touchy pref backlight-level 40             # LED boards default to 10 %
touchy board-info                          # now reports the real size
```

- The same widget tree renders (LVGL → pixel strip); lay out with `absolute()` +
  `rect`, or flex/grid + `grow_*`, against the size from `board_info`.
- Use `start_inverted` / `end_inverted` on `anim_track` so motion spans "the
  whole panel" on any geometry instead of a baked-in pixel value.
- `is_touchable` is `False`, so `touchy init` writes a touch-less setup screen
  with **no** `widget_ref(id="page")` — `show_user_screen()` cannot target it.
  Upload and `screen_load()` your own screen instead.
- `log_line()`, `fps()` and `force_render()` are handy diagnostics on a matrix.
- If `board_info.temp_is_flash` (`T:` is flash-backed, no PSRAM), throttle
  high-rate image rewrites.

## Images

```python
from touchy_pad.api import ImageCache

# PNG/JPEG/BMP/GIF/WebP are converted to LVGL .bin host-side; .gif stays animated.
pad.file_save("F:host/images/logo.png", png_bytes, max_width=128, max_height=128)
# -> image("logo", asset="F:host/images/logo.bin")

cache = ImageCache(pad, max_dim=72)         # one per device, not thread-safe
path = cache.set_cached_image(icon_png)     # T:host/icache/<hash>.bin, uploaded once
pad.set_image_button_slot("key_0", pressed=False, path=path)   # repaint in place
```

- `ImageCache` is content-addressed (xxh3-128), LRU-evicted after 128 entries;
  build a fresh one per attach (its first call wipes the cache root). This is
  the StreamDeck repaint path.
- `ImageSource(callable, every=0.2)` is a *live* asset on a stable
  `T:dyn/<n>.bin` path: `src.update()` rewrites the file, which *is* the
  repaint. `image()` / `image_button()` also accept a bare `PIL.Image` or
  `bytes` (auto-wrapped).
- Prefer `R:`/`T:` for assets you rewrite often (flash wear); `F:` for things
  that must survive reboot.

## Actions, events and gestures

```python
button("paste", text="Paste",
       on_click=macro_action([macros.key_tap(hid_keys.KEY_V, hid_keys.CTRL)]))
button("ping", text="Ping", on_click=host_action(on_event=cb))
pad.client.run_actions([...])       # fire any Action list from the host now
```

- `host_action(on_event=...)` allocates a code ≥ `0x10000` and registers the
  callable on upload. With an explicit code (keep it **below** `0x10000`) you
  must wire it yourself: `pad.on_host_event(code, cb)`.
- `LvEvent`: `user_data` (widget id), `value` (sliders), `checked`
  (toggles/boxes), `host_code`, and `code` (`1`=PRESSED, `8`=RELEASED,
  `7`=CLICKED).
- Macros: `key_tap` / `key_down` / `key_up`, `type_text`, `mouse_click`,
  `mouse_move` (bare = live trackpad delta), `scroll_move`, `zoom_move`,
  `mouse_button_down/up`, `set_delay`, `consumer_key(VOLUME_UP | VOLUME_DOWN |
  MUTE | PLAY_PAUSE)`; keys live in `hid_keys` (`KEY_*`, `MOD_*`, and the
  `CTRL`/`SHIFT`/`ALT` aliases).
- `trackpad(id, ...)` defaults to a normal touchpad (1/2/3-finger clicks, drag,
  2-finger scroll). Every extra gesture family is **opt-in via field presence**
  (`swipe_initial_distance=`, `zoom_initial_distance=`, `twist_initial_angle=`,
  `hold_time=`) and additive; pass `[]` to disable one. Put a `log_line()` on
  the page to watch recognised gestures while tuning thresholds.

## From any language: HTTP + JSON

A WiFi-provisioned pad takes the same commands on one endpoint, as a bare
protobuf message or canonical protobuf-JSON (no framing — HTTP delimits it):

```sh
touchy pref wifi-set-ssid MyNetwork && touchy pref wifi-set-psk hunter2
curl -s -H 'Content-Type: application/json' \
  -d '{"setProperties":{"props":[{"widgetId":"welcome","propertyName":"text","stringValue":"hi"}]}}' \
  http://192.168.1.42/touchy/api/v1/command
```

`POST /touchy/api/v1/command`, `Content-Type: application/protobuf` (default) or
`application/json`; the response mirrors the request encoding. JSON covers
`setProperties`, `sysBoardInfoGet`, `screenWake`, `getPreferences`,
`sysRebootBootloader`, `eventConsume`; `setPreferences` and `runActions` are
protobuf-only, and an unknown key returns HTTP 400. Events are polled. From
Python just use `touchy_open(url="http://host[:port]")` or set `TOUCHY_URL`;
`https://` picks up the mTLS credentials saved by `touchy pref provision-mtls`.

## CLI cheat sheet

```sh
touchy board-info | init | update
touchy screen load F:host/s/home.pb | wake | demo
touchy file-save PATH FILE | file-delete PATH | file-reset
touchy events                             # stream touches (Ctrl-C)
touchy property set WIDGET_ID PROPERTY VALUE [--int]
touchy pref backlight-level 0-100 | log-level | boot-delay | backlight-timeout
touchy pref from-template NAME | json-get | json-set | wifi-set-ssid | provision-mtls
touchy simulator [--headless] [--port N]
```

## Develop and test with no hardware

The simulator speaks the real wire protocol over TCP, so the same host code runs
against it:

```sh
touchy --sim-headless board-info          # script/CI friendly, no Qt
touchy --sim-gui screen demo              # in-process sim + window
touchy simulator --headless --port 8935   # long-lived server
export TOUCHY_SIM_URL=tcp://127.0.0.1:8935   # Python *and* Rust pick this up
```

```python
from touchy_pad.api import create_sim_device, destroy_sim_device, touchy_open

create_sim_device()
with touchy_open() as pad:
    pad.screen_save(my_screen)
    pad.screen_load("F:host/s/my_screen.pb")
destroy_sim_device()
```

Fidelity limits: the simulator implements only the `set_properties` subset
`x`/`y`/`w`/`h`/`bg_color`/`text`, has no backlight or auto-off, no
refresh-latency model and no `IO_ERROR` model, and it does **not** emulate a
physical LED matrix. Use it for layout and logic; check panel wiring on real
hardware. Keep `touchy_pad` imports lazy in library code so importing your
package never needs the native USB stack (and CI needs no device).

## Gotchas

- **Wire versions are validated on load, and a mismatch deletes the file.**
  After a firmware update that bumps the root `Widget` version, re-upload your
  screens (`screen_save` / `user_screen_save`). At the time of writing
  `Widget.Version.CURRENT == 30`, `ProtocolVersion.CURRENT == 14`,
  `PreferencesFile.Version.CURRENT == 9` — confirm in the live `llms.txt` /
  `proto/`.
- The version lives on the **root `Widget`** (there is no `Screen.Version`); the
  DSL stamps it. For hand-built protobuf set
  `msg.active.version = protobuf.Widget.Version.CURRENT`.
- **Event callbacks block protocol polling** — they run on the poller thread;
  multiple callbacks for one code run in registration order.
- Windows has no libusb: discovery must degrade to a clear `NoBackendError`, not
  a traceback. On Linux you need the udev rule (`bin/install-rules.sh`).
- Generated code (`_proto/*`, `proto/default_screen.json` in the touchy-pad
  repo) is regenerated, never hand-edited.
- Paths must be drive-prefixed (see the mental model).

## Anti-patterns

- Do not clone the touchy-pad repo to *use* it — install the package and keep
  the consuming project's normal tooling (`pip`/Poetry/`cargo`).
- Do not substitute another LED/LVGL library or bit-bang GPIO when the user
  asked for a Touchy-Pad.
- Do not re-upload a whole screen to change one string — use
  `set_properties([build_property_override(...)])`.
- Do not hard-code a pixel size; read `board_info`.
- Do not mix up the two action kinds: `macro_action` works with the host
  detached, `host_action` needs your process running.

