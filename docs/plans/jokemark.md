# voicebot — plan: jokemark (a model bake-off for the quip stage)

**Where this sits.** This extends Stage 1/2 of
[`initial.md`](initial.md) — the responder CLI (`voicebot reply` /
`voicebot models`). It replaces the ad-hoc loop of

```sh
voicebot reply -f sim-overheard.txt --model A | tee a.txt
voicebot reply -f sim-overheard.txt --model B | tee b.txt
# … eyeball two loose text files, forget which temperature they used
```

with one command that runs a **packaged, restaurant/dinner-party corpus**
against a **TOML model matrix**, pulls whatever the server is missing, and
writes a **single Markdown report** with one section per model/parameter set.
It exercises only the responder (persona → prompt → reply → `/command` parse);
the mic, gate and display are deliberately not involved.

> **Status: DONE (2026-11-17).** Implemented as planned:
>
> * **Assets** — `src/voicebot/assets/overheard.txt` (36 unique lines: the six
>   `sim-overheard.txt` smoke lines verbatim + 30 restaurant/dinner-party lines)
>   and `src/voicebot/assets/jokemark-models.toml` (Hermes-3 8B + Human-Like
>   Mistral Nemo 12B, each at the default sampler and with a temperature
>   variant). `pyproject.toml` includes `src/voicebot/assets`; `just build` was
>   verified — both the wheel and the sdist contain the two files.
> * **Core** — `src/voicebot/jokemark.py`: `ModelSpec` / `LineResult` /
>   `ModelResult`, strict `load_matrix`, `load_lines`, `run_benchmark` (pull,
>   model-outer, abort-the-bank-on-error, per-row latency, partial results via
>   `on_result`) and `render_markdown`.
> * **Fetching** — `ChatClient.has_model` (tolerates implicit `:latest`) and
>   `ChatClient.pull_model` (`POST /api/pull` `{"model", "stream": false}`),
>   with `DEFAULT_PULL_TIMEOUT = 3600.0`.
> * **CLI** — `voicebot jokemark` with `--models`, `-f/--lines`, `-o/--out`,
>   `--url`, `--native`, `--timeout`, `--pull/--no-pull`, `--limit`,
>   `--persona`, `--persona-file`, `--dry-run`; `just jokemark`.
> * **Docs** — README "Model bake-off (jokemark)", AGENTS layout/commands,
>   `.clinerules/plans.md` index; `jokemark.md` gitignored as an artifact.
> * **Matrix update (2026-11-17)** — added four newer candidates at `Q4_K_M`
>   (TheDrummer Rocinante-X 12B, a Gemma 3 4B comedy fine-tune, Gemma 4 12B,
>   and a Qwen3.5 9B Claude-merge) so each fits a 16GB Raspberry Pi 5; the
>   existing entries and the 0.85 benchmark baseline are unchanged. Of those
>   four only Rocinante survived the ear test — the other three are retired in
>   place (commented-out `[[models]]` blocks with a verdict), not deleted.
> * **Matrix format (2026-11-17)** — `jokemark-models.json` became
>   `jokemark-models.toml`: the matrix is meant to be hand-edited, and comments
>   beat a JSON file that cannot explain itself. `tomllib` is stdlib on the
>   project's 3.11 floor, so this cost no dependency (PyYAML would have); the
>   shape is unchanged (`version`, `[defaults]`, repeated `[[models]]` tables).
> * **Default model (2026-11-17)** — `DEFAULT_MODEL` is now
>   `hf.co/TheDrummer/Rocinante-X-12B-v1-GGUF:Q4_K_M` (the funniest entry in the
>   bake-off, and Q4_K_M keeps it Pi-sized). Hermes-3 8B stays in the matrix as
>   the disciplined baseline and Human-Like Nemo Q8_0 as the 12B runner-up; the
>   packaged matrix is asserted to contain the default.
>
> `just lint` clean; `just test` **161 passed / 2 skipped**. Live checks
> (2026-11-17): `/api/pull` accepted `"model"` and returned
> `{"status":"success"}` on the 0.40.14 server, and a one-model × 2-line run
> produced a correct report (1 reply, 1 `/notfunny` skip, 0 errors) — the run
> also surfaced a model emitting `/meer…`, which the responder strips as an
> unknown command, exactly the sort of thing the bake-off exists to show. A
> full 4-entry × 36-line baseline run (P5) remains a manual step.
>
> **Implementation notes.** The summary's `errors` counts every line without a
> reply (failed cell + never-attempted rows); raw newlines render as `<br>`;
> Ctrl-C renders the banks completed so far via the `on_result` callback and
> exits 130.

## Goal / summary

`voicebot jokemark` should:

1. read a longer **overheard corpus** shipped inside the package
   (`voicebot/assets/overheard.txt`) — the six current `sim-overheard.txt`
   lines plus ~30 typical restaurant / dinner-party lines;
2. read a **model matrix** from TOML (`voicebot/assets/jokemark-models.toml`),
   so a model entry can carry sampler parameters, not just a name;
3. iterate the matrix, **pulling** each model through Ollama's `/api/pull` when
   the server does not already have it;
4. for every model, ask for a reply to every corpus line and keep the result
   (text, skip flag, raw output, latency);
5. write one Markdown file that shows, per model/parameter set, what that model
   would have said for every line — plus a summary table and any errors.

Definition of done: `just jokemark` on a machine that can reach the Ollama
server produces `jokemark.md` with every model/section filled in, and the
network-free test suite covers the loaders, the run loop, the renderer and the
CLI.

## Why bother (what the manual loop gets wrong)

* **Six lines is too small.** Two or three lines in, every model has said
  something plausible; the interesting failures (`/notfunny` abuse, emoji,
  run-on paragraphs, weak jokes) only show up over a wider set of inputs.
* **Parameters drift silently.** `--temperature` / `--min-p` / `--native` live
  in the shell history, not in the artifact, so two runs are not comparable.
* **Models are easy to forget to pull**, and a half-pulled HF path fails
  mid-run with an OpenCLI error that looks like a prompt problem.
* **Output is not archived.** Two terminals of stdout are not a record of what
  was evaluated; the Markdown file is.

## Approach

Keep the same shape as the rest of the CLI: a **pure core module** (`jokemark.py`
— loaders, runner, renderer, no click, no sockets), a **thin click command** in
`cli.py`, and the transport work in `server.py`. The run reuses the existing
`Responder` unchanged (so the persona and `/command` parsing are exactly what
`reply`/`listen` use), adds two native-API methods to `ChatClient` for the
Ollama pull, and renders the collected `Reply` objects to Markdown. Assets are
ordinary text/TOML inside the import package, read with `importlib.resources`
so an installed wheel behaves like a checkout.

## The CLI

New subcommand (not a flag on `reply`): the other stages are already one
command each (`reply`, `models`, `listen`) and `jokemark` has its own option
set — it must not inherit `--model` / `--temperature` etc., because the matrix
owns those.

```
voicebot jokemark [OPTIONS]

  --models FILE        TOML matrix (default: packaged jokemark-models.toml)
  -f, --lines FILE     overheard corpus (default: packaged overheard.txt)
  -o, --out FILE       Markdown report (default: ./jokemark.md)
  --url URL            Ollama server (default: $VOICEBOT_URL / DEFAULT_URL)
  --native             talk to /api/chat instead of /v1 (matrix may override)
  --timeout S          per-request chat timeout (default: DEFAULT_TIMEOUT)
  --pull / --no-pull   fetch missing models first (default: --pull)
  --limit N            only the first N corpus lines (quick smoke run)
  --persona TEXT       system prompt, as for reply/listen
  --persona-file FILE  read the system prompt from FILE (wins over --persona)
  --dry-run            resolve + print the matrix and line count, then exit
```

Rationale:

* **`--dry-run`** validates a hand-edited TOML/corpus without touching the
  network — it is also how the CLI test asserts the matrix was parsed.
* **`--limit`** keeps iteration (and a live smoke test) cheap.
* **`--no-pull`** is the offline / “server is already warm” switch; with it a
  missing model becomes a recorded error instead of a multi-gigabyte download.
* **`--out`** defaults to a fixed name so a run is easy to find and re-run;
  the path is echoed on stdout when the report is written.
* No `--json` yet: Markdown is the deliverable. A machine-readable result JSON
  is a natural follow-up once there is a consumer (see *Deferred*).

Exit status: **0** when at least one model produced a reply (per-model errors
are data, not a crash); **1** when the matrix/corpus cannot be loaded, or no
model produced a single reply. A run that fails still writes its report, so the
failure is inspectable. Bad `--models` / `-f` paths are ordinary click errors.
The options follow the repo convention — `--url`, `--native` and `--timeout`
read the same `VOICEBOT_URL` / `VOICEBOT_NATIVE` / `VOICEBOT_TIMEOUT` env vars
as `reply` and `listen`.

## Data: the packaged assets

```
src/voicebot/assets/
├── overheard.txt            # 6 existing lines + ~30 restaurant/dinner-party lines
└── jokemark-models.toml     # the model/parameter matrix (comments welcome)
```

They live *inside* the import package so they ship in the wheel and are found
with `importlib.resources`:

```python
from importlib import resources

def packaged_text(name: str) -> str:
    return (resources.files("voicebot") / "assets" / name).read_text(encoding="utf-8")
```

`files("voicebot") / "assets" / …` works for an installed wheel (including a
zipped one) and for an editable install; the assets directory does **not** need
an `__init__.py`. `pyproject.toml` gets an explicit
`include = [{ path = "src/voicebot/assets", format = ["sdist", "wheel"] }]`
under `[tool.poetry]` so packaging never depends on Poetry's "data files inside
a package are automatic" behaviour; P1's acceptance step unzips the wheel to
prove the two files are in it.

### The corpus (`overheard.txt`)

Rules for the ~30 new lines:

* **Restaurant / dinner-party small talk**, the kind of sentence a mic would
  actually catch: ordering, wine, splitting the bill, someone's new job, a
  holiday story, a cold dish, a dietary preference, a loud table next door.
* **Bare utterances, no speaker labels** — that is what STT hands the
  responder. Lowercase, light punctuation (the existing lines include `?` and a
  dash), one line each.
* **4–14 words**, with a handful of 2–3-word fragments so the `/notfunny`
  restraint rate on weak input is visible. (The listen-stage gate is *not*
  applied here: jokemark feeds every line on purpose.)
* **Unique lines** — the report keys a row by line, so a duplicate would be
  ambiguous. Blank lines are ignored; a leading `#` is not special (an
  overheard line may contain one).
* The **first six lines are the existing `sim-overheard.txt` verbatim**, so the
  benchmark keeps covering everything the old manual runs did.

`sim-overheard.txt` itself stays in the repo as the tiny smoke fixture used by
`README.md`, `just reply` and `just listen --fake`; a test asserts every line
in it still appears in the packaged corpus, so the two cannot drift.

### The model matrix (`jokemark-models.toml`)

An envelope document rather than a bare list, so shared defaults and future
matrix axes (personas, repeats, seeds) have somewhere to go. TOML specifically,
because this file is edited by hand and each experiment deserves a comment
(why this quant, what this variant is probing). `tomllib` is stdlib from 3.11,
so it needs no dependency — unlike YAML, which would pull in PyYAML.

```toml
# Per-entry keys: model, label, temperature, min_p, ctx, max_tokens, native, timeout.
version = 1

[defaults]
temperature = 0.85
min_p = 0.05
ctx = 4096
max_tokens = 120

# The disciplined baseline.
[[models]]
model = "hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:latest"
label = "hermes-3-8b"

[[models]]
model = "hf.co/bartowski/Human-Like-Mistral-Nemo-Instruct-2407-GGUF:Q8_0"
label = "human-like-nemo-12b"

# Near-greedy: consistent, but repeats itself.
[[models]]
model = "hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:latest"
label = "hermes-3-8b t=0.0"
temperature = 0.0

[[models]]
model = "hf.co/bartowski/Human-Like-Mistral-Nemo-Instruct-2407-GGUF:Q8_0"
label = "human-like-nemo-12b t=1.0"
temperature = 1.0
```

* The first two entries are the models that were being evaluated by hand when
  the bake-off was written up (Hermes-3 8B was `DEFAULT_MODEL` then;
  Human-Like Mistral Nemo Q8_0 was the funnier-but-more-erratic alternative
  documented in `initial.md`). The two `t=` variants exist to exercise the
  parameter axis and to answer the sampler question with evidence rather than
  feel.
* **Per-entry keys:** `model` (required), `label` (optional, must be unique),
  `temperature`, `min_p`, `ctx`, `max_tokens`, `native`, `timeout`.
  Missing sampler values come from `defaults`, then from
  `voicebot.config` (`DEFAULT_TEMPERATURE`, …).
* **`label`** is the report's heading key; when absent it is derived from the
  model path (`…/Hermes-3-Llama-3.1-8B-GGUF:latest` → `hermes-3-llama-3.1-8b`).
  Derived labels that collide are an error, not a silent merge.
* **Validation is strict and loud:** unknown keys (typo protection), an empty
  `models` list, a missing/blank `model`, a duplicate label, an unsupported
  `version`, and out-of-range numbers (`temperature < 0`, `min_p` outside
  0–1, `ctx` / `max_tokens` ≤ 0) all fail before any network call, with the
  offending entry named.
* `--models` accepts a hand-written file; the packaged one is only the default.
  That is the intended way to try a new candidate model: add one entry, run
  `jokemark --models mine.toml`, compare against the baseline report.

## Fetching models

`ChatClient` grows two small, native-API methods (the OpenAI `/v1` surface has
no pull):

```python
def has_model(self, name: str) -> bool:
    """True if *name* is already pulled (tolerating Ollama's implicit ':latest')."""

def pull_model(self, name: str, *, timeout: float = DEFAULT_PULL_TIMEOUT) -> None:
    """POST {url}/api/pull {"model": name, "stream": false}; VoicebotError on failure."""
```

* `has_model` compares against `list_models()` with the `:latest` suffix
  normalised on both sides, so `hf.co/…/foo` matches `hf.co/…/foo:latest`.
* `pull_model` is **blocking with `"stream": false`** (the server does the
  download; we only need the final status). A streaming progress bar is a
  later nicety, not part of this pass.
* Pulls use their own `DEFAULT_PULL_TIMEOUT = 3600.0` in `config.py`, not the
  120 s chat timeout: the 12B Q8_0 GGUF is roughly 13 GB, and a cold fetch over
  a home uplink is minutes, not seconds.
* A pull failure is logged with the model name and recorded as that matrix
  entry's error; the run moves on to the next entry. With `--no-pull`, a model
  that `has_model` does not see is recorded as `not pulled (--no-pull)` and its
  bank is skipped.
* `pull_model` uses `requests` directly (already a dependency for the native
  `/api/chat` path) and normalises everything to `VoicebotError`, matching
  `server.py`'s existing style.

## The run loop

```
for spec in matrix:                      # matrix order, one model loaded at a time
    if pull and not client.has_model(spec.model): client.pull_model(...)
    for i, line in enumerate(lines, 1):  # corpus order
        reply = responder.respond(line)  # persona fixed for the whole run
        record(spec, line, reply, elapsed)
        log progress: "hermes-3-8b 12/36"
```

* **Model-outer, line-inner.** It keeps one model resident for its whole bank
  (the server does not have to swap weights per line) and it is the order the
  report is read in.
* **One `Responder` per model**, built with that entry's `ChatClient`
  (`Settings` from the merged entry parameters) and the run's persona. The
  responder's existing parsing gives `text`, `skip`, `runs` and `raw` for free,
  so no new prompt/parse logic is introduced.
* **Sequential, no concurrency.** Ollama serves this box one generation at a
  time; firing 36 requests in parallel would just queue and make the latencies
  meaningless.
* **Per-line failure policy:** the first `VoicebotError` inside a model's bank
  is recorded and aborts *that bank* (server down / model unloaded). The next
  matrix entry still gets its chance. This avoids 36 consecutive 120 s timeouts
  when the server has gone away.
* **Latency is measured around `responder.respond`** and stored per reply —
  the report's summary shows the median and the slowest reply, which is the
  number that matters for the 30 s budget in `initial.md`.
* The persona is read once (file wins over flag, as in `reply`/`listen`) and
  its **first line plus SHA-256 prefix go into the report header**, so a
  persona edit cannot silently invalidate a comparison.
* A keyboard interrupt writes whatever has been collected so far (a partial
  report beats losing a long run) and exits non-zero.

## The report

One Markdown file, `jokemark.md` by default. Shape:

```markdown
# jokemark — 2026-11-17 21:04 UTC

Server `https://kai.pygmy-map.ts.net` · corpus `overheard.txt` (36 lines)
· persona `You are a witty, slightly dramatic robot lightbulb…` (sha256:1a2b3c4d)
· native: no

## Summary

| # | label | model | temp | min-p | ctx | max | replies | skips | errors | median | slowest |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | hermes-3-8b | hf.co/NousResearch/…:latest | 0.85 | 0.05 | 4096 | 120 | 31 | 5 | 0 | 0.9 s | 2.4 s |
| 2 | human-like-nemo-12b | hf.co/bartowski/…:Q8_0 | 0.85 | 0.05 | 4096 | 120 | 28 | 8 | 0 | 2.1 s | 5.0 s |

## 1. hermes-3-8b

`hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:latest` — temperature 0.85,
min_p 0.05, ctx 4096, max_tokens 120, native off

| # | overheard | quip | raw | s |
|---|---|---|---|---|
| 1 | meer loves the new toy | — *(/notfunny)* | `/notfunny` | 0.4 |
| 2 | the bread is done baking | Time to toast! | `Time to toast!` | 0.9 |
| … | | | | |

## 2. human-like-nemo-12b
…
```

* Columns: the **command-stripped text** (what would reach the display) and
  the **raw output** (so `/color` runs, stray commands and formatting are
  visible while judging). A skip renders as `— *(/notfunny)*`, never blank, so
  the reader can tell “model declined” from “cell missing”.
* Cell escaping: `|` → `\|`, newlines → `<br>`, backticks wrapped around raw
  output; long raw text is truncated at ~300 chars with `…` (full text stays in
  the in-memory result; see *Deferred* for a JSON sidecar).
* A failed bank renders an `**Error:** …` block under its heading and `—` cells;
  the summary's `errors` column counts that bank's failed cells.
* Numbers are stable `#` indexes shared by the summary and every section, so the
  same line has the same row number in every model's table.
* Timestamps are UTC ISO-ish (`YYYY-MM-DD HH:MM UTC`), so a report is
  self-describing when attached to an issue.

## File & component layout

| File | Change |
|---|---|
| `src/voicebot/assets/overheard.txt` | **new** — 6 old lines + ~30 restaurant/dinner-party lines. |
| `src/voicebot/assets/jokemark-models.toml` | **new** — the matrix above. |
| `src/voicebot/jokemark.py` | **new** — `ModelSpec`, `load_matrix`, `load_lines`, `run_benchmark`, `render_markdown`. Pure, network-free, no click. |
| `src/voicebot/server.py` | add `has_model` + `pull_model` (+ the pull timeout argument). |
| `src/voicebot/config.py` | add `DEFAULT_PULL_TIMEOUT`. |
| `src/voicebot/cli.py` | add the `jokemark` command (thin: options → `load_*` / `run_benchmark` → `render_markdown` → write the file); document it in the module docstring. |
| `pyproject.toml` | explicit `include` for `src/voicebot/assets`. |
| `justfile` | `jokemark *args: install` → `poetry run voicebot jokemark {{args}}`; comment the manual workflow it replaces. |
| `README.md` | a “Model bake-off” subsection; demote `reply -f sim-overheard.txt` to a smoke example. |
| `AGENTS.md` | one row in the repo-layout table for `jokemark.py` + the assets dir. |
| `tests/test_jokemark.py` | **new** — loaders, runner, renderer, packaged-asset invariants. |
| `tests/test_cli.py` | `jokemark` CLI cases (fake client, tmp files). |
| `tests/test_server.py` | **new, small** — `has_model` / `pull_model` payload + error normalisation (monkeypatched `requests`/`OpenAI`). |

The split mirrors the existing conventions: `cli.py` stays the only click
module, `server.py` stays the only socket, and `jokemark.py` is the testable
middle — the same shape as `listen.py` (loop) vs `cli.py` (wiring).

Sketch of the core API:

```python
@dataclass(frozen=True)
class ModelSpec:
    label: str
    model: str
    temperature: float
    min_p: float
    ctx: int
    max_tokens: int
    native: bool = False
    timeout: float = DEFAULT_TIMEOUT

@dataclass(frozen=True)
class LineResult:          # one cell
    line: str
    reply: Reply | None
    elapsed_s: float
    error: str | None = None

@dataclass(frozen=True)
class ModelResult:         # one matrix entry's bank
    spec: ModelSpec
    results: tuple[LineResult, ...]
    error: str | None = None   # pull / whole-bank failure
    pulled: bool = False

def load_matrix(text: str) -> tuple[ModelSpec, ...]: ...
def load_lines(text: str, *, limit: int | None = None) -> tuple[str, ...]: ...
def run_benchmark(specs, lines, *, persona, client_factory, pull, log=log) -> list[ModelResult]: ...
def render_markdown(results, *, url, persona, corpus_name, native) -> str: ...
```

`client_factory(spec) -> ChatClient` is injected, exactly like the fake client
in `test_cli.py`, so tests never pull or chat for real.

## Phased implementation

| Phase | Work | Acceptance |
|---|---|---|
| **P1 — assets** | Write `overheard.txt` (6 + ~30 unique restaurant/dinner-party lines) and `jokemark-models.toml` (2 models + 2 sampler variants); add the `pyproject.toml` include. | `python -c` reads both via `importlib.resources`; `just build` + `unzip -l dist/touchy_voicebot-*.whl` shows both files; asset tests green. |
| **P2 — fetch primitives** | `DEFAULT_PULL_TIMEOUT`, `ChatClient.has_model` / `pull_model`, `tests/test_server.py`. | Unit tests prove the `/api/pull` payload, `:latest` matching, and `VoicebotError` normalisation; live check pulls an already-present model (fast no-op). |
| **P3 — jokemark core** | `jokemark.py` (loaders + validation, runner with the error policy, Markdown renderer) and `tests/test_jokemark.py` with a recording fake client. | All tests network-free; renderer output contains one summary row per spec, stable row numbers, skip/error markers, and escaped `|`/newlines. |
| **P4 — CLI + docs** | `voicebot jokemark` wiring, `just jokemark`, README/AGENTS updates, CLI tests (`--dry-run`, `--no-pull`, `--limit`, `-o`). | `just lint` clean; `just test` green; `voicebot jokemark --dry-run` prints 4 specs × 36 lines and writes nothing. |
| **P5 — live baseline** | Real run against the server (`just jokemark`), inspect the report, record the outcome (and, if useful, keep one report in `docs/` as the baseline the next run is compared against). | `jokemark.md` has every section filled; timings and skip rates look sane; the two known models produce recognisably different quips. |

P1–P4 are one small PR-sized change; P5 is a human-supervised live run (it
downloads models).

## Testing strategy

**Network-free (`just test`, default):**

* `load_matrix`: defaults merged per entry, derived labels, strict validation
  (unknown key, empty list, duplicate label, bad ranges, wrong version).
* `load_lines`: order preserved, blanks dropped, a duplicate line rejected
  (the report keys rows by line), `--limit` applied, packaged asset read
  through `importlib.resources`.
* `run_benchmark` with a recording fake: one result per spec/line; a
  `/notfunny` reply lands as `skip=True`; a missing model with `pull=False`
  records an error without chat calls; a mid-bank `VoicebotError` stops that
  bank only and the next spec still runs; latency is recorded (> 0).
* `render_markdown`: summary row count, section order = matrix order, raw
  commands visible, skip marker present, `|` and newlines escaped, error block
  rendered.
* Assets: `overheard.txt` has ≥ 36 non-blank unique lines, the first six equal
  `sim-overheard.txt` verbatim, every `sim-overheard.txt` line is present, the
  TOML parses, it covers `DEFAULT_MODEL`, and it still documents every tried
  candidate (retired ones live on as commented-out entries).
* CLI (`CliRunner`, `isolated_filesystem`, monkeypatched `ChatClient`):
  default run writes `jokemark.md` and pulls only the absent model;
  `--no-pull` never calls `pull_model`; `--dry-run` writes nothing;
  `--models`/`-f` overrides are honoured; a bad matrix exits 1 with a message.

**Live (opt-in, `VOICEBOT_LIVE=1`):**

* Keep it cheap: one already-pulled model × `--limit 1` through the same code
  path, asserting a reply or a skip — no pulls in CI, no multi-GB downloads.

**Manual acceptance:** `just jokemark` (2 models + 2 variants × 36 lines ≈ 144
calls) → read `jokemark.md`; confirm the summary's skip counts and medians
match what the sections show, and that the report alone is enough to pick the
next default model without consulting shell history.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Command shape | a `jokemark` **subcommand** | One command per stage, like `reply` / `models` / `listen`; the matrix owns model/sampler options, so inheriting `_LLM_OPTIONS` would be misleading. |
| Corpus location | packaged `src/voicebot/assets/` | Works from a wheel on the Pi, not just from a source checkout; `importlib.resources` avoids `__file__` path games. |
| Corpus size/shape | ~36 bare, unique, lowercase utterances | Enough to expose `/notfunny` and formatting failures; bare utterances match what STT actually produces. |
| Matrix format | TOML envelope (`version`/`[defaults]`/`[[models]]`) | Parameters per row without a CLI flag explosion; room for personas/repeats/seeds later. TOML (stdlib `tomllib`) over JSON so the hand-edited file can carry comments, and over YAML so no dependency is added. |
| Initial entries | Hermes-3 8B + Human-Like Nemo 12B, each at the default sampler and one temperature variant | Exactly what has been evaluated by hand, plus an A/B that exercises the parameter axis. |
| Pull default | on, `stream: false`, 1 h timeout | “fetch if needed” is the request; a cold 13 GB fetch must not hit the 120 s chat timeout. |
| Failure policy | abort the failing model's bank, continue the run | Keeps a down server / bad model from costing 36 timeouts, without losing the other models. |
| Output | one Markdown file, fixed default name | Directly readable/diffable and attachable; no presentation layer in the CLI beyond writing it. |
| Sampling | one sample per line, no seed | It is a vibe check (“jokemark”), not a statistically rigorous benchmark; repeats are deferred. |
| Dependencies | none new — stdlib `tomllib` / `importlib.resources` / `dataclasses` / `hashlib` + existing `click` / `requests` / `openai` | Keeps the Pi install and the wheel small; `requests` already backs the native `/api/chat` path. |

## Deferred (explicitly not in this pass)

* **JSON sidecar / `--json`** with the full untruncated replies, for later
  score-keeping or plotting.
* **`repeats` / `seed`** per matrix entry (variance and determinism runs).
* **Streaming pull progress** (Ollama `/api/pull` with `"stream": true`).
* **Concurrent models** — pointless on a single-GPU/CPU Ollama box.
* **LLM-as-judge / automatic scoring** of quips; the report is for a human.
* **`keep_alive` tuning** between matrix entries.
* **Per-entry persona** overrides (the matrix currently fixes one persona per
  run, recorded in the header).

## Risks / open questions

* **`/api/pull` body field — resolved (2026-11-17).** `{"model": …, "stream":
  false}` returned `{"status": "success"}` on the live server (Ollama 0.40.14),
  so that is the payload `pull_model` posts. `docs/rpi-deploy.md`'s older
  `"name"` snippet is historical; no fallback is needed.
* **Disk and bandwidth on the server.** Four matrix rows are only two distinct
  models (≈ 5 GB Hermes + ≈ 13 GB Nemo at Q8_0); the 1 h pull timeout and
  `--no-pull` are the mitigations, but P5 should confirm free space first.
* **Nondeterminism.** One sample per line at `temperature=0.85` means one run
  is a hint, not a verdict; the report says so and `repeats` is the follow-up
  if a close call needs settling.
* **Cold-load latency pollutes the first rows.** The first reply after a pull
  includes model load time. The per-row latency exposes it; a warm-up call per
  model is tempting but would double cost — deferred, and the median is robust.
* **Report churn in git — resolved.** `jokemark.md` is a generated artifact and
  is gitignored; point `-o` at `docs/…` when a report is worth keeping as a
  dated baseline.
* **Model availability.** HF-path pulls need the *server* to reach
  huggingface.co; a proxy-less box fails at pull time. That is recorded as a
  per-entry error, so the report still lands.
* **Markdown escaping.** Model output is arbitrary text; the renderer must
  escape `|`, newlines and backticks or it will corrupt the table. Covered by a
  test with a deliberately nasty reply.
* **Persona drift.** The report header stamps the persona text's first line +
  hash; if the persona changes between runs, the comparison is explicitly
  labelled as different rather than silently misleading.

## Relationship to the other plans / docs

* Extends [`initial.md`](initial.md) Stage 1/2 (the responder and `models`);
  the persona, sampler defaults and `/command` contract are reused unchanged.
* Complements [`audioin.md`](audioin.md): jokemark benchmarks the *responder*
  half of the “mic → STT → gate → responder” chain; it deliberately bypasses
  the gate so weak-input behaviour is visible.
* Touches [`standalone.md`](standalone.md) packaging only by adding the assets
  include to `pyproject.toml` (and the wheel-contents check).
* `sim-overheard.txt` remains the tiny demo fixture; its lines are the first
  six of the packaged corpus, enforced by a test rather than by convention.

