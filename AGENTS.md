# touchy-dankbot — AI agent guide

`CLAUDE.md` is a symlink to this file — keep them in sync via the symlink.

`dankbot` is an eavesdropping desk robot: it listens to nearby conversation,
asks a small LLM (a local [Ollama] server — the remote box is opt-in via
`--url` / `DANKBOT_URL`) for a witty one-liner, and scrolls
that line across an addressable-LED matrix (a [touchy-pad]). It is the AI half
of the **creep3r** art project. This repo is standalone: it depends on the
*published* `touchy-pad` package, not the touchy-pad source tree.

[Ollama]: https://ollama.com
[touchy-pad]: https://github.com/geeksville/touchy-pad

## Repo layout

| Path | Purpose |
|------|---------|
| `src/dankbot/cli.py` | `click` CLI: `reply` / `models` / `jokemark` / `listen` (`listen` always scrolls on the pad). |
| `src/dankbot/server.py` | `ChatClient` (OpenAI `/v1` + native `/api/chat`, plus `has_model` / `pull_model` / `ensure_model`) + `DankbotError`. |
| `src/dankbot/responder.py` | Persona → prompt → reply, and the reply value object. |
| `src/dankbot/jokemark.py` | The model bake-off: matrix/corpus loaders, run loop, Markdown + JSON reports, playback (no-server replay), optional pad echo. |
| `src/dankbot/persona.py` | The default persona (the toaster robot). |
| `src/dankbot/commands.py` | The `/command` channel (`/notfunny`, `/color/NAME`). |
| `src/dankbot/audio.py` | Mic capture + Moonshine STT (the audio-in stage). |
| `src/dankbot/gate.py` | The utterance gate (min words, de-dup). |
| `src/dankbot/listen.py` | The listen loop wiring the above together. |
| `src/dankbot/display.py` | The **only** module that imports `touchy_pad` (the display stage: LED marquee or LCD text field). |
| `src/dankbot/logs.py` | Terminal logging (rich): `HEARD` / `SKIP` / `REPLY` in colour, quips in their display colours. |
| `src/dankbot/config.py` | Defaults + the `Settings` value objects. |
| `src/dankbot/assets/` | Packaged jokemark data: `overheard.txt` + `jokemark-models.toml` (read via `importlib.resources`). |
| `tests/` | pytest (network-free; audio + touchy-pad are faked). |
| `docs/` | `plans/` (design), `hardware.md`, `TODO.md`. |
| `justfile` | All dev tasks — prefer `just <recipe>`. |
| `.agents/skills/moonshine-voice/` | The Moonshine Voice SDK cheat-sheet. |
| `.agents/skills/touchy-pad/` | The Touchy-Pad host SDK cheat-sheet (fetch the live `llms.txt` first). |

## Build / test / run

This project uses **Poetry** (never uv/uvx). Everything goes through `just`:

```bash
just install               # venv + deps
just test                  # pytest (network-free)
DANKBOT_LIVE=1 just test  # + a real request to the server / the mic
just lint                  # ruff format + check
just build                 # wheel + sdist into dist/
just reply -- --line "..."  # run the CLI in the venv
just listen -- --fake sim-overheard.txt
just jokemark              # model bake-off → jokemark.md + jokemark.json (needs the server)
just jokemark -- --playback jokemark.json   # replay a saved run (no server, optional pad)
just bump-version          # bump version, commit, tag, push
```

`uvx <tool>` → `poetry run <tool>`; `uv add <pkg>` → `poetry add <pkg>`.

## Conventions & gotchas

- **Keep `touchy_pad` imports inside `display.py`, and lazy** (inside the
  functions). That is what lets the whole package + test suite run with no
  touchy-pad and no hardware.
- **`touchy-pad` floor carries stage lb26** — the host API the display needs
  (`SetPropertiesCmd` / `build_property_override` + `Label.recolor` arrived in
  0.3.5; 0.3.8 added `Label.scroll_speed_px_s`, the marquee-speed knob
  `display.py` uses to slow the scroll to `DEFAULT_SCROLL_SPEED_PX_S`; **lb26**,
  released in 0.4.0, added `SetPropertyCmd.font_size_value` and
  `FontSize` / `FONT_SIZES` — the runtime `text_font` override a long quip steps
  down through). A host library older than lb26 sends that entry as an
  `int_value`, which the lb26 firmware refuses (and pre-lb26 firmware would read
  as a font pointer), so the floor is 0.4.0, the first release that carries it.
  Older releases break the display path and its tests. To develop against
  upstream instead of PyPI, run `just use-touchy-local`: it clones or
  fast-forwards
  [geeksville/touchy-pad](https://github.com/geeksville/touchy-pad) into the
  git-ignored `reference/touchy-pad`, generates the protobuf bindings upstream
  keeps out of git (via a scratch venv with `grpcio-tools`), and path-depends on
  that checkout's `app/` host library (`just use-touchy-pypi` restores the
  published package).
- **Keep `moonshine_voice` / `sounddevice` imports lazy** too (inside
  `AudioIn._build()` / `resolve_device()`), so importing `dankbot` never needs
  the native libs — and CI needs no mic.
- **The mic is supervised.** PortAudio only *prints* a capture error, so
  `AudioIn.lines` polls the stream (`_capture_alive`, which peeks at the
  transcriber's `_sd_stream`) and reopens the transcriber when it dies,
  re-resolving the device by name (a replug moves the PortAudio index). The
  *first* open still raises `AudioError` (fail loud at boot); only a *re*open
  waits `DEFAULT_RECONNECT_DELAY_S` and retries. See
  `docs/plans/micreconnect.md`.
- **Logging:** `logging.getLogger(__name__)`; `cli.main` calls `logs.configure()`
  (a rich handler on stderr — colour only on a terminal, so pipes and `--json`
  stay clean). Every line leads with a local `logs.TIME_FORMAT` stamp
  (`[16:00:35]`), repeated even within the same second
  (`omit_repeated_times=False`), so a latency that drifts is visible. Every
  command takes `--debug` / `DANKBOT_DEBUG`, whose click
  callback calls `logs.enable_debug()` to drop the root and `dankbot` loggers
  to `DEBUG`; the commands raise `dankbot` to INFO with
  `logs.ensure_verbosity()` (never quieter), so `--debug` survives. The listen
  lines come from `logs.heard_line` / `skip_line` /
  `reply_line`, which draw a quip in its display colours; keep their *plain*
  text (`REPLY: …`, `SKIP(too_short): …`) intact — `grep` and `caplog` rely on
  it. A reply (and a `/notfunny` skip) appends the pipeline latency as a dim
  `(45s latency)` suffix — `Listener._answer` measures the wait plus the model
  round trip, and reports them **split** (`queue_wait_s` / `model_s`, summed by
  `latency_s`); the suffix grows to `(45s latency: 30s queue + 15s model)` once
  the queue wait is real, so a backlog is distinguishable from a slow model.
  Keep the suffix *after* the plain text. High-frequency RPC-ish
  lines go on a child logger.
- **The `/command` contract** lives in `commands.py`; recognised commands are
  stripped from every displayed/printed reply (unknown slash tokens are recovered
  as text). Keep `/notfunny` as the final backstop. Markdown emphasis
  (`*whirr*`, `**clang**`) is not a command — it is split into its own run and
  drawn in `EMPHASIS_COLOURS` (red, purple, blue, green, pink; cycling per
  reply) with the markers stripped, because the panel has no bold or italic.
- **Config is flag → env → default** via click (`default=` + `envvar=`); a new
  setting gets a `DEFAULT_*` in `config.py` and a `DANKBOT_*` env name.
- **No network in tests.** The `ChatClient` is injected/monkeypatched
  everywhere; live checks are opt-in behind `DANKBOT_LIVE=1`.
- Version is single-sourced in `pyproject.toml` + `src/dankbot/__init__.py`;
  bump both with `just bump-version` (never hand-edit one of them).

## Hardware

- Mic: Seeed **reSpeaker XVF3800** USB 4-mic array (`docs/hardware.md`). It does
  its own AEC/beamforming/noise-suppression, but *VAD runs host-side* in
  Moonshine, so the code is mic-agnostic.
- Display: a [touchy-pad] board (LED matrix or LCD) over USB — boards/firmware
  live in the touchy-pad project.
- Audio in the devcontainer: `/dev/snd` is bind-mounted from the host and
  `PULSE_SERVER` points at the host's PipeWire (see `.devcontainer/`).

## Release

`just bump-version [X.Y.Z]` bumps the patch version, commits, tags `vX.Y.Z` and
pushes. Pushing that tag triggers `.github/workflows/release.yml`, which tests,
builds, and publishes `touchy-dankbot` to PyPI via OIDC trusted publishing — no
GitHub Release is needed (`release.yml` refuses a tag that does not match the
version in `pyproject.toml`). `testpypi.yml` is a manual dry-run to TestPyPI.

## Working with this repo (agent rules)

- **Never auto-commit or push.** Make the edits; the human commits. (This
  includes `just bump-version`, which commits/tags/pushes — treat it as a human
  action.)
- Plans live in `docs/plans/*.md`; keep them there, not in chat. See
  `.clinerules/` for the full rules (collaboration, terminal, plans, searching).
