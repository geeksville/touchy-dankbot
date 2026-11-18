# touchy-dankbot — extracting to a standalone repo

## Goal

Make `touchy-dankbot` a self-contained project that no longer needs the touchy-pad
source tree: depend on the *published* `touchy-pad` package, ship a trimmed
devcontainer, add CI + PyPI release workflows, and be movable out of
`touchy-pad/tools/dank-bot` with no code changes.

## Deliverables (this pass)

- **`.devcontainer/`** — a trimmed copy of the parent's (Python, Poetry, just,
  libusb, ALSA/PortAudio, host `/dev/snd` + host PipeWire wiring). Dropped:
  ESP-IDF, Rust/Tauri, Deno, GTK/StreamController, the Qt runtime for the
  touchy-pad simulator GUI, and protoc/nanopb.
- **`.github/workflows/`** — `ci.yml` (ruff + pytest + build, on
  ubuntu/macos/windows), `release.yml` (test → OIDC PyPI publish on a GitHub
  Release), `testpypi.yml` (manual dry run).
- **`pyproject.toml`** — distribution name **`touchy-dankbot`**;
  `touchy-pad` from PyPI (floor `0.4.0`, the first release carrying stage lb26 —
  see below);
  PyPI metadata (keywords/classifiers/urls).
- **`justfile`** — `build`, `bump-version`, and `use-touchy-local` /
  `use-touchy-pypi` recipes for switching the dependency.
- **`README.md`**, **`AGENTS.md`** (+ `CLAUDE.md` symlink), **`.clinerules/`**.

## Key decisions

| Decision | Choice | Why |
|---|---|---|
| PyPI name | `touchy-dankbot` | Renamed `voicebot` → `dank-bot` → `touchy-dankbot` on 2026-11-18: the distribution had been `touchy-voicebot`, and the bare `dank-bot` name was already taken on PyPI. |
| Import package / console script | `dankbot` / `dankbot` | The import package cannot carry a hyphen (not a Python identifier), and the console script follows it (`dankbot`) — only the *distribution* keeps the `touchy-` prefix. Env vars follow the module: `DANKBOT_*`. |
| `touchy-pad` dependency | PyPI `>=0.4.0` (default) | The floor is the first release with the host API the display uses (`SetPropertiesCmd` / `build_property_override` + `Label.recolor` from 0.3.5, `Label.scroll_speed_px_s` — the marquee speed — from 0.3.8, and stage lb26's `SetPropertyCmd.font_size_value` from 0.4.0). `just use-touchy-local` (a path dep on a clone of upstream `main` in the git-ignored `reference/touchy-pad/app`) stays the dev path for host-library work. |
| CI OS matrix | ubuntu + macos + windows | `moonshine-voice` ships wheels for linux x86_64/arm64, macos arm64, win_amd64; the tests import no native libs, so all three install + run. |
| Devcontainer | strip to Python + audio + USB | touchy-dankbot devs do not build firmware or run the sim GUI. |

## Notes / open questions

- **Touchy-pad floor**: the display needs `SetPropertiesCmd` /
  `build_property_override` (0.3.5), `Label.scroll_speed_px_s` (0.3.8) and, for
  the long-quip font step-down, **stage lb26** (`SetPropertyCmd.font_size_value`),
  which shipped as 0.4.0 — so the floor is `>=0.4.0`. `poetry.lock` is
  committed, so a fresh `poetry install` / CI resolve just works.
  `just use-touchy-local` re-locks
  against a clone of upstream `main` (cloned/fast-forwarded into the git-ignored
  `reference/touchy-pad`) for host-library work;
  `just use-touchy-pypi` puts the published package back.
- **`use-touchy-local` mechanics**: the recipe clones/fast-forwards upstream
  `main` into the git-ignored `reference/touchy-pad`, generates the protobuf
  bindings upstream keeps out of git (its `build-proto-py` recipe, fed a scratch
  venv that has `grpcio-tools`), then `poetry add --editable
  reference/touchy-pad/app` and reinstalls.
- **Trusted publishing**: create the `touchy-dankbot` project on PyPI with a
  trusted publisher (workflow `release.yml`, environment `pypi`), plus a
  `testpypi` environment for the dry run. Until that exists the release workflow
  will fail at the publish step.
- **Version sync**: the version lives in `pyproject.toml` *and*
  `src/dankbot/__init__.py`; `just bump-version` keeps both in step.
- Once extracted, the `docs/plans/initial.md` "extraction" risk item is
  resolved and the repo can drop its `tools/dank-bot` framing.
