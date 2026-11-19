"""The responder's message shaping and ``/command`` handling (no network)."""

from __future__ import annotations

import logging

from dankbot.commands import COLOURS, WHITE, Run
from dankbot.responder import Reply, Responder


def _chat_returning(text: str):
    def chat(messages: list[dict[str, str]]) -> str:
        return text

    return chat


def test_respond_returns_a_quip():
    responder = Responder(_chat_returning("Time to toast!"), model="m")
    assert responder.respond("the bread is done baking") == Reply(
        source="the bread is done baking",
        text="Time to toast!",
        skip=False,
        model="m",
        runs=(Run("Time to toast!", WHITE),),
        raw="Time to toast!",
    )


def test_respond_suppresses_notfunny():
    responder = Responder(_chat_returning("/notfunny"), model="m")
    out = responder.respond("meer loves the new toy")
    assert out.skip is True
    assert out.text == ""  # callers may print .text unconditionally
    assert out.runs == ()


def test_respond_carries_colour_runs():
    responder = Responder(_chat_returning("/color/red Danger!"), model="m")
    out = responder.respond("the light is red")
    assert out.runs == (Run("Danger!", COLOURS["red"]),)
    assert out.text == "Danger!"


def test_messages_are_system_then_user():
    seen: list[list[dict[str, str]]] = []

    def chat(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return "ok"

    responder = Responder(chat, model="m", persona="PERSONA")
    responder.respond("hello there")
    assert seen == [
        [
            {"role": "system", "content": "PERSONA"},
            {"role": "user", "content": "hello there"},
        ]
    ]


def test_as_dict_json_shape():
    out = Reply(source="a", text="b", skip=False, model="m")
    assert out.as_dict() == {
        "input": "a",
        "reply": "b",
        "skip": False,
        "model": "m",
        "raw": "",
    }


def test_respond_warns_when_asked_about_an_empty_line(caplog):
    """The gate should make this unreachable — say so loudly if it ever is not."""
    responder = Responder(_chat_returning("Time to toast!"), model="m")
    with caplog.at_level(logging.WARNING, logger="dankbot.responder"):
        responder.respond("   ")
    assert "empty line" in caplog.text


def test_respond_logs_the_round_trip_size_at_debug(caplog):
    responder = Responder(_chat_returning("Time to toast!"), model="m")
    with caplog.at_level(logging.DEBUG, logger="dankbot.responder"):
        responder.respond("the bread is done baking")
    assert "24 chars in" in caplog.text
    assert "14 chars out" in caplog.text
