"""The jokemark core: matrix/corpus loaders, the run loop, the Markdown renderer.

Everything here is network-free — the benchmark is handed a fake client.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from voicebot import jokemark
from voicebot.config import (
    DEFAULT_CTX,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIN_P,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
)
from voicebot.jokemark import (
    JokemarkError,
    LineResult,
    ModelResult,
    ModelSpec,
    load_lines,
    load_matrix,
    render_markdown,
    run_benchmark,
)
from voicebot.responder import Reply
from voicebot.server import VoicebotError

FIXED_NOW = datetime(2026, 11, 17, 21, 4, tzinfo=UTC)


def _matrix(*models: dict, **extra) -> str:
    data = {"version": 1, "models": list(models)}
    data.update(extra)
    return json.dumps(data)


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
            raise VoicebotError("network down")
        self.pull_calls.append(name)

    def chat(self, messages: list[dict[str, str]]) -> str:
        line = messages[-1]["content"]
        self.calls.append(line)
        if self.fail_on is not None and line == self.fail_on:
            raise VoicebotError("boom")
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
    text = json.dumps(
        {
            "version": 1,
            "defaults": {"temperature": 0.5, "native": True},
            "models": [{"model": "m", "label": "x"}],
        }
    )
    spec = load_matrix(text, native=False, timeout=30)[0]
    assert spec.temperature == 0.5
    assert spec.native is True
    assert spec.timeout == 30


def test_an_entry_overrides_the_defaults():
    text = json.dumps(
        {
            "version": 1,
            "defaults": {"temperature": 0.5},
            "models": [
                {"model": "m", "label": "x", "temperature": 0, "native": False, "timeout": 5}
            ],
        }
    )
    spec = load_matrix(text, native=True)[0]
    assert spec.temperature == 0.0
    assert spec.native is False
    assert spec.timeout == 5.0


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"version": 1, "models": []}, "non-empty"),
        ({"version": 2, "models": [{"model": "m"}]}, "unsupported"),
        ({"version": 1, "wat": 1, "models": [{"model": "m"}]}, "unknown matrix keys"),
        ({"version": 1, "models": [{"model": ""}]}, "non-empty string"),
        ({"version": 1, "models": [{"model": "m", "temp": 0.5}]}, "unknown keys"),
        ({"version": 1, "models": [{"model": "m"}, {"model": "m"}]}, "duplicate label"),
        ({"version": 1, "models": [{"model": "m", "temperature": -1}]}, "negative"),
        ({"version": 1, "models": [{"model": "m", "min_p": 2}]}, "between 0 and 1"),
        ({"version": 1, "models": [{"model": "m", "ctx": 0}]}, "positive"),
        ({"version": 1, "models": [{"model": "m", "max_tokens": 0}]}, "positive"),
        ({"version": 1, "models": [{"model": "m", "native": "yes"}]}, "true or false"),
    ],
)
def test_matrix_validation_is_loud(data, match):
    with pytest.raises(JokemarkError, match=match):
        load_matrix(json.dumps(data))


def test_not_json_and_not_an_object_are_rejected():
    with pytest.raises(JokemarkError, match="not valid JSON"):
        load_matrix("{nope")
    with pytest.raises(JokemarkError, match="JSON object"):
        load_matrix("[]")


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


def test_the_packaged_matrix_adds_smaller_untried_candidates():
    specs = load_matrix(jokemark.packaged_text(jokemark.MATRIX_ASSET))
    models = {spec.model for spec in specs}
    for repo in (
        "TheDrummer/Rocinante-X-12B-v1-GGUF",
        "mradermacher/gemma3-4b-it-comedy-v2-GGUF",
        "unsloth/gemma-4-12b-it-GGUF",
        "mradermacher/Qwen3.5-9B-Claude-3xMerge-UNCENSORED-heretic-i1-GGUF",
    ):
        assert any(repo in model for model in models)
    # All four new candidates are Q4_K_M — the quant that fits a 16 GB Pi 5.
    assert len([spec for spec in specs if "Q4_K_M" in spec.model]) == 4


# -- the run loop -------------------------------------------------------------


def _run(backend, lines=("hello", "bye"), *, specs=None, pull=False, on_result=None):
    return run_benchmark(
        specs or (ModelSpec(label="a", model="m"),),
        lines,
        persona="PERSONA",
        client_factory=lambda _spec: backend,
        pull=pull,
        on_result=on_result,
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
