"""CLI behaviour with a monkeypatched ChatClient — never touches the network."""

from __future__ import annotations

import json
from typing import ClassVar

from click.testing import CliRunner

from voicebot import cli


class FakeClient:
    """Stand-in for ``voicebot.server.ChatClient`` keyed by user line."""

    replies: ClassVar[dict[str, str]] = {}
    #: ``None`` means "every model is already pulled"; a set restricts it.
    available: ClassVar[set[str] | None] = None
    pulled: ClassVar[list[str]] = []

    def __init__(self, settings):
        self.model = settings.model

    def chat(self, messages):
        return self.replies.get(messages[-1]["content"], "Time to toast!")

    def list_models(self):
        return ["hermes", "nemo"]

    def has_model(self, name):
        return FakeClient.available is None or name in FakeClient.available

    def pull_model(self, name):
        FakeClient.pulled.append(name)


class FakeDisplay:
    """Stand-in for ``voicebot.display.PadDisplay`` — never touches hardware."""

    instances: ClassVar[list[FakeDisplay]] = []

    def __init__(self, screen_name=None, clear_after=None, display_mode=None):
        self.screen_name = screen_name
        self.clear_after = clear_after
        self.display_mode = display_mode
        self.started = False
        self.closed = False
        self.shown: list[tuple] = []
        FakeDisplay.instances.append(self)

    def start(self):
        self.started = True
        return True

    def show(self, runs):
        self.shown.append(runs)
        return True

    def close(self):
        self.closed = True


def _use_fake(monkeypatch, replies=None, client=None):
    FakeClient.replies = replies or {}
    FakeClient.available = None
    FakeClient.pulled = []
    FakeDisplay.instances = []
    monkeypatch.setattr(cli, "ChatClient", client or FakeClient)
    monkeypatch.setattr(cli, "PadDisplay", FakeDisplay)


def test_reply_line(monkeypatch):
    _use_fake(monkeypatch, {"the bread is done baking": "Time to toast!"})
    result = CliRunner().invoke(cli.main, ["reply", "--line", "the bread is done baking"])
    assert result.exit_code == 0
    assert result.output.strip() == "Time to toast!"


def test_reply_suppresses_notfunny(monkeypatch):
    _use_fake(monkeypatch, {"meer loves the new toy": "/notfunny"})
    result = CliRunner().invoke(cli.main, ["reply", "--line", "meer loves the new toy"])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_reply_strips_commands_from_the_printed_text(monkeypatch):
    _use_fake(monkeypatch, {"the light is red": "/color/red Danger ahead"})
    result = CliRunner().invoke(cli.main, ["reply", "--line", "the light is red"])
    assert result.exit_code == 0
    assert result.output.strip() == "Danger ahead"


def test_reply_json_is_structural(monkeypatch):
    _use_fake(monkeypatch, {"x": "/notfunny"})
    result = CliRunner().invoke(cli.main, ["reply", "--line", "x", "--json"])
    assert json.loads(result.output) == {
        "input": "x",
        "reply": "",
        "skip": True,
        "model": cli.DEFAULT_MODEL,
        "raw": "/notfunny",
    }


def test_reply_reads_lines_from_file(monkeypatch, tmp_path):
    _use_fake(monkeypatch, {"a": "quip a", "b": "/notfunny", "c": "quip c"})
    lines = tmp_path / "overheard.txt"
    lines.write_text("a\n\nb\nc\n")  # blank line is dropped
    result = CliRunner().invoke(cli.main, ["reply", "-f", str(lines)])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["quip a", "quip c"]


def test_reply_reads_stdin(monkeypatch):
    _use_fake(monkeypatch, {"a": "quip a"})
    result = CliRunner().invoke(cli.main, ["reply"], input="a\n")
    assert result.output.splitlines() == ["quip a"]


def test_models_lists_server_models(monkeypatch):
    _use_fake(monkeypatch)
    result = CliRunner().invoke(cli.main, ["models"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["hermes", "nemo"]


def test_version_option():
    result = CliRunner().invoke(cli.main, ["--version"])
    assert "voicebot" in result.output


def test_listen_fake_replays_utterances(monkeypatch, tmp_path):
    _use_fake(monkeypatch, {"the bread is done baking": "Time to toast!"})
    lines = tmp_path / "overheard.txt"
    lines.write_text("the bread is done baking\nuh\n")
    result = CliRunner().invoke(cli.main, ["listen", "--fake", str(lines), "--json"])
    assert result.exit_code == 0
    events = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert [e["event"] for e in events].count("heard") == 2
    reply = next(e for e in events if e["event"] == "reply")
    assert reply["heard"] == "the bread is done baking"
    assert reply["reply"] == "Time to toast!"
    skip = next(e for e in events if e["event"] == "skip")
    assert skip["reason"] == "too_short"


def test_listen_fake_never_asks_about_short_utterances(monkeypatch, tmp_path):
    asked: list[str] = []

    class RecordingClient(FakeClient):
        def chat(self, messages):
            asked.append(messages[-1]["content"])
            return "quip"

    _use_fake(monkeypatch, client=RecordingClient)
    lines = tmp_path / "overheard.txt"
    lines.write_text("uh\nhmm yeah\n")
    result = CliRunner().invoke(cli.main, ["listen", "--fake", str(lines), "--json"])
    assert result.exit_code == 0
    events = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert events and all(e["event"] in ("heard", "skip") for e in events)
    assert not any(e["event"] == "reply" for e in events)
    assert asked == []


def test_listen_scrolls_each_quip_on_the_pad(monkeypatch, tmp_path):
    """The pad is driven by default — no flag to pass."""
    _use_fake(monkeypatch, {"the bread is done baking": "/color/red Time to toast!"})
    lines = tmp_path / "overheard.txt"
    lines.write_text("the bread is done baking\nuh\n")
    result = CliRunner().invoke(cli.main, ["listen", "--fake", str(lines)])
    assert result.exit_code == 0
    assert len(FakeDisplay.instances) == 1
    display = FakeDisplay.instances[0]
    assert display.started and display.closed
    # Only the reply reaches the pad; the skipped utterance does not.
    assert [[run.text for run in runs] for runs in display.shown] == [["Time to toast!"]]
    assert display.shown[0][0].colour == 0xFF0000  # the /color/red run


def test_listen_passes_the_display_mode_to_the_pad(monkeypatch, tmp_path):
    _use_fake(monkeypatch, {"the bread is done baking": "Time to toast!"})
    lines = tmp_path / "overheard.txt"
    lines.write_text("the bread is done baking\n")
    result = CliRunner().invoke(
        cli.main, ["listen", "--fake", str(lines), "--display-mode", "multiline"]
    )
    assert result.exit_code == 0
    assert FakeDisplay.instances[0].display_mode == "multiline"


def test_listen_defaults_the_display_mode_to_auto(monkeypatch, tmp_path):
    _use_fake(monkeypatch, {"the bread is done baking": "Time to toast!"})
    lines = tmp_path / "overheard.txt"
    lines.write_text("the bread is done baking\n")
    result = CliRunner().invoke(cli.main, ["listen", "--fake", str(lines)])
    assert result.exit_code == 0
    assert FakeDisplay.instances[0].display_mode == "auto"


def test_an_unknown_display_mode_is_rejected(monkeypatch, tmp_path):
    _use_fake(monkeypatch, {"the bread is done baking": "Time to toast!"})
    lines = tmp_path / "overheard.txt"
    lines.write_text("the bread is done baking\n")
    result = CliRunner().invoke(
        cli.main, ["listen", "--fake", str(lines), "--display-mode", "nope"]
    )
    assert result.exit_code != 0
    assert FakeDisplay.instances == []


def test_listen_logs_heard_and_reply(monkeypatch, tmp_path, caplog):
    import logging

    _use_fake(monkeypatch, {"the bread is done baking": "Time to toast!"})
    lines = tmp_path / "overheard.txt"
    lines.write_text("the bread is done baking\n")
    with caplog.at_level(logging.INFO, logger="voicebot"):
        result = CliRunner().invoke(cli.main, ["listen", "--fake", str(lines)])
    assert result.exit_code == 0
    assert "HEARD: the bread is done baking" in caplog.text
    assert "REPLY: Time to toast!" in caplog.text


# -- jokemark -----------------------------------------------------------------


def _all_output(result) -> str:
    """stdout + stderr (click 8.1 mixes them into .output; 8.2 splits them)."""
    return result.output + getattr(result, "stderr", "")


def _matrix(path, models):
    """A minimal TOML matrix (`version` + one `[[models]]` table per entry)."""
    out = ["version = 1"]
    for model in models:
        out.append("[[models]]")
        out += [
            f"{key} = {json.dumps(value) if isinstance(value, str) else value}"
            for key, value in model.items()
        ]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def _corpus(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_jokemark_dry_run_prints_the_matrix(monkeypatch, tmp_path):
    _use_fake(monkeypatch)
    matrix = _matrix(
        tmp_path / "models.toml",
        [
            {"model": "m1", "label": "one"},
            {"model": "m2", "label": "two", "temperature": 0.0},
        ],
    )
    corpus = _corpus(tmp_path / "overheard.txt", ["hello there", "bye now"])
    out = tmp_path / "report.md"
    result = CliRunner().invoke(
        cli.main,
        ["jokemark", "--models", str(matrix), "-f", str(corpus), "-o", str(out), "--dry-run"],
    )
    assert result.exit_code == 0
    assert "2 models × 2 lines" in result.output
    assert "one — m1" in result.output
    assert "temp 0" in result.output
    assert not out.exists()
    assert FakeDisplay.instances == []  # --dry-run never reaches for the pad


def test_jokemark_writes_a_report_and_pulls_only_the_missing_model(monkeypatch, tmp_path):
    _use_fake(monkeypatch, {"hello there": "hi", "bye now": "/notfunny"})
    FakeClient.available = {"m1"}
    matrix = _matrix(
        tmp_path / "models.toml",
        [{"model": "m1", "label": "one"}, {"model": "m2", "label": "two"}],
    )
    corpus = _corpus(tmp_path / "overheard.txt", ["hello there", "bye now"])
    out = tmp_path / "report.md"
    result = CliRunner().invoke(
        cli.main, ["jokemark", "--models", str(matrix), "-f", str(corpus), "-o", str(out)]
    )
    assert result.exit_code == 0
    assert FakeClient.pulled == ["m2"]
    assert f"wrote {out}" in result.output
    report = out.read_text(encoding="utf-8")
    assert "## 1. one" in report
    assert "## 2. two" in report
    assert "— *(/notfunny)*" in report


def test_jokemark_no_pull_records_the_missing_model(monkeypatch, tmp_path):
    _use_fake(monkeypatch)
    FakeClient.available = set()
    matrix = _matrix(tmp_path / "models.toml", [{"model": "m1", "label": "one"}])
    corpus = _corpus(tmp_path / "overheard.txt", ["hello there"])
    out = tmp_path / "report.md"
    result = CliRunner().invoke(
        cli.main,
        ["jokemark", "--models", str(matrix), "-f", str(corpus), "-o", str(out), "--no-pull"],
    )
    assert result.exit_code == 1  # nothing produced a reply
    assert FakeClient.pulled == []
    assert "not pulled" in out.read_text(encoding="utf-8")


def test_jokemark_rejects_a_bad_matrix(monkeypatch, tmp_path):
    _use_fake(monkeypatch)
    matrix = tmp_path / "models.toml"
    matrix.write_text("version = 1\n", encoding="utf-8")
    corpus = _corpus(tmp_path / "overheard.txt", ["hello there"])
    result = CliRunner().invoke(
        cli.main,
        ["jokemark", "--models", str(matrix), "-f", str(corpus), "-o", str(tmp_path / "r.md")],
    )
    assert result.exit_code == 1
    assert "matrix" in _all_output(result)


def test_jokemark_dry_run_reads_the_packaged_assets(monkeypatch, tmp_path):
    _use_fake(monkeypatch)
    result = CliRunner().invoke(
        cli.main, ["jokemark", "--dry-run", "--limit", "3", "-o", str(tmp_path / "r.md")]
    )
    assert result.exit_code == 0
    assert "models × 3 lines" in result.output
    assert "hermes-3-8b" in result.output


def test_jokemark_shows_each_reply_on_an_attached_pad(monkeypatch, tmp_path):
    _use_fake(monkeypatch, {"hello there": "/color/red hi", "bye now": "/notfunny"})
    matrix = _matrix(tmp_path / "models.toml", [{"model": "m1", "label": "one"}])
    corpus = _corpus(tmp_path / "overheard.txt", ["hello there", "bye now"])
    out = tmp_path / "report.md"
    result = CliRunner().invoke(
        cli.main,
        [
            "jokemark",
            "--models",
            str(matrix),
            "-f",
            str(corpus),
            "-o",
            str(out),
            "--display-mode",
            "multiline",
        ],
    )
    assert result.exit_code == 0
    display = FakeDisplay.instances[0]
    assert display.started and display.closed
    assert display.display_mode == "multiline"
    # The same parsed runs `listen` shows: commands stripped, /notfunny skipped.
    assert [[run.text for run in runs] for runs in display.shown] == [["hi"]]
    assert display.shown[0][0].colour == 0xFF0000  # the /color/red run


def test_jokemark_still_runs_when_no_pad_is_attached(monkeypatch, tmp_path):
    _use_fake(monkeypatch, {"hello there": "hi"})

    class NoPadDisplay(FakeDisplay):
        """A display whose one-off probe finds no touchy-pad."""

        def start(self):
            self.started = True
            return False

    monkeypatch.setattr(cli, "PadDisplay", NoPadDisplay)
    matrix = _matrix(tmp_path / "models.toml", [{"model": "m1", "label": "one"}])
    corpus = _corpus(tmp_path / "overheard.txt", ["hello there"])
    out = tmp_path / "report.md"
    result = CliRunner().invoke(
        cli.main, ["jokemark", "--models", str(matrix), "-f", str(corpus), "-o", str(out)]
    )
    assert result.exit_code == 0
    display = NoPadDisplay.instances[0]
    assert display.started and display.closed
    assert display.shown == []  # nothing was ever sent to a pad
    assert out.exists()


def _bake(monkeypatch, tmp_path, replies):
    """Run one model over *replies*' keys and return the JSON sidecar path."""
    _use_fake(monkeypatch, replies)
    matrix = _matrix(tmp_path / "models.toml", [{"model": "m1", "label": "one"}])
    corpus = _corpus(tmp_path / "overheard.txt", list(replies))
    result = CliRunner().invoke(
        cli.main,
        [
            "jokemark",
            "--models",
            str(matrix),
            "-f",
            str(corpus),
            "-o",
            str(tmp_path / "report.md"),
        ],
    )
    assert result.exit_code == 0, result.output
    return tmp_path / "report.json"


def test_jokemark_writes_a_json_sidecar_next_to_the_report(monkeypatch, tmp_path):
    sidecar = _bake(monkeypatch, tmp_path, {"hello there": "hi", "bye now": "/notfunny"})
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["corpus"]["lines"] == ["hello there", "bye now"]
    (model,) = data["models"]
    assert model["label"] == "one"
    assert [(row["reply"], row["skip"]) for row in model["results"]] == [
        ("hi", False),
        ("", True),
    ]


def test_jokemark_refuses_to_overwrite_the_sidecar_with_the_report(monkeypatch, tmp_path):
    _use_fake(monkeypatch)
    matrix = _matrix(tmp_path / "models.toml", [{"model": "m1", "label": "one"}])
    corpus = _corpus(tmp_path / "overheard.txt", ["hello there"])
    result = CliRunner().invoke(
        cli.main,
        [
            "jokemark",
            "--models",
            str(matrix),
            "-f",
            str(corpus),
            "-o",
            str(tmp_path / "report.json"),
        ],
    )
    assert result.exit_code == 1
    assert "sidecar" in _all_output(result)


def test_jokemark_playback_replays_without_the_llm(monkeypatch, tmp_path, caplog):
    import logging

    from voicebot.jokemark import PLAYBACK_PAUSE_S

    sidecar = _bake(monkeypatch, tmp_path, {"hello there": "/color/red hi", "bye now": "/notfunny"})

    class NoClient:
        """A ChatClient that must never be constructed by playback."""

        def __init__(self, settings):
            raise AssertionError("playback must not talk to the LLM")

    monkeypatch.setattr(cli, "ChatClient", NoClient)
    FakeDisplay.instances = []
    pauses: list[float] = []
    monkeypatch.setattr("voicebot.jokemark.time.sleep", pauses.append)

    with caplog.at_level(logging.INFO, logger="voicebot.cli"):
        result = CliRunner().invoke(cli.main, ["jokemark", "--playback", str(sidecar)])

    assert result.exit_code == 0
    assert "replayed 1 replies" in result.output
    assert "no model calls" in result.output
    display = FakeDisplay.instances[0]
    assert display.started and display.closed
    assert [[run.text for run in runs] for runs in display.shown] == [["hi"]]
    assert display.shown[0][0].colour == 0xFF0000  # the saved /color/red run
    assert pauses == [PLAYBACK_PAUSE_S]
    assert "REPLY: hi" in caplog.text
    assert "SKIP(/notfunny)" in caplog.text
    assert "hello there" in caplog.text


def test_jokemark_playback_without_a_pad_does_not_pause(monkeypatch, tmp_path):
    sidecar = _bake(monkeypatch, tmp_path, {"hello there": "hi"})

    class NoPadDisplay(FakeDisplay):
        """A display whose one-off probe finds no touchy-pad."""

        def start(self):
            self.started = True
            return False

    monkeypatch.setattr(cli, "PadDisplay", NoPadDisplay)
    FakeDisplay.instances = []  # forget the bake's display; keep only playback's
    pauses: list[float] = []
    monkeypatch.setattr("voicebot.jokemark.time.sleep", pauses.append)

    result = CliRunner().invoke(cli.main, ["jokemark", "--playback", str(sidecar)])

    assert result.exit_code == 0
    assert "replayed 1 replies" in result.output
    assert NoPadDisplay.instances[0].shown == []
    assert pauses == []  # the log needs no reading time


def test_jokemark_playback_rejects_a_bad_report(monkeypatch, tmp_path):
    _use_fake(monkeypatch)
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")

    result = CliRunner().invoke(cli.main, ["jokemark", "--playback", str(bad)])

    assert result.exit_code == 1
    assert "not valid JSON" in _all_output(result)
    assert FakeDisplay.instances == []  # the file failed before the pad was probed
