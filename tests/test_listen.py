"""The listen loop: gate -> responder -> events, with no mic, model or network."""

from __future__ import annotations

import json
import threading
import time

from dankbot.audio import FakeTranscriptSource
from dankbot.gate import UtteranceGate
from dankbot.listen import (
    KIND_HEARD,
    KIND_REPLY,
    KIND_SKIP,
    Listener,
    ListenEvent,
    json_emitter,
)
from dankbot.responder import Responder


def _responder(mapping: dict[str, str] | None = None, default: str = "Time to toast!"):
    def chat(messages: list[dict[str, str]]) -> str:
        return (mapping or {}).get(messages[-1]["content"], default)

    return Responder(chat, model="m")


def _collector():
    events: list[ListenEvent] = []
    return events, events.append


def test_emits_heard_then_reply_inline():
    events, emit = _collector()
    listener = Listener(_responder(), UtteranceGate(min_words=1), emit=emit, inline=True)
    listener.run(FakeTranscriptSource(["the bread is done baking"]))
    assert [e.kind for e in events] == [KIND_HEARD, KIND_REPLY]
    assert events[1].reply == "Time to toast!"
    assert events[1].text == "the bread is done baking"
    assert [r.text for r in events[1].runs] == ["Time to toast!"]


def test_short_utterances_are_skipped_and_the_llm_is_never_asked():
    asked: list[list[dict[str, str]]] = []

    def chat(messages):
        asked.append(messages)
        return "quip"

    events, emit = _collector()
    listener = Listener(
        Responder(chat, model="m"), UtteranceGate(min_words=3), emit=emit, inline=True
    )
    listener.run(FakeTranscriptSource(["uh", "hmm yeah"]))
    assert [e.kind for e in events] == [KIND_HEARD, KIND_SKIP, KIND_HEARD, KIND_SKIP]
    assert all(e.reason == "too_short" for e in events if e.kind == KIND_SKIP)
    assert asked == []


def test_notfunny_comes_back_as_a_skip():
    events, emit = _collector()
    listener = Listener(
        _responder(default="/notfunny"), UtteranceGate(min_words=1), emit=emit, inline=True
    )
    listener.run(FakeTranscriptSource(["meer loves the new toy"]))
    skips = [e for e in events if e.kind == KIND_SKIP]
    assert skips and skips[0].reason == "notfunny"
    assert not any(e.kind == KIND_REPLY for e in events)


def test_source_is_closed_after_the_run():
    source = FakeTranscriptSource(["the bread is done baking"])
    Listener(_responder(), UtteranceGate(min_words=1), emit=lambda e: None, inline=True).run(source)
    assert source.closed is True


def test_threaded_path_delivers_every_reply_before_returning():
    events, emit = _collector()
    listener = Listener(_responder(), UtteranceGate(min_words=1), emit=emit)  # threaded
    listener.run(FakeTranscriptSource(["the bread is done baking", "the light is red"]))
    replies = [e.text for e in events if e.kind == KIND_REPLY]
    assert len(replies) == 2  # run() drains the worker before it returns


def test_a_slow_responder_does_not_block_the_loop_and_drops_the_stale_utterance():
    released = threading.Event()
    started = threading.Event()

    def chat(messages):
        started.set()
        released.wait(5)
        return "ok"

    events, emit = _collector()
    listener = Listener(
        Responder(chat, model="m"), UtteranceGate(min_words=1), emit=emit, max_queued=1
    )
    source = FakeTranscriptSource(["alpha one two", "bravo one two", "charlie one two"])
    runner = threading.Thread(target=listener.run, args=(source,))
    runner.start()
    assert started.wait(5), "the worker never picked up the first utterance"
    released.set()
    runner.join(10)
    assert not runner.is_alive()
    heard = [e.text for e in events if e.kind == KIND_HEARD]
    assert heard == ["alpha one two", "bravo one two", "charlie one two"]  # nothing blocked
    replies = [e.text for e in events if e.kind == KIND_REPLY]
    assert 1 <= len(replies) <= 2
    assert "bravo one two" not in replies  # the stale middle one was dropped
    assert replies[-1] == "charlie one two"  # the newest wins


def test_a_failing_reply_does_not_kill_the_loop():
    def chat(messages):
        raise RuntimeError("boom")

    events, emit = _collector()
    listener = Listener(Responder(chat, model="m"), UtteranceGate(min_words=1), emit=emit)
    listener.run(FakeTranscriptSource(["alpha one two", "bravo one two"]))
    assert [e.kind for e in events if e.kind == KIND_HEARD] == [KIND_HEARD, KIND_HEARD]
    assert not any(e.kind == KIND_REPLY for e in events)


def test_json_emitter_shape(capsys):
    json_emitter(ListenEvent(KIND_REPLY, text="a", reply="b", model="m", latency_s=45.234))
    assert json.loads(capsys.readouterr().out) == {
        "event": "reply",
        "heard": "a",
        "reply": "b",
        "reason": "",
        "model": "m",
        "latency_s": 45.23,
    }


def test_json_reports_no_latency_before_the_model_has_answered(capsys):
    json_emitter(ListenEvent(KIND_HEARD, text="a"))
    assert json.loads(capsys.readouterr().out)["latency_s"] is None


def test_events_are_logged_by_default(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="dankbot"):
        listener = Listener(_responder(), UtteranceGate(min_words=1), inline=True)
        listener.run(FakeTranscriptSource(["the bread is done baking", "uh"]))
    assert "HEARD: the bread is done baking" in caplog.text
    assert "REPLY: Time to toast!" in caplog.text
    assert "SKIP(too_short): uh" in caplog.text


def test_the_display_sink_only_sees_replies():
    shown: list[tuple] = []
    listener = Listener(
        _responder(),
        UtteranceGate(min_words=1),
        emit=lambda e: None,
        display=shown.append,
        inline=True,
    )
    listener.run(FakeTranscriptSource(["the bread is done baking", "uh"]))
    assert ["".join(r.text for r in runs) for runs in shown] == ["Time to toast!"]


def test_a_broken_display_sink_does_not_kill_the_loop():
    def boom(runs):
        raise RuntimeError("no pad")

    events, emit = _collector()
    listener = Listener(
        _responder(), UtteranceGate(min_words=1), emit=emit, display=boom, inline=True
    )
    listener.run(FakeTranscriptSource(["the bread is done baking"]))
    assert any(e.kind == KIND_REPLY for e in events)


# -- pipeline latency: "how far behind is the machine?" ----------------------


def test_a_reply_reports_the_pipeline_latency():
    def chat(messages):
        time.sleep(0.05)  # a slow model stage
        return "Time to toast!"

    events, emit = _collector()
    listener = Listener(
        Responder(chat, model="m"), UtteranceGate(min_words=1), emit=emit, inline=True
    )
    listener.run(FakeTranscriptSource(["the bread is done baking"]))
    reply = next(e for e in events if e.kind == KIND_REPLY)
    assert reply.latency_s >= 0.05
    assert next(e for e in events if e.kind == KIND_HEARD).latency_s is None


def test_a_gate_skip_has_no_latency_because_the_model_never_ran():
    events, emit = _collector()
    listener = Listener(_responder(), UtteranceGate(min_words=3), emit=emit, inline=True)
    listener.run(FakeTranscriptSource(["uh"]))
    skip = next(e for e in events if e.kind == KIND_SKIP)
    assert skip.reason == "too_short"
    assert skip.latency_s is None


def test_a_notfunny_skip_reports_the_latency_of_its_round_trip():
    def chat(messages):
        time.sleep(0.05)
        return "/notfunny"

    events, emit = _collector()
    listener = Listener(
        Responder(chat, model="m"), UtteranceGate(min_words=1), emit=emit, inline=True
    )
    listener.run(FakeTranscriptSource(["nothing to see here"]))
    skip = next(e for e in events if e.kind == KIND_SKIP)
    assert skip.reason == "notfunny"
    assert skip.latency_s >= 0.05


def test_latency_counts_the_wait_behind_a_busy_reply_queue():
    """The second reply's own model call is instant — the time it reports is the backlog."""

    def chat(messages):
        if messages[-1]["content"] == "alpha one two":
            time.sleep(0.1)  # one slow reply, so the next one has to wait for the worker
        return "ok"

    events, emit = _collector()
    listener = Listener(
        Responder(chat, model="m"), UtteranceGate(min_words=1), emit=emit, max_queued=4
    )
    listener.run(FakeTranscriptSource(["alpha one two", "charlie one two"]))
    latency = {e.text: e.latency_s for e in events if e.kind == KIND_REPLY}
    assert latency["alpha one two"] >= 0.1  # its own slow model
    assert latency["charlie one two"] >= 0.09  # queued behind it, not its own model
