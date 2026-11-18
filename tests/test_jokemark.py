"""The jokemark core: matrix/corpus loaders, the run loop, the Markdown renderer.

Everything here is network-free — the benchmark is handed a fake client.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dankbot import jokemark
from dankbot.commands import Run
from dankbot.config import (
    DEFAULT_CTX,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIN_P,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
)
from dankbot.jokemark import (
    JokemarkError,
    LineResult,
    ModelResult,
    ModelSpec,
    Report,
    load_lines,
    load_matrix,
    load_report,
    playback,
    render_json,
    render_markdown,
    run_benchmark,
)
from dankbot.responder import Reply
from dankbot.server import DankbotError

FIXED_NOW = datetime(2026, 11, 17, 21, 4, tzinfo=UTC)


def _toml_value(value: object) -> str:
    """A scalar as TOML (JSON string quoting is valid TOML for what we write)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    return repr(value)


def _matrix(*models: dict, defaults: dict | None = None, version: int = 1, **top: object) -> str:
    """A model matrix in TOML — *top* becomes root keys, *defaults* its table."""
    out = [f"version = {_toml_value(version)}"]
    out += [f"{key} = {_toml_value(value)}" for key, value in top.items()]
    if defaults:
        out.append("[defaults]")
        out += [f"{key} = {_toml_value(value)}" for key, value in defaults.items()]
    for entry in models:
        out.append("[[models]]")
        out += [f"{key} = {_toml_value(value)}" for key, value in entry.items()]
    return "\n".join(out) + "\n"


def _reply(text: str = "quip", *, skip: bool = False, raw: str | None = None) -> Reply:
    return Reply(
        source="line",
        text=text,
        skip=skip,
        model="m",
        raw=text if raw is None else raw,
    )


class FakeBackend:
    """A ChatBackend that records calls and can fail on a chosen line."""

    def __init__(self, *, replies=None, available=True, fail_on=None, pull_fails=False):
        self.replies = replies or {}
        self.available = available
        self.fail_on = fail_on
        self.pull_fails = pull_fails
        self.calls: list[str] = []
        self.pull_calls: list[str] = []

    def has_model(self, name: str) -> bool:
        return self.available

    def pull_model(self, name: str) -> None:
        if self.pull_fails:
            raise DankbotError("network down")
        self.pull_calls.append(name)

    def chat(self, messages: list[dict[str, str]]) -> str:
        line = messages[-1]["content"]
        self.calls.append(line)
        if self.fail_on is not None and line == self.fail_on:
            raise DankbotError("boom")
        return self.replies.get(line, "quip")


# -- the matrix ---------------------------------------------------------------


def test_matrix_defaults_fill_in_the_sampler():
    specs = load_matrix(_matrix({"model": "hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:latest"}))
    assert len(specs) == 1
    spec = specs[0]
    assert spec.model == "hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:latest"
    assert spec.label == "hermes-3-llama-3.1-8b"
    assert spec.temperature == DEFAULT_TEMPERATURE
    assert spec.min_p == DEFAULT_MIN_P
    assert spec.ctx == DEFAULT_CTX
    assert spec.max_tokens == DEFAULT_MAX_TOKENS
    assert spec.native is False
    assert spec.timeout == DEFAULT_TIMEOUT


def test_matrix_defaults_and_the_cli_baseline_are_merged():
    text = _matrix(
        {"model": "m", "label": "x"},
        defaults={"temperature": 0.5, "native": True},
    )
    spec = load_matrix(text, native=False, timeout=30)[0]
    assert spec.temperature == 0.5
    assert spec.native is True
    assert spec.timeout == 30


def test_an_entry_overrides_the_defaults():
    text = _matrix(
        {"model": "m", "label": "x", "temperature": 0, "native": False, "timeout": 5},
        defaults={"temperature": 0.5},
    )
    spec = load_matrix(text, native=True)[0]
    assert spec.temperature == 0.0
    assert spec.native is False
    assert spec.timeout == 5.0


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (_matrix(), "non-empty"),
        (_matrix({"model": "m"}, version=2), "unsupported"),
        (_matrix({"model": "m"}, wat=1), "unknown matrix keys"),
        (_matrix({"model": ""}), "non-empty string"),
        (_matrix({"model": "m", "temp": 0.5}), "unknown keys"),
        (_matrix({"model": "m"}, {"model": "m"}), "duplicate label"),
        (_matrix({"model": "m", "temperature": -1}), "negative"),
        (_matrix({"model": "m", "min_p": 2}), "between 0 and 1"),
        (_matrix({"model": "m", "ctx": 0}), "positive"),
        (_matrix({"model": "m", "max_tokens": 0}), "positive"),
        (_matrix({"model": "m", "native": "yes"}), "true or false"),
        (_matrix({"model": "m"}, defaults={"wat": 1}), "unknown keys"),
    ],
)
def test_matrix_validation_is_loud(text, match):
    with pytest.raises(JokemarkError, match=match):
        load_matrix(text)


def test_invalid_toml_is_rejected():
    with pytest.raises(JokemarkError, match="not valid TOML"):
        load_matrix("version = = 1")
    with pytest.raises(JokemarkError, match="not valid TOML"):
        load_matrix("{nope")


# -- the corpus ---------------------------------------------------------------


def test_load_lines_trims_drops_blanks_and_keeps_order():
    assert load_lines("a\n\n b \nc\n") == ("a", "b", "c")
    assert load_lines("a\nb\nc", limit=2) == ("a", "b")


def test_load_lines_rejects_duplicates_empties_and_bad_limits():
    with pytest.raises(JokemarkError, match="duplicate"):
        load_lines("a\na\n")
    with pytest.raises(JokemarkError, match="empty"):
        load_lines(" \n")
    with pytest.raises(JokemarkError, match="--limit"):
        load_lines("a\n", limit=0)


def test_the_packaged_corpus_extends_the_smoke_file():
    corpus = load_lines(jokemark.packaged_text(jokemark.CORPUS_ASSET))
    smoke_path = Path(__file__).resolve().parents[1] / "sim-overheard.txt"
    smoke = load_lines(smoke_path.read_text(encoding="utf-8"))
    assert corpus[: len(smoke)] == smoke
    assert len(corpus) >= 36
    assert all(line == line.lower() for line in corpus)


def test_the_packaged_matrix_keeps_the_previously_evaluated_models():
    specs = load_matrix(jokemark.packaged_text(jokemark.MATRIX_ASSET))
    models = {spec.model for spec in specs}
    assert any("Hermes-3-Llama-3.1-8B" in model for model in models)
    assert any("Human-Like-Mistral-Nemo" in model for model in models)
    assert any(spec.temperature == 0.0 for spec in specs)


def test_the_packaged_matrix_still_documents_every_tried_candidate():
    # A candidate that scored badly stays in the file as a commented-out
    # `[[models]]` block with a verdict next to it, so assert on the raw text:
    # retiring a model must not erase the record of having tried it.
    text = jokemark.packaged_text(jokemark.MATRIX_ASSET)
    for repo in (
        "TheDrummer/Rocinante-X-12B-v1-GGUF",
        "mradermacher/gemma3-4b-it-comedy-v2-GGUF",
        "unsloth/gemma-4-12b-it-GGUF",
        "mradermacher/Qwen3.5-9B-Claude-3xMerge-UNCENSORED-heretic-i1-GGUF",
    ):
        assert repo in text


def test_the_packaged_matrix_covers_the_default_model():
    # The default must stay a bake-off candidate, so changing one without the
    # other (or a typo in the model path) is caught here.
    specs = load_matrix(jokemark.packaged_text(jokemark.MATRIX_ASSET))
    assert DEFAULT_MODEL in {spec.model for spec in specs}


# -- the run loop -------------------------------------------------------------


def _run(backend, lines=("hello", "bye"), *, specs=None, pull=False, on_result=None, display=None):
    return run_benchmark(
        specs or (ModelSpec(label="a", model="m"),),
        lines,
        persona="PERSONA",
        client_factory=lambda _spec: backend,
        pull=pull,
        on_result=on_result,
        display=display,
    )


def test_a_bank_records_one_row_per_line():
    backend = FakeBackend(replies={"hello": "hi"})
    (bank,) = _run(backend)
    assert bank.pulled is False
    assert bank.error is None
    assert [row.line for row in bank.results] == ["hello", "bye"]
    assert bank.results[0].reply.text == "hi"
    assert all(row.reply is not None for row in bank.results)
    assert all(row.elapsed_s >= 0 for row in bank.results)
    assert backend.calls == ["hello", "bye"]


def test_a_notfunny_reply_is_a_skip():
    (bank,) = _run(FakeBackend(replies={"hello": "/notfunny"}), lines=("hello",))
    assert bank.results[0].reply.skip is True
    assert bank.results[0].reply.text == ""


# -- the optional display ------------------------------------------------------


def test_the_display_sink_sees_each_reply_but_not_a_skip():
    """The same parsed runs ``listen`` shows — commands stripped, colours kept."""
    shown: list[tuple] = []
    (bank,) = _run(
        FakeBackend(replies={"hello": "/color/red hi", "bye": "/notfunny"}),
        display=shown.append,
    )
    assert len(bank.results) == 2
    assert ["".join(run.text for run in runs) for runs in shown] == ["hi"]
    assert shown[0][0].colour == 0xFF0000  # /color/red


def test_a_broken_display_sink_does_not_stop_the_benchmark():
    def boom(runs):
        raise RuntimeError("no pad")

    (bank,) = _run(FakeBackend(), display=boom)
    assert len(bank.results) == 2
    assert all(row.reply is not None for row in bank.results)


def test_no_display_sink_is_the_default():
    (bank,) = _run(FakeBackend())
    assert len(bank.results) == 2


def test_a_missing_model_is_pulled_first():
    backend = FakeBackend(available=False)
    (bank,) = _run(backend, pull=True)
    assert backend.pull_calls == ["m"]
    assert bank.pulled is True
    assert len(bank.results) == 2


def test_no_pull_skips_a_missing_model_without_chatting():
    backend = FakeBackend(available=False)
    (bank,) = _run(backend, pull=False)
    assert backend.pull_calls == []
    assert backend.calls == []
    assert bank.error == "not pulled (--no-pull)"
    assert bank.results == ()


def test_a_failed_pull_is_recorded_not_raised():
    (bank,) = _run(FakeBackend(available=False, pull_fails=True), pull=True)
    assert bank.error == "network down"
    assert bank.results == ()


def test_a_mid_bank_failure_stops_only_that_bank():
    first = FakeBackend(fail_on="bad")
    second = FakeBackend()
    backends = {"a": first, "b": second}
    seen: list[ModelResult] = []
    specs = (ModelSpec(label="a", model="m1"), ModelSpec(label="b", model="m2"))
    results = run_benchmark(
        specs,
        ("ok", "bad", "never"),
        persona="P",
        client_factory=lambda spec: backends[spec.label],
        pull=False,
        on_result=seen.append,
    )
    assert [model.spec.label for model in seen] == ["a", "b"]
    assert [row.line for row in results[0].results] == ["ok", "bad"]
    assert results[0].results[1].error == "boom"
    assert results[0].error == "boom"
    assert [row.line for row in results[1].results] == ["ok", "bad", "never"]
    assert results[1].error is None


# -- the report ---------------------------------------------------------------


def _render(results, lines, *, native=False):
    return render_markdown(
        results,
        lines,
        url="http://ollama:11434",
        persona="PERSONA\nsecond line",
        corpus_name="overheard.txt",
        native=native,
        now=FIXED_NOW,
    )


def test_render_writes_the_header_summary_and_one_section_per_model():
    spec = ModelSpec(label="m1", model="org/model:latest")
    results = [
        ModelResult(
            spec=spec,
            results=(
                LineResult("a|b", _reply("quip", raw="quip\nraw"), 0.5),
                LineResult("c", _reply("", skip=True, raw="/notfunny"), 0.25),
            ),
        )
    ]
    md = _render(results, ("a|b", "c", "d"))
    assert md.startswith("# jokemark — 2026-11-17 21:04 UTC\n")
    assert "`overheard.txt` (3 lines)" in md
    assert "persona `PERSONA`" in md
    assert "native: no" in md
    assert "## 1. m1" in md
    assert "a\\|b" in md
    assert "quip<br>raw" in md
    assert "— *(/notfunny)*" in md
    assert "| 3 | d | — | — | — |" in md

    summary = next(line for line in md.splitlines() if line.startswith("| 1 | m1 |"))
    cells = [cell.strip() for cell in summary.strip("|").split("|")]
    assert cells[7:10] == ["1", "1", "1"]


def test_render_marks_errors_and_lines_never_attempted():
    spec = ModelSpec(label="bad", model="m")
    results = [
        ModelResult(
            spec=spec,
            results=(LineResult("a", None, 0.2, error="boom"),),
            error="boom",
        )
    ]
    md = _render(results, ("a", "b"), native=True)
    assert "**Error:** boom" in md
    assert "— *(error)*" in md
    assert "`boom`" in md
    assert "| 2 | b | — | — | — |" in md
    assert "native: yes" in md

    summary = next(line for line in md.splitlines() if line.startswith("| 1 | bad |"))
    cells = [cell.strip() for cell in summary.strip("|").split("|")]
    assert cells[7:10] == ["0", "0", "2"]


def test_render_truncates_long_raw_output_and_escapes_backticks():
    spec = ModelSpec(label="m", model="m")
    raw = "`" + "x" * 400
    results = [ModelResult(spec=spec, results=(LineResult("line", _reply(raw=raw), 0.1),))]
    md = _render(results, ("line",))
    assert "x" * 400 not in md
    assert "…" in md


def test_render_notes_a_fresh_pull():
    results = [ModelResult(spec=ModelSpec(label="m", model="m"), results=(), pulled=True)]
    md = _render(results, ("a",))
    assert "pulled just now" in md
    assert "| 1 | a | — | — | — |" in md


# -- the JSON report ----------------------------------------------------------


def _render_json(results, lines, *, native=False):
    return render_json(
        results,
        lines,
        url="http://ollama:11434",
        persona="PERSONA\nsecond line",
        corpus_name="overheard.txt",
        native=native,
        now=FIXED_NOW,
    )


def _coloured(text="quip", *, skip=False, raw=None, colour=0xFFFFFF):
    return Reply(
        source="line",
        text=text,
        skip=skip,
        model="m",
        runs=() if skip else (Run(text, colour),),
        raw=text if raw is None else raw,
    )


def _run_with_every_outcome():
    """One bank with a coloured reply, a /notfunny skip and a failed line."""
    spec = ModelSpec(label="m1", model="org/model:latest", temperature=0.5)
    return [
        ModelResult(
            spec=spec,
            results=(
                LineResult(
                    "a|b",
                    _coloured("Danger ahead", raw="/color/red Danger ahead", colour=0xFF0000),
                    0.5,
                ),
                LineResult("c", _coloured("", skip=True, raw="/notfunny"), 0.25),
                LineResult("d", None, 0.1, error="boom"),
            ),
            pulled=True,
        )
    ]


def test_render_json_keeps_the_same_run_machine_readable():
    long_raw = "/color/red " + "x" * 400
    results = _run_with_every_outcome()
    results[0] = ModelResult(
        spec=results[0].spec,
        results=(
            LineResult("a|b", _coloured("Danger ahead", raw=long_raw, colour=0xFF0000), 0.5),
            *results[0].results[1:],
        ),
        pulled=True,
    )
    data = json.loads(_render_json(results, ("a|b", "c", "d"), native=True))

    assert data["version"] == jokemark.REPORT_VERSION
    assert data["generated"] == "2026-11-17T21:04:00+00:00"
    assert data["url"] == "http://ollama:11434"
    assert data["native"] is True
    assert data["persona"]["snippet"] == "PERSONA"
    assert data["persona"]["text"] == "PERSONA\nsecond line"
    assert data["corpus"] == {"name": "overheard.txt", "lines": ["a|b", "c", "d"]}

    (model,) = data["models"]
    assert model["label"] == "m1"
    assert model["model"] == "org/model:latest"
    assert model["temperature"] == 0.5
    assert model["pulled"] is True
    reply, skip, failed = model["results"]
    assert (reply["line"], reply["reply"], reply["skip"]) == ("a|b", "Danger ahead", False)
    assert reply["raw"] == long_raw  # untruncated, unlike the Markdown table
    assert reply["runs"] == [{"text": "Danger ahead", "colour": 0xFF0000}]
    assert reply["elapsed_s"] == 0.5
    assert (skip["reply"], skip["skip"], skip["raw"]) == ("", True, "/notfunny")
    assert (failed["reply"], failed["error"]) == (None, "boom")


def test_a_report_round_trips_through_load_report():
    original = _run_with_every_outcome()
    report = load_report(_render_json(original, ("a|b", "c", "d")))

    assert report.lines == ("a|b", "c", "d")
    assert report.url == "http://ollama:11434"
    assert report.corpus_name == "overheard.txt"
    assert report.persona == "PERSONA\nsecond line"
    assert report.native is False
    assert report.generated == "2026-11-17T21:04:00+00:00"

    (model,) = report.models
    assert model.spec == original[0].spec
    assert model.pulled is True
    reply, skip, failed = model.results
    assert reply.line == "a|b"
    assert reply.reply.text == "Danger ahead"
    assert reply.reply.runs == (Run("Danger ahead", 0xFF0000),)
    assert reply.reply.raw == "/color/red Danger ahead"
    assert skip.reply.skip is True and skip.reply.text == ""
    assert failed.reply is None and failed.error == "boom"


def test_a_report_without_runs_keeps_the_text_visible():
    payload = {
        "version": jokemark.REPORT_VERSION,
        "models": [{"label": "m", "model": "m", "results": [{"line": "a", "reply": "hi"}]}],
        "corpus": {"lines": ["a"]},
        "persona": "P",
    }
    (model,) = load_report(json.dumps(payload)).models
    assert model.results[0].reply.runs == (Run("hi"),)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("not json", "not valid JSON"),
        (json.dumps([]), "JSON object"),
        (
            json.dumps({"version": 2, "models": [{"model": "m"}], "corpus": {"lines": []}}),
            "version",
        ),
        (json.dumps({"version": 1, "models": [], "corpus": {"lines": []}}), "non-empty"),
        (json.dumps({"version": 1, "models": [{"model": "m"}], "corpus": {}}), "corpus.lines"),
        (
            json.dumps(
                {
                    "version": 1,
                    "models": [{"model": "m"}],
                    "corpus": {"lines": ["a"]},
                    "persona": 3,
                }
            ),
            "persona",
        ),
    ],
)
def test_load_report_rejects_a_malformed_file(payload, match):
    with pytest.raises(JokemarkError, match=match):
        load_report(payload)


# -- playback -----------------------------------------------------------------


def _playback_report() -> Report:
    """A saved run with a reply, a skip, a second reply and an error row."""
    return Report(
        models=(
            ModelResult(
                spec=ModelSpec(label="m1", model="m"),
                results=(
                    LineResult("a", _coloured("Danger ahead"), 0.4),
                    LineResult("b", _coloured("", skip=True, raw="/notfunny"), 0.2),
                    LineResult("c", _coloured("quiet"), 0.3),
                    LineResult("d", None, 0.1, error="boom"),
                ),
            ),
        ),
        lines=("a", "b", "c", "d"),
        url="http://ollama:11434",
        corpus_name="overheard.txt",
        persona="P",
    )


def test_playback_draws_each_reply_and_pauses_only_when_it_drew():
    shown: list[list[str]] = []
    pauses: list[float] = []
    entries: list[tuple[str, str]] = []

    def display(runs):
        shown.append([run.text for run in runs])
        return len(shown) == 1  # the pad has gone away by the second quip

    replayed = playback(
        _playback_report(),
        emit=lambda spec, row: entries.append((spec.label, row.line)),
        display=display,
        sleep=pauses.append,
    )

    assert replayed == 2  # the skip and the error are not replies
    assert entries == [("m1", "a"), ("m1", "b"), ("m1", "c"), ("m1", "d")]
    assert shown == [["Danger ahead"], ["quiet"]]
    assert pauses == [jokemark.PLAYBACK_PAUSE_S]  # only the draw that landed


def test_playback_without_a_pad_pauses_nothing():
    pauses: list[float] = []
    replayed = playback(_playback_report(), sleep=pauses.append)
    assert replayed == 2
    assert pauses == []
