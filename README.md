# touchy-dankbot
An **eavesdropping desk robot**: it listens to nearby conversation and asks a
small LLM for a witty one-liner, then shows it on a [touchy-pad] display — a
one-line marquee scrolling across an addressable-LED matrix, or a wrapped text
field on an LCD.

`dankbot` is a local (not connected to the internet) art experiment — a pendant LED
pillar that overhears, quips, and scrolls. It runs on a Raspberry-Pi-class box,
with a USB mic for ears and a [touchy-pad] display (an LED matrix, or any
touchy-pad LCD) for a mouth, and it uses an [Ollama] instance for the wit.

[touchy-pad]: https://github.com/geeksville/touchy-pad
[Ollama]: https://ollama.com

> **Status: early.** Today this is the **responder + listener**: text (or
> speech) in, quip out — served by an Ollama server (by default the local
> `ollama serve`; no model ships with dankbot), optionally shown on a
> **touchy-pad**. Speech-to-text runs locally with **Moonshine Voice**. 

## How it works

```
   [ people talking nearby ]
             |
             v
   [ mic + VAD + STT ]         Moonshine Voice — on-device, CPU-only
             |  text, one utterance at a time
             v
   [ utterance gate ]          >= a few content words, de-duplicated
             |
             v
   [ dankbot responder ]      a witty line via a local Ollama LLM
             |  a one-to-two-sentence quip, or /notfunny
             v
   [ touchy-pad display ]      scrolling marquee (LED) / wrapped text (LCD)
```

Two halves, usable independently:

* **Audio in** (`dankbot listen`) — mic -> transcript -> gate -> LLM, logging
  `HEARD:` / `SKIP(...)` / `REPLY:` per utterance.
* **LED out** (`dankbot listen`) — each quip is a *single* property write on a
  touchy-pad; an LED matrix scrolls it, a tall LCD wraps it in a text field. No
  screen re-upload.

## Install

Requires **Python 3.11-3.14**.

```sh
pip install touchy-dankbot    # or:  pipx install touchy-dankbot
```

That pulls the [`touchy-pad`](https://pypi.org/project/touchy-pad/) host library
(**>= 0.4.0**) from PyPI for the pad output. The LLM / STT half needs no
touchy-pad at all — with no pad attached the display is simply a logged warning.
The display relies on a recent touchy-pad host API
(`SetPropertiesCmd` / `build_property_override` + inline-recolour labels,
`scroll_speed_px_s` for the marquee speed, and the `text_font` override that
steps a long quip down a font size — that last one needs **stage lb26**, shipped
in 0.4.0); if the published version is too old, run
`just use-touchy-local`, which clones the git `main` (see [Dev](#dev)).
Without it the quip still shows, just at full size.

You also need a reachable **Ollama** server. The default is the local one —
`ollama serve` on the same box, `http://localhost:11434`; for a remote box over
Tailscale / LAN, point `--url` / `DANKBOT_URL` at it. On first use, Moonshine
downloads its STT model into `~/.cache/moonshine_voice` and runs offline
afterwards.

For the mic you want a real USB array (the Seeed reSpeaker XVF3800 is what this
was built against — see [docs/hardware.md](docs/hardware.md)); it is auto-selected
by name, so `--device` is only needed for a different input.

## Quickstart

```sh
# One line:
dankbot reply --line "the bread is done baking"

# A file of overheard lines (one per line):
dankbot reply -f sim-overheard.txt

# What's pulled on the server?
dankbot models

# Listen to the mic (transcribe + witty reply, logged and scrolled on the pad):
dankbot listen

# Replay a file instead — no mic, no model (the pad still gets the quips):
dankbot listen --fake sim-overheard.txt

# Bake off models: packaged corpus × model matrix → jokemark.md + jokemark.json
dankbot jokemark

# Replay a saved bake-off: no server, no model — log + pad, 1 s per quip
dankbot jokemark --playback jokemark.json
```

## Commands

| Command | What it does |
|---|---|
| `dankbot reply` | Read overheard lines (`--line` / `-f FILE` / stdin) and print a witty reply for each (`--json` for `{input, reply, skip, model, raw}` per line). Commands are stripped from the printed text, so a `/notfunny` reply prints nothing. |
| `dankbot models` | List the models the server has pulled. |
| `dankbot jokemark` | Bake off the quip stage: every model/params in a TOML matrix against the packaged overheard corpus (fetching missing models), writing a Markdown report and a JSON sidecar (default `jokemark.md` + `jokemark.json`). `--dry-run` shows the matrix without touching the network. If a touchy-pad is attached, each reply is also drawn on it as it is produced (entirely optional). `--playback FILE` replays a saved JSON report instead — no server, no model: entries are logged, quips shown on an attached pad, one second each. |
| `dankbot listen` | Listen to the mic, transcribe with **Moonshine**, log `HEARD:` / `SKIP(...)` / `REPLY:` per utterance, and scroll each quip on an attached touchy-pad; `--fake FILE` replays a text file instead (no mic, no model). |

Configuration is **flag -> env -> default** — click implements the precedence:

| Setting | Flag | Env | Default |
|---|---|---|---|
| Server URL | `--url` | `DANKBOT_URL` | `http://localhost:11434` (local `ollama serve`; the old remote box `https://kai.pygmy-map.ts.net` still works) |
| Model | `--model` | `DANKBOT_MODEL` | Rocinante-X-12B (Q4_K_M) |
| Persona | `--persona` / `--persona-file` | `DANKBOT_PERSONA` | the toaster robot |
| Temperature | `--temperature` | `DANKBOT_TEMP` | `0.9` |
| min-p | `--min-p` | `DANKBOT_MIN_P` | `0.05` |
| Context | `--ctx` | `DANKBOT_CTX` | `4096` |
| Max output tokens | `--max-tokens` | `DANKBOT_MAX_TOKENS` | `120` |
| Native `/api/chat` | `--native` | `DANKBOT_NATIVE` | off (use `/v1`) |
| Fetch a missing model | `--pull` / `--no-pull` | `DANKBOT_PULL` | on |

The default is what won the bake-off. Fallbacks, both already in the matrix:
`--model hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:latest` (the disciplined
8B) or `--model hf.co/bartowski/Human-Like-Mistral-Nemo-Instruct-2407-GGUF:Q8_0`
(the 12B runner-up).

A model the server does not have yet is fetched on first use: `reply` and
`listen` ask the server what it has at startup and pull the model when it is
missing, so a fresh box works on the first quip instead of failing with a 404.
A multi-GB download is a surprise, so it logs a `WARNING` before it starts —
and `--no-pull` (`DANKBOT_PULL=0`) turns the fetch off. The fetch is
best-effort: a server that cannot be asked, or that is not Ollama at all
(`/api/pull` is Ollama-only — the `/v1` path stays portable to vLLM /
`llama.cpp`), only gets a log line and the chat call reports the real problem.
`dankbot jokemark` does the same per matrix entry.

Every command also accepts `--debug` (env `DANKBOT_DEBUG`): it lowers the
Python logging level from `WARNING` to `DEBUG`, so the verbose detail — HTTP
request/retry lines, display rendering, cleanup failures — is printed on stderr
alongside the usual `HEARD:` / `SKIP(...)` / `REPLY:` lines.

## Model bake-off (jokemark)

`dankbot jokemark` runs the quip stage over a corpus instead of one line at a
time:

```sh
just jokemark                     # or: dankbot jokemark
dankbot jokemark --dry-run       # print the resolved matrix + line count
dankbot jokemark --models mine.toml -o report.md --no-pull
dankbot jokemark --playback jokemark.json   # replay a saved run (no server)
```

* **Corpus:** `src/dankbot/assets/overheard.txt` — the six
  `sim-overheard.txt` smoke lines plus restaurant/dinner-party conversation,
  shipped in the package and read with `importlib.resources`.
* **Matrix:** `src/dankbot/assets/jokemark-models.toml` — model paths plus
  optional `label`, `temperature`, `min_p`, `ctx`, `max_tokens`, `native` and
  `timeout`. TOML, so the file can carry comments about each experiment.
  `[defaults]` applies to every row; a row overrides it. Add a candidate model
  there (or in your own `--models` file) to put it in the bake-off; comment an
  entry out to retire it, leaving the verdict beside it.
* **Fetching:** missing models are pulled via Ollama's `/api/pull` first
  (`--no-pull` records them as errors instead).
* **Pad (optional):** if a touchy-pad is attached when the run starts, every
  reply is also drawn on it as it arrives — the same parsed colour runs
  `dankbot listen` scrolls. A missing pad is ignored (one warning, no
  per-reply probing), so the bake-off never waits for hardware;
  `--display-mode` picks the layout.
* **Reports:** one Markdown file with a summary table (replies / skips / errors /
  median / slowest) and, per model, every line with the quip it produced, the
  raw output (commands visible) and the latency — plus a JSON sidecar
  (`<out>.json`, `jokemark.json` by default) with the same data, untruncated
  and including the colour runs. Both are generated artifacts.
* **Playback:** `--playback jokemark.json` replays that sidecar without a
  server or a model — every entry is logged (`label · overheard → REPLY: …`,
  in the quip's display colours) and each non-skip quip is drawn on an attached
  touchy-pad, pausing one second so it can be read.

The first six corpus lines are the old manual-testing file, so the bake-off
keeps covering everything `dankbot reply -f sim-overheard.txt` used to.

## The name of the game

A small model asked to "tell me a joke" regurgitates memorised dad jokes. Wit
comes from a **persona baked into the system prompt** plus a **loosened
sampler** (`temperature=0.9`, `min_p=0.05`). The persona also defines a tiny
**command channel**: a recognised command in the model's output is a directive
dankbot consumes — and never displays (an unknown slash token is recovered as
text).

* `/notfunny` — "nothing worth saying": the reply is *suppressed* rather than
  showing a weak joke.
* `/color/NAME` — colour the text — `red` / `green` / `blue` / `yellow` /
  `white` / `purple` / `pink`, or a `0xRRGGBB` hex, e.g. `/color/red Danger
  ahead`.

Markdown emphasis becomes colour too, since the panel has no bold or italic:
`*whirr*`, `*crickets*`, `*glitches and sparkles*` scroll as the phrase alone,
cycling red → purple → blue → green → pink (an explicit `/color` still wins, and
any leftover `*` is dropped).

All of that lives in [`src/dankbot/persona.py`](src/dankbot/persona.py) and
[`src/dankbot/commands.py`](src/dankbot/commands.py).

## Listening (`dankbot listen`)

The audio-in half: open the mic, transcribe speech with **Moonshine Voice**
(streaming, CPU-only — the Raspberry Pi target), keep only utterances with at
least a few content words, and ask the LLM for a quip, logging `HEARD:` /
`SKIP(...)` / `REPLY:` lines and showing the quip on an attached touchy-pad
(that is always on; a missing pad just logs a warning).

Those log lines go to **stderr** and are rendered with
[`rich`](https://rich.readthedocs.io): `HEARD:` / `SKIP(...)` / `REPLY:` each get
their own colour, and the `REPLY:` line prints the quip **in the same colours the
display scrolls** for it (see [`src/dankbot/logs.py`](src/dankbot/logs.py)) — so
a colour reply is visible without the pad. A pipe or `NO_COLOR` falls back to
plain text, and the message text itself is unchanged.

The microphone is **supervised**: `dankbot listen` watches its PortAudio capture
stream and, if the array goes away (unplugging the USB cable), re-opens it — so
plugging the mic back in brings the ears back with no restart, even if it lands
on a different device index. Boot with no mic is still a hard error.

| Setting | Flag | Env | Default |
|---|---|---|---|
| Capture device | `--device` | `DANKBOT_MIC` | auto (name ~ `respeaker`) |
| STT model size | `--model-arch` | `DANKBOT_MODEL_ARCH` | `small` (tiny/base/small/medium) |
| Min content words | `--min-words` | `DANKBOT_MIN_WORDS` | `3` |
| Min duration (s) | `--min-duration` | `DANKBOT_MIN_DURATION` | `0.6` |
| Repeat debounce (s) | `--debounce` | `DANKBOT_DEBOUNCE` | `5.0` |
| Replay instead of the mic | `--fake FILE` | — | off |
| JSON events | `--json` | — | off |
| On-device screen name | `--screen-name` | — | `dankbot` |
| Panel layout | `--display-mode` | `DANKBOT_DISPLAY_MODE` | `auto` (height rule) |
| Blank after quiet (s) | `--clear-after` | `DANKBOT_CLEAR_AFTER` | `120` (`0` = never) |

All the LLM flags (`--url` / `--model` / `--persona` / ...) are shared with
`reply`.

## The display (on the touchy-pad)

The display half: dankbot builds **one** label screen on a touchy-pad, then each
quip is a single property write (no screen re-upload). The layout comes from the
panel's reported height:

* **`<= 32` px tall (an LED matrix)** — one full-panel *scrolling marquee*
  (`LONG_MODE_SCROLL_CIRCULAR`). See
  [docs/plans/ledout.md](docs/plans/ledout.md).
* **taller (an LCD)** — one *multiline text field* that wraps to the panel width
  and puts `...` on the last visible line if a quip is too long
  (`LONG_MODE_DOTS`). A typical <100-character quip fits with room to spare. See
  [docs/plans/lcdout.md](docs/plans/lcdout.md).

`--display-mode` (`DANKBOT_DISPLAY_MODE`) overrides the rule with `scroll` or
`multiline`; the default `auto` uses the height. `/color/NAME` runs (and the
colour runs the parser makes from `*emphasis*`) are rendered in colour via LVGL
inline recolour on **both** layouts — e.g. `/color/red Danger ahead` shows
"Danger ahead" in red.

* The panel shows **"I'm dankbot"** on connect, then the quips; after
  `--clear-after` seconds with nothing new it goes blank.
* On an LED matrix the marquee scrolls at **30 px/s** —
  `DEFAULT_SCROLL_SPEED_PX_S` (slower than LVGL's own 40 px/s default) — via
  touchy-pad's `scroll_speed_px_s` label field (stage LB19, touchy-pad 0.3.8).
* The pad is opened **lazily** — plug it in at any time and the next quip lands
  on it. Nothing is fatal: no pad / an unplugged pad / an incompatible firmware
  all just log a warning and retry. Swapping an LED matrix for an LCD (or back)
  rebuilds the screen in the other layout on the next quip.
* Geometry and the base font come from the board's reported display size, so the
  same code drives an 8x8 / 32x8 LED matrix and a 480x272 LCD; 8/16/30 px are the
  compiled fonts, and the LCD picks the largest that leaves two lines.
* A **long quip steps down to a smaller font** instead of being cut off: the
  label's drawn length (colour markup excluded) is fitted to the field, and the
  `text_font` pixel size rides in the same property batch as the text — ~170
  characters fit at 30 px on the LCD, 500+ at 16 px. The marquee never needs
  this (it scrolls forever). See
  [docs/plans/quipfit.md](docs/plans/quipfit.md).
* A **fresh LED board is headless** (`0x0`) until its panel chain is
  provisioned: `touchy pref from-template led-32x8`. dankbot says so if it sees
  `0x0`.
* The screen is saved as `F:host/s/dankbot.pb` (`--screen-name` to change it) —
  deliberately *not* `default`, so `touchy init` can't clobber it.
* A quip is capped by the firmware's `SetPropertyCmd` string buffer — a 512-byte
  nanopb field, i.e. **511 characters** (the terminator is counted); dankbot
  truncates a longer reply to fit, so a chatty model can't overflow it. Use
  `--max-tokens` to keep replies short if you prefer.

## Dev

```sh
just install              # venv + deps
just test                 # pytest (network-free)
DANKBOT_LIVE=1 just test # + one real request to the server / the mic
just lint                 # ruff format + check
just build                # wheel + sdist into dist/
just bump-version         # bump version, commit, tag, push (tag → PyPI publish)
```

The devcontainer (`.devcontainer/`) ships Python, Poetry, just, libusb (for the
touchy-pad USB transport) and ALSA / PortAudio (for the mic) — open it in VS Code
and `just test` works. By default the `touchy-pad` dependency is the **published
PyPI package**; `just use-touchy-local` clones (or fast-forwards)
[upstream](https://github.com/geeksville/touchy-pad) into the git-ignored
`reference/touchy-pad` and depends on that checkout's `app/` host library while
hacking on touchy-pad itself (`just use-touchy-pypi` puts the published package
back).

Only [`src/dankbot/display.py`](src/dankbot/display.py) imports `touchy_pad`,
and it does so lazily, so the rest of dankbot (and the whole test suite) runs
with no touchy-pad and no hardware.

## Contributing

you probably don't want this yet.

I'm okay with using AI tools to help make code — I used them a fair amount on
this project (one of my first experiments with not writing all my code "by
hand"). But some of my other open-source projects have shown me the current hell
that PR management is becoming, so I'd **love** any contributions (and I promise
to be kind), with two asks:

* Send only PRs **you** are willing to sign off as "nicely written" (with your
own experience as a software engineer). If your little AI buddy made something a
bit ugly, iterate with it until it isn't.
* Keep PRs fairly **atomic** — touch just the code a single, nicely defined
  feature or bug-fix needs.
* Only send **tested** code you've run against real hardware (not just the
  simulator).

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).  Copyright 2026 S. Kevin Hester, kevinh@geeksville.com.
