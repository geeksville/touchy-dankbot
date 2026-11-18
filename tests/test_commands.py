"""The ``/command`` parser: colour runs, ``/notfunny``, and the recovery rules."""

from __future__ import annotations

from dankbot.commands import (
    COLOURS,
    EMPHASIS_COLOURS,
    PINK,
    PURPLE,
    WHITE,
    Run,
    parse,
    resolve_colour,
)


def test_plain_text_is_one_white_run():
    out = parse("Time to toast!")
    assert out.runs == (Run("Time to toast!", WHITE),)
    assert out.text == "Time to toast!"
    assert out.skip is False
    assert out.found == ()


def test_notfunny_skips():
    out = parse("/notfunny")
    assert out.skip is True
    assert out.runs == ()
    assert out.text == ""
    assert out.found == ("/notfunny",)


def test_notfunny_wins_over_text():
    # Kept as the final backstop: the prompt tells the model not to combine
    # them, but if it does, a skip is still a skip.
    assert parse("/notfunny I'll stay quiet").skip is True


def test_dressed_up_notfunny_still_skips():
    assert parse('"**/notfunny**"').skip is True


def test_text_that_merely_contains_notfunny_is_not_a_skip():
    assert parse("Not funny, that joke.").skip is False


def test_colour_starts_a_run():
    out = parse("/color/red Danger!")
    assert out.runs == (Run("Danger!", COLOURS["red"]),)
    assert out.text == "Danger!"


def test_colour_mid_sentence_splits_runs():
    out = parse("Look out /color/red Danger!")
    assert out.runs == (Run("Look out ", WHITE), Run("Danger!", COLOURS["red"]))
    assert out.text == "Look out Danger!"


def test_several_colours_in_one_reply():
    out = parse("/color/red Stop /color/blue go")
    assert [r.colour for r in out.runs] == [COLOURS["red"], COLOURS["blue"]]
    assert out.text == "Stop go"


def test_hex_colours_are_accepted():
    for value in ("0xFF00AA", "#FF00AA", "ff00aa"):
        assert parse(f"/color/{value} hi").runs[0].colour == 0xFF00AA


def test_named_colours_are_case_insensitive():
    assert parse("/COLOR/Red hi").runs[0].colour == COLOURS["red"]


def test_a_bad_colour_value_keeps_the_current_colour(caplog):
    with caplog.at_level("WARNING"):
        out = parse("/color/chartreuse hi")
    assert out.runs == (Run("hi", WHITE),)
    assert "chartreuse" in caplog.text


def test_a_bare_colour_resets_to_white(caplog):
    with caplog.at_level("WARNING"):
        out = parse("/color/red Danger /color/ safe")
    assert out.runs == (Run("Danger ", COLOURS["red"]), Run("safe", WHITE))
    assert out.text == "Danger safe"
    assert "no colour" in caplog.text


def test_an_unknown_command_is_recovered_as_text(caplog):
    with caplog.at_level("WARNING"):
        out = parse("/frobnicate hi")
    assert out.text == "frobnicate hi"
    assert out.found == ("/frobnicate",)
    assert "frobnicate" in caplog.text


def test_a_stray_leading_slash_keeps_the_word():
    # jokemark: "/meer is quite smitten…" means "meer is quite smitten…".
    assert parse("/meer is quite smitten").text == "meer is quite smitten"
    assert parse("/Oh dear, no appetisers").text == "Oh dear, no appetisers"


def test_an_invented_command_keeps_its_value():
    # jokemark: "/names/Oh dear…/name" — keep the argument, drop the invented
    # command and the straggling tail.
    assert parse("/names/Oh dear, how inconvenient./name").text == ("Oh dear, how inconvenient.")


def test_an_unknown_command_with_a_trailing_slash_keeps_its_word():
    assert parse("/nudge/ You mean it?").text == "nudge You mean it?"
    assert parse("Must have been /exhausted/ by it").text == "Must have been exhausted by it"


def test_a_trailing_unknown_command_is_dropped():
    assert parse("that is it /cringe/").text == "that is it"
    assert parse("that is it /name").text == "that is it"


def test_a_command_only_reply_is_a_skip():
    assert parse("/color/red").skip is True
    assert parse("   ").skip is True


def test_the_colon_separator_is_not_a_colour_command(caplog):
    # The contract switched from ``/color:NAME`` to ``/color/NAME``; the old
    # form is no longer a valid command, so the colour stays white.
    with caplog.at_level("WARNING"):
        out = parse("/color:red hi")
    assert out.runs == (Run(":red hi", WHITE),)
    assert "/color" in out.found
    assert "color" in caplog.text


def test_a_trailing_slash_after_the_value_is_ignored():
    out = parse("/color/green/ Danger!")
    assert out.runs == (Run("Danger!", COLOURS["green"]),)
    assert out.text == "Danger!"  # the stray "/" is not displayed
    assert out.found == ("/color/green/",)
    assert parse("/color/#FF00AA/ hi").runs[0].colour == 0xFF00AA
    assert parse("/notfunny/").skip is True
    # Repeated trailing slashes are absorbed too — nothing stray reaches the display.
    assert parse("/color/green// Danger!").runs == (Run("Danger!", COLOURS["green"]),)


def test_the_colour_alias_is_accepted():
    assert parse("/colour/red Danger!").runs == (Run("Danger!", COLOURS["red"]),)
    assert parse("/Colour/0xFF4D00 Gone").runs == (Run("Gone", 0xFF4D00),)


def test_a_colour_glued_to_its_text_is_split_apart():
    # jokemark: "/color/whiteI mean…" and "/color/0xFF4D00But…".
    assert parse("/color/whiteI mean this").runs == (Run("I mean this", COLOURS["white"]),)
    assert parse("/color/0xFF4D00But I am glowing").runs == (Run("But I am glowing", 0xFF4D00),)
    assert parse("/color/yellow<br>Oh, nice").runs == (Run("Oh, nice", COLOURS["yellow"]),)


def test_html_breaks_become_spaces():
    assert parse("one<br>two<br/>three<br />four").text == "one two three four"


def test_dangling_slashes_are_dropped():
    assert parse("it had wheels./ really").text == "it had wheels. really"
    assert parse("a / b").text == "a b"
    assert parse("very hard/ I bet").text == "very hard I bet"


def test_urls_and_word_slashes_are_not_commands():
    assert parse("see http://example.com/x for more").text == "see http://example.com/x for more"
    assert parse("either/or").text == "either/or"
    assert parse("3/4 of it").text == "3/4 of it"


def test_markdown_emphasis_gets_its_own_colour_and_loses_its_markers():
    # jokemark: "*crickets*", "*glitches and sparkles*", "*whirr*".
    out = parse("*crickets*")
    assert out.runs == (Run("crickets", EMPHASIS_COLOURS[0]),)
    assert out.text == "crickets"


def test_emphasis_cycles_through_the_emphasis_colours():
    out = parse("*dings* *crickets* *glitches and sparkles*")
    assert [run.colour for run in out.runs] == list(EMPHASIS_COLOURS[:3])
    assert out.text == "dings crickets glitches and sparkles"


def test_the_emphasis_cycle_restarts_at_red_after_the_last_colour():
    beats = len(EMPHASIS_COLOURS) + 1
    out = parse(" ".join(f"*beat{i}*" for i in range(beats)))
    assert out.runs[-1].colour == EMPHASIS_COLOURS[0]
    assert out.text == " ".join(f"beat{i}" for i in range(beats))


def test_emphasis_inside_a_sentence_splits_the_run():
    out = parse("Oh, I'm *so* worried")
    assert out.runs == (
        Run("Oh, I'm ", WHITE),
        Run("so", EMPHASIS_COLOURS[0]),
        Run(" worried", WHITE),
    )


def test_emphasis_tolerates_spaces_inside_the_markers():
    # jokemark: "* dims slightly * Oh dear." — the padded form is common.
    out = parse("* dims slightly * Oh dear")
    assert out.runs == (Run("dims slightly ", EMPHASIS_COLOURS[0]), Run("Oh dear", WHITE))


def test_bold_is_emphasis_too():
    assert parse("**clang**").runs == (Run("clang", EMPHASIS_COLOURS[0]),)


def test_an_explicit_colour_wins_over_emphasis():
    # The /color command is the model's own instruction: the beat loses its
    # markers but keeps the run's colour.
    out = parse("/color/yellow *ding* ding")
    assert out.runs == (Run("ding ding", COLOURS["yellow"]),)


def test_stray_asterisks_are_dropped():
    # jokemark: hermes-3 opens replies with a lone "*", then a /color command.
    out = parse("* /color/red Ah, the age-old question")
    assert out.runs == (Run("Ah, the age-old question", COLOURS["red"]),)
    assert parse("The bulb flickers *").text == "The bulb flickers"


def test_purple_and_pink_are_named_colours():
    assert parse("/color/purple hi").runs[0].colour == PURPLE
    assert parse("/color/pink hi").runs[0].colour == PINK
    assert resolve_colour("purple") == PURPLE
    assert resolve_colour("pink") == PINK


def test_resolve_colour():
    assert resolve_colour("green") == COLOURS["green"]
    assert resolve_colour("0X00FF00") == 0x00FF00
    assert resolve_colour("nope") is None
