# touchy-dankbot — plan: microphone reconnect (supervised capture)

**Where this sits.** A hardening pass on [`audioin.md`](audioin.md)'s audio-in
stage. The `MicTranscriber` capture is opened **once** at the start of
`dankbot listen`; if the USB array is unplugged the PortAudio stream dies and
the transcriber goes permanently silent, and replugging it changes nothing —
PortAudio's device list is built at `Pa_Initialize` (no hotplug refresh in the
shipped 19.6), so the app never re-opens it. On an appliance that is a dead
robot that needs a restart to recover. This plan makes the mic **supervised**.

> **Status: IMPLEMENTED.** `AudioIn` now polls its capture stream and reconnects
> (device re-resolved by name, since a replug moves the PortAudio index); the
> first open still fails loudly, only a *re*open waits and retries.
> `DEFAULT_RECONNECT_DELAY_S = 2.0`; `reconnect_delay_s <= 0` turns it off.

## Goal

`dankbot listen` keeps running across a mic being unplugged and plugged back
in — with no restart — on the same box, and the audio comes back.

## Why it does not recover today

| Layer | What happens on unplug |
|---|---|
| ALSA / PortAudio | the capture stream errors and (at best) stops calling the callback |
| `moonshine_voice.MicTranscriber` | the audio callback only **prints** the PortAudio status; `_add_run` only prints a transcribe error. Neither reaches `on_error` (its `Error` event is emitted from `Stream.stop()`, not from the audio path) |
| `dankbot.audio.AudioIn.lines` | `self._mic = self._build()` once, then blocks on `self._queue.get()` forever — no timeout, nothing watches the device |

So nothing in the chain either notices or re-opens. (Same story for the *pad*,
but that is event-driven: `display.py` opens lazily per quip, so a replugged pad
is picked up on the next reply — there is no "next reply" while the mic is gone,
which is why audio needs a poll.)

## Approach

Keep `MicTranscriber` (it owns VAD, streaming, resampling and its capture
worker — see `audioin.md`; taking capture over ourselves is not worth the
duplication). Add supervision *around* it in `AudioIn`:

1. **Liveness probe** — `_capture_alive()` peeks at the transcriber's
   PortAudio stream (`mic._sd_stream`, a `sounddevice.InputStream`) and calls it
   dead when it is `closed`, not `active`, **or** its clock (`stream.time`,
   `Pa_GetStreamTime`) has stopped advancing across two probes. A live stream's
   clock always moves, so a frozen one means no callbacks are running. The probe
   errs towards "dead" — a wrong answer only costs one reconnect.
2. **Reconnect loop** in `lines()` — an inner `_drain()` yields utterances until
   the queue times out and the probe says the stream died (or `on_error`
   flagged `_reconnect`); then the transcriber is torn down and a fresh one is
   built. **`resolve_device` moved from `__init__` into `_build`**, so each
   reconnect re-resolves the mic by name and lands on the new PortAudio index.
3. **Fail loud once, retry after** — the *first* build still raises
   `AudioError` (a box with no mic should say so, as it does today), and a
   config problem (bad `--model-arch`, missing engine) is resolved once up
   front in `_engine()` so it is never retried. Only a *re*open failure waits
   `reconnect_delay_s` and tries again — which is exactly the "device is not
   back yet" case.
4. **Interruptible waits** — a `threading.Event` (`_wake`) lets `close()` end a
   reconnect wait at once, so ctrl-c stays responsive.

## File layout

* `src/dankbot/config.py` — `DEFAULT_RECONNECT_DELAY_S` (+ the `AudioSettings`
  field, keeping that value object complete).
* `src/dankbot/audio.py` — `AudioIn`: `_engine`, `_build` (device re-resolved),
  `lines` / `_drain` / `_capture_alive` / `_wait_for_device`, `close`.
* `tests/test_audio.py` — a scripted fake `moonshine_voice` (`MicTranscriber`
  instances that die, fail to start, or come back) + `_capture_alive` units.
* `README.md` / `AGENTS.md` — a line each.

No new CLI flag: the delay is a `config.py` default like `DEFAULT_MIC_HINT`
(overridable in code/tests via `AudioIn(reconnect_delay_s=…)`).

## Testing strategy

Network-free, hardware-free, as ever. The fakes drive everything:

* the capture dying → the mic is torn down and a **second** transcriber is
  built, and its line is delivered (proves recovery, not just teardown);
* a reconnect attempt failing (`start_error`) → the half-built transcriber is
  released and the *next* attempt succeeds once "the device is back";
* `_capture_alive` units: no stream → assume alive; `active=False` /
  `closed=True` → dead; advancing clock → alive; frozen clock → dead;
* `on_error` flags a reconnect; `close()` interrupts a reconnect wait;
* the existing "first `start()` failure raises `AudioError`" test is unchanged
  (fail-loud at startup is preserved).

## Risks / open questions

* **Detection is best-effort.** It depends on PortAudio reporting the dead
  stream (`active`) or a frozen stream clock. If a backend keeps a dead *and*
  erroring stream "active" with a wall-clock time, we cannot see the loss —
  `MicTranscriber` gives us no event for it. Mitigated by `on_error` also
  flagging a reconnect, and by the reconnect re-resolving the device by name.
  **Verify with a real unplug** on the target box.
* **Cost of a false positive** is one transcriber rebuild (model reload from the
  local cache) — a short audio gap, not a crash. `reconnect_delay_s <= 0` is the
  escape hatch if supervision ever misbehaves on a given box.
* The deeper alternative — own the capture (`sd.InputStream` + a plain
  `Transcriber`) so device loss is detected deterministically — was rejected as
  too much duplication of upstream's worker/resampling for this stage. Revisit
  only if the probe proves unreliable in practice.
* `mic._sd_stream` is a private attribute: guarded with `getattr`, and a
  missing one degrades to "assume alive" (log at debug), never an exception.
