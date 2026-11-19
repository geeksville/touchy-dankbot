# touchy-dankbot — plan: reply latency instrumentation

**Where this sits.** A measurement pass on the audio-in stage
([`audioin.md`](audioin.md)) and the quip stage. In a long `dankbot listen`
session the `(Ns latency)` suffix on the `REPLY:` lines only ever climbs
(observed: 24s → 197s → 349s). That number today is a **single** figure that
conflates two very different things — how long the utterance waited for the
reply worker (the pipeline falling behind) and how long the model itself took —
so it cannot say which one is growing. This plan adds the instrumentation to
tell them apart (and to rule out a couple of other suspects).

> **Status: IMPLEMENTED.** `latency_s` is split into `queue_wait_s` + `model_s`
> on every reply/`/notfunny` skip (log suffix and `--json`); the reply queue
> logs what it drops; the responder and the server log the prompt/reply size and
> their own timing at DEBUG; the per-request `OpenAI` client is closed.

## Goal

Make the growing latency **attributable** from the logs alone, without a
profiler: is it the queue backing up, the model slowing down, a resource leak,
or something being sent to the model that should not be?

## What the number means today

`Listener._answer` reads the clock once, after `Responder.respond` returns:

```python
latency_s = time.monotonic() - pending.heard_at
```

`heard_at` is stamped in `_handle` (when the utterance arrived) and the worker
is a **one-deep-ish queue** (`max_queued=2`, drop-oldest), so that number is:

```
latency_s = queue_wait_s + model_s
            ^^^^^^^^^^^   ^^^^^^^
            how far behind the machine is   how slow the model is
```

Only the sum is reported, so a 349s reply could be a 340s backlog behind a slow
model or a 20s backlog behind a 330s model — and the fix is different in each
case (drop more aggressively / shrink the prompt vs. move the model off the Pi).

## Approach

1. **Split the latency at the source.** `_answer` already has the two clock
   readings it needs; stamp one *before* `respond` and one after, and put
   `queue_wait_s` and `model_s` on the `ListenEvent` beside `latency_s` (their
   sum, kept so nothing that reads `latency_s` changes meaning).
2. **Show the split in the log, only when it matters.** The suffix stays the
   familiar `(45s latency)`; when the utterance actually waited in the queue it
   becomes `(45s latency: 30s queue + 15s model)`. With no queue the whole
   number *is* the model time, so the short form is already the answer — this
   keeps `--fake` and fast replies reading exactly as before (and the existing
   exact-string log tests unchanged).
3. **Report the split in `--json`** as `queue_wait_s` / `model_s`, for a run
   that is captured rather than watched.
4. **Log the queue drops.** `_submit` silently throws away the oldest pending
   utterance when the queue is full; a DEBUG line there is the clearest signal
   that the model cannot keep up with the talk.
5. **Instrument the request itself** (the "are we sending empty phrases?"
   question). The responder logs, at DEBUG, the input/output size and its own
   round-trip time, and **warns** if it is ever asked about an empty line — the
   gate should make that unreachable, so a warning here is a real bug report.
   The server logs the prompt/reply size and the HTTP round-trip time, plus the
   process's open file-descriptor count (a one-line leak gauge for the
   socket-per-request hypothesis below).
6. **Close the per-request `OpenAI` client.** `_chat_openai` builds a fresh
   `OpenAI` (an `httpx.Client`, with a connection pool) for every request and
   never closes it. That is a resource leak and a connection-churn source; a
   `with` block closes it deterministically. (One-line change, easy to revert
   if a pure measurement run is wanted.)


## Why not a profiler / metrics endpoint

The box is a headless appliance; the observable is the terminal log. A
`logging`-based split rides the existing `HEARD`/`SKIP`/`REPLY` lines and
`--json`, needs no new dependency, and is exactly what the operator already
looks at. Anything heavier (cProfile, a Prometheus endpoint) is more machinery
than this stage warrants.

## File layout

* `src/dankbot/listen.py` — `ListenEvent.queue_wait_s` / `.model_s` (and in
  `as_dict`); `_answer` measures the split; `_submit` logs a dropped utterance;
  `log_emitter` passes the split on.
* `src/dankbot/logs.py` — `_append_latency` renders the breakdown;
  `reply_line` / `skip_line` take a `queue_wait_s`; `QUEUE_WAIT_EPS` decides
  when the breakdown is worth showing.
* `src/dankbot/responder.py` — a DEBUG round-trip line + an empty-line warning.
* `src/dankbot/server.py` — a DEBUG `chat` line (prompt/reply size, elapsed,
  open fds) + `with OpenAI(...)` so the client is closed.
* `tests/` — the split on events, the breakdown suffix, the responder/server
  DEBUG lines, and the client-close.
* `README.md` / `AGENTS.md` — the latency note gains the split.

## Testing strategy

Network-free and hardware-free, as ever:

* a reply queued behind a slow worker reports a **large `queue_wait_s`** and a
  small `model_s`, while an unqueued one reports `queue_wait_s ≈ 0`;
* `queue_wait_s + model_s == latency_s` (within the clock's resolution);
* the log suffix shows `(45s latency)` with no queue and
  `(45s latency: 30s queue + 15s model)` with one;
* `--json` carries the two new fields (and `None` on a `HEARD`);
* a full reply queue logs the utterance it drops;
* `Responder.respond("")` warns; a normal line logs its size at DEBUG;
* `ChatClient.chat` logs the sizes at DEBUG and **closes** the `OpenAI` client
  it built (a fake client records `close`).

## Risks / open questions

* **The split assumes the clock readings are cheap and monotonic** — they are
  (`time.monotonic`), and `queue_wait_s` may be very slightly negative if the
  clock ticks between `_handle` and `_answer` in the inline path; the log
  format clamps the derived model time at 0.
* **`openai` client reuse.** `with OpenAI(...)` stops the leak but still opens a
  fresh connection per request. Caching one client on the `ChatClient` would
  reuse the keep-alive connection and could shave real time off each round trip;
  it needs a lifecycle (close at shutdown) and is left as a follow-up so this
  change stays a measurement.
* **A warning on an empty line is belt-and-braces.** `AudioIn._on_line` drops
  blank lines and `UtteranceGate` needs ≥ `min_words` content words, so it
  should never fire; it exists so that if it *does*, it is visible rather than a
  silently wasted round trip.
* **Root cause is still open.** This plan makes the growth visible and
  attributable; the fix (aggressiveness of the drop-oldest queue, the model
  placement, the prompt) follows from which term grows.
