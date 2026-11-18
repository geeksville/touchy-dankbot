# touchy-dankbot — plan: fetch the model on first use (auto-pull)

**Where this sits.** The quip stage ([`initial.md`](initial.md)) and the listen
stage ([`audioin.md`](audioin.md)) both build a `ChatClient` and start talking.
This plan covers the *"the server does not have the model yet"* edge, which
[`jokemark.md`](jokemark.md) already solved for the bake-off — it has
`--pull/--no-pull` plus `has_model` / `pull_model` on the client — but `reply`
and `listen` had no such escape: pointing `dankbot` at a fresh Ollama (or at
`DEFAULT_MODEL` on a box that has not pulled it) failed on the first quip with

```
dankbot: NotFoundError: Error code: 404 - model 'hf.co/…' not found
```

**Status: DONE (2026-11-18).** `ChatClient.ensure_model()` + a shared
`--pull/--no-pull` (`DANKBOT_PULL`) on `reply` / `listen`; `jokemark`'s flag now
reads the same default and env var. `just test` **258 passed / 2 skipped**;
`just lint` clean.

## Approach

1. **`ChatClient.ensure_model(name) -> bool`** (`server.py`, beside
   `has_model` / `pull_model`): ask what the server has; pull the model when it
   is missing; return whether it fetched. `reply` and `listen` call it once at
   startup, from `_build_responder`, before the first chat.
2. **`--pull/--no-pull`**, default **on** (`DEFAULT_PULL`, `DANKBOT_PULL`), added
   to the shared `_LLM_OPTIONS` so `reply` and `listen` cannot drift. `jokemark`'s
   own flag keeps its wording but takes the same default and env var.
3. **Best-effort, never fatal.** A multi-GB download must not be able to break a
   setup that already worked, so `ensure_model` never raises: a server it cannot
   ask (down, or not Ollama — `/api/pull` is Ollama-only) and a failed pull both
   just log, and the chat call that follows reports the real problem.
4. **Say so before it starts.** The pull is blocking (`stream: False`) and can
   run for minutes; the "fetching it" line is logged at `WARNING` because
   `reply` runs at the default `WARNING` level — an `INFO` line would leave the
   user staring at a silent terminal.

## Choices & rationale

| Decision | Why |
|---|---|
| Pull on **both** `reply` and `listen`, not just `listen` | `reply` is the documented first thing to run (`dankbot reply --line …`); a 404 there is the worst first impression. Both share `_LLM_OPTIONS` anyway. |
| Default **on**, with `--no-pull` | Matches `jokemark` (already default-on) and the "just works on a fresh box" goal; the flag is the escape hatch for metered links and for a deliberate 404. |
| `ensure_model` on `ChatClient`, not in `cli.py` | The client owns `has_model` / `pull_model`; the CLI keeps only the wiring (`_build_responder`). |
| `WARNING` for the notice, `DEBUG` for "cannot list models" | `WARNING` is the only level visible in `reply` (which never raises verbosity); a failed *list* is a secondary detail — the chat error that follows is the real one. |
| A failed pull is **not** an error | Keeps `/v1` portable (vLLM / `llama.cpp` have no `/api/pull`) and keeps a transient network blip from killing `listen` at startup. |
| No progress reporting | `/api/pull` is called with `stream: False` (as jokemark does); streaming progress is a separate, larger change (see Open questions). |

## Files

| File | Change |
|---|---|
| `src/dankbot/server.py` | `ChatClient.ensure_model()`. |
| `src/dankbot/config.py` | `DEFAULT_PULL = True`. |
| `src/dankbot/cli.py` | `--pull/--no-pull` in `_LLM_OPTIONS`; `pull=` threaded through `_build_responder`, `reply`, `listen`; `jokemark`'s flag takes `DEFAULT_PULL` / `DANKBOT_PULL`. |
| `tests/test_server.py` | The fetch/no-fetch logic, and both non-fatal paths. |
| `tests/test_cli.py` | `FakeClient.ensure_model` + wiring: fetches when missing, not when present, `--no-pull`, `DANKBOT_PULL=0`, and `listen --fake`. |
| `README.md`, `AGENTS.md` | The `--pull` row, the behaviour paragraph, the `ensure_model` mention. |

## Phases

1. `ensure_model` + `DEFAULT_PULL` + the shared flag (done).
2. Tests: logic in `test_server.py`, wiring in `test_cli.py` (done).
3. Docs (done).

## Testing strategy

Network-free, as everywhere else: `test_server.py` monkeypatches `list_models` /
`pull_model` on a real `ChatClient` (so the logic is the code under test), and
`test_cli.py` drives the real CLI with the `FakeClient`, asserting
`FakeClient.pulled`. The live path is checked by hand against the local server
(see below) rather than in the suite.

## Risks / open questions

| # | Risk / question | Handling |
|---|---|---|
| Q1 | An implicit multi-GB download on `dankbot reply` | Announced at `WARNING` before it starts, `--no-pull` / `DANKBOT_PULL=0` to opt out, and `DEFAULT_PULL_TIMEOUT` (1 h) bounds it. |
| Q2 | A typo'd `--model` now costs a failed pull *and* the chat 404 | Two lines, both naming the model; the alternative (a hard failure) breaks Q3. |
| Q3 | A non-Ollama `/v1` server (vLLM, `llama.cpp`) has no `/api/pull` | `ensure_model` swallows it — the chat call is unchanged, so portability is preserved. |
| Q4 | `listen` startup now needs the server | Only for the model *list*; that failure is `DEBUG` and non-fatal, so `listen` still starts (and reports per utterance, as before). |
| Q5 | The blocking pull looks like a hang on a slow link | The `WARNING` says "this can take minutes". Streaming `/api/pull` progress (and a progress bar) is the recorded follow-up — it needs a streaming HTTP client path. |
| Q6 | `has_model` costs one extra round trip per run | One `/v1/models` (or `/api/tags`) call at startup, once per process — not per line. |
