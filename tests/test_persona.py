"""The persona text and the ``/command`` convention it advertises."""

from __future__ import annotations

from dankbot.persona import DEFAULT_PERSONA


def test_default_persona_carries_the_contract():
    # The mechanism must name the escape hatch, advertise colour, and forbid
    # emoji / stray command characters (the output is destined for a scrolling
    # LED matrix).
    assert "/notfunny" in DEFAULT_PERSONA
    assert "/color/" in DEFAULT_PERSONA
    assert "emoji" in DEFAULT_PERSONA
    # jokemark's two most common model mistakes: a /notfunny mixed with a quip,
    # and a colour name glued to the following text.
    assert "whole reply" in DEFAULT_PERSONA
    assert "one space" in DEFAULT_PERSONA
