# Plans

Design and implementation plans live in `docs/plans/*.md` (kebab-case) and are
tracked in git.

## Rules

- When I create or edit a plan, I MUST save it as `docs/plans/<topic>.md`. Create
  the directory if it does not exist yet.
- Always persist plans to that directory so they can be committed — never leave a
  plan only in chat or in a temporary location.
- One plan per file. Update the existing file for a topic instead of creating a
  near-duplicate; use dated filenames only for an explicit supersession.
- A plan should include: goal/summary, approach, library/technology choices with
  rationale, file & component layout, a phased sequence, testing strategy, and
  risks/open questions.
- Do not delete a plan; mark it superseded and link the replacement.

## Existing plans

- `docs/plans/initial.md` — the original touchy-dankbot plan (the responder CLI).
- `docs/plans/audioin.md` — the audio-in stage (mic → Moonshine → gate → reply).
- `docs/plans/ledout.md` — the LED-out stage (scroll quips on a touchy-pad).
- `docs/plans/lcdout.md` — LCD out: tall panels wrap the quip in a multiline
  text field; `<= 32`-px-tall panels keep the scrolling marquee.
- `docs/plans/quipfit.md` — quip fit: a long reply steps down to a smaller
  compiled font (`text_font` override) instead of being ellipsized.
- `docs/plans/standalone.md` — extracting touchy-dankbot to its own repo (packaging,
  PyPI name, CI, devcontainer).
- `docs/plans/jokemark.md` — the model bake-off (`dankbot jokemark`): packaged
  overheard corpus × TOML model matrix → one Markdown report.
- `docs/plans/modelpull.md` — auto-pull: `reply` / `listen` fetch a model the
  server does not have yet (`ensure_model`, `--pull/--no-pull`).
- `docs/plans/micreconnect.md` — supervised capture: poll the PortAudio stream
  and re-open the mic when the array goes away.
- `docs/plans/latency.md` — reply latency instrumentation: split `latency_s`
  into `queue_wait_s` + `model_s` so a backlog is distinguishable from a slow
  model (log suffix, `--json`, DEBUG request sizes).
- `docs/plans/rpi-service.md` — running `dankbot listen` as a systemd **user**
  service (`rpi/user-service.sh`): autostart via linger, journald logging, and the
  `DANKBOT_*` env file.

Docs that are *not* plans: `docs/TODO.md` (loose backlog), `docs/hardware.md`
(hardware notes), `docs/responder.md` / `docs/transcribe.md` (research notes).
