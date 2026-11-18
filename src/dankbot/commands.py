"""The ``/command`` contract: directives embedded in the model's output.

Any *recognised* command word in the LLM's reply is a directive to dankbot and
is never displayed. Two commands exist today:

* ``/notfunny`` — the model has nothing worth saying; suppress the whole reply.
* ``/color/NAME`` (also accepted as ``/colour/NAME``) — start a coloured run.
  ``NAME`` is ``red`` / ``green`` / ``blue`` / ``yellow`` / ``white`` /
  ``purple`` / ``pink``, or ``0xRRGGBB`` (also ``#RRGGBB`` / ``RRGGBB``) so the
  model can pick any colour. Everything after the command is that colour until
  the next ``/color`` (or the end of the reply).

Small models routinely get the syntax slightly wrong, so the grammar is
deliberately forgiving:

* a stray *trailing* slash on the token is absorbed (``/color/green/`` is green,
  not an unknown colour ``green/``);
* a colour glued to its text still works — ``/color/whiteThe end`` is white
  ``The end``, and ``/color/0xFF4D00But...`` is orange ``But...``;
* a bare ``/color`` / ``/color/`` resets to white;
* literal ``<br>`` / ``<br/>`` markup becomes a space;
* a dangling slash is dropped (``wheels./`` → ``wheels.``, ``a / b`` → ``a b``)
  while URLs (``http://example.com/x``) and word slashes (``either/or``) stay;
* an *unknown* command is recovered as text instead of swallowed — a stray
  leading slash keeps the word (``/meer is here`` → ``meer is here``) and an
  invented command with a value keeps the value (``/names/Oh dear`` → ``Oh
  dear``). A trailing unknown token is treated as junk and dropped.

Markdown emphasis is treated as *colour*, not syntax: the panel has no bold or
italic, so ``*whirr*`` / ``**clang**`` (an action beat, a stressed word) becomes
its own run drawn in the next of :data:`EMPHASIS_COLOURS` — red, purple, blue,
green, pink, cycling per reply so two adjacent beats never look the same. The
markers are stripped. A span inside an explicit ``/color`` keeps that colour
(the model's command wins), and a ``*`` left over afterwards is markup noise
and is dropped, like a dangling slash.

A reply parses into ordered :class:`Run` objects (text + colour). Colours are
purely decorative here: LVGL renders the *unmarked* text in the label's
``text_color`` (white) and only the ``#RRGGBB …#`` runs in their colour, so a
reply with no ``/color`` is simply white — there is no reset and no carried-over
state between replies.

See ``docs/plans/ledout.md`` → "The ``/command`` contract".
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import cycle

log = logging.getLogger(__name__)

#: The "unmarked" colour — what LVGL renders plain (non-``#RRGGBB``) text in.
WHITE = 0xFFFFFF

#: Vivid violet and hot pink: readable on an LED matrix, and the two emphasis
#: colours that ``/color/NAME`` did not already have.
PURPLE = 0xA020F0
PINK = 0xFF69B4

#: Named colours accepted by ``/color/NAME``.
COLOURS = {
    "red": 0xFF0000,
    "green": 0x00FF00,
    "blue": 0x0000FF,
    "yellow": 0xFFFF00,
    "white": 0xFFFFFF,
    "purple": PURPLE,
    "pink": PINK,
}

#: The colours cycled through for markdown emphasis — ``*whirr*``, ``*crickets*``,
#: ``*glitches and sparkles*``. A model uses them for an action beat or a
#: stressed word, which a display without bold/italic can only say with colour.
#: Deliberately short (five) and loud: one reply rarely has more than two beats,
#: and the cycle restarts at ``red`` for every reply.
EMPHASIS_COLOURS = (
    COLOURS["red"],
    PURPLE,
    COLOURS["blue"],
    COLOURS["green"],
    PINK,
)

#: A command token: ``/name`` optionally ``/value``, starting a word (so ``a/b``
#: and the ``//`` in a URL are not commands). ``\w``/``/`` before the slash is
#: excluded so ``http://x`` does not read as ``/x``. The value is one
#: slash-free token, so ``/color/red`` and ``/color/0xFF00AA`` both parse, and any
#: stray *trailing* slashes are absorbed by the token rather than becoming part of
#: the value or the displayed text — ``/color/green/`` is ``green``, not ``green/``.
#: The value group may be empty: that is how ``/nudge/`` (an invented command) is
#: told apart from ``/meer`` (a stray slash on an ordinary word).
_CMD_RE = re.compile(r"(?<![/\w])/([a-z]+)(?:(/)([^\s/]*))?/*", re.IGNORECASE)
#: ``0xRRGGBB`` / ``#RRGGBB`` / a bare ``RRGGBB``.
_HEX_RE = re.compile(r"^(?:0x|#)?([0-9a-f]{6})$", re.IGNORECASE)
#: The same, but allowed to be glued to the following text (``0xFF4D00But``).
_HEX_PREFIX_RE = re.compile(r"^(?:0x|#)?([0-9a-f]{6})", re.IGNORECASE)
_SPACE_RE = re.compile(r"[ \t]+")
#: Literal HTML-ish line breaks some models put in their replies.
_BR_RE = re.compile(r"<\s*/?\s*br\s*/?\s*>", re.IGNORECASE)
#: A slash that is not part of a URL/word and has nothing to command.
_DANGLING_SLASH_RE = re.compile(r"(?<![:/])/(?![\w/])")
#: Markdown emphasis round an action beat or stressed word: ``*whirr*`` and
#: ``**clang**``. Neither pattern crosses a newline (a stray ``*`` must not pair
#: up with one paragraphs away) and the emphasis body may not contain ``*``, so
#: ``*pop* *sparkles*`` is two spans, not one. The bold form goes first and may
#: contain ``*`` itself, so ``**I'm not *that* special.**`` stays one span.
_EMPHASIS_RE = re.compile(r"\*\*(?P<strong>[^\n]+?)\*\*|\*(?P<emphasis>[^*\n]+?)\*")


@dataclass(frozen=True)
class Run:
    """A stretch of displayable text in a single colour (``0xRRGGBB``)."""

    text: str
    colour: int = WHITE


@dataclass(frozen=True)
class ParsedReply:
    """One model reply, split into command-free colour runs."""

    runs: tuple[Run, ...] = ()
    skip: bool = False
    found: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """The plain text, every command stripped."""
        return "".join(run.text for run in self.runs)


def resolve_colour(value: str) -> int | None:
    """``/color/`` value → ``0xRRGGBB``, or ``None`` if it isn't recognised."""
    named = COLOURS.get(value.lower())
    if named is not None:
        return named
    match = _HEX_RE.match(value)
    return int(match.group(1), 16) if match else None


def _split_colour(value: str) -> tuple[int, str] | None:
    """A ``/color/`` value → ``(colour, text glued onto it)``, or ``None``.

    Models often forget the space after the colour, so the value runs on into
    the quip (``/color/whiteI mean``, ``/color/0xFF4D00But...``). Split the
    recognised prefix off and hand the remainder back as displayable text.
    """
    lowered = value.lower()
    for name in sorted(COLOURS, key=len, reverse=True):
        if lowered.startswith(name):
            return COLOURS[name], value[len(name) :]
    match = _HEX_PREFIX_RE.match(value)
    if match:
        return int(match.group(1), 16), value[match.end() :]
    return None


def _clean(text: str) -> str:
    """Drop HTML breaks, leftover emphasis markers and dangling slashes.

    Asterisks are gone by now if they paired up (see :func:`_emphasised`), so a
    stray one is markup the model forgot to close — never displayable content
    on a panel that has no italic.
    """
    return _DANGLING_SLASH_RE.sub("", _BR_RE.sub(" ", text).replace("*", ""))


def _recover(match: re.Match[str], raw: str) -> str:
    """The text to show for an unknown ``/word`` token (see the module doc)."""
    value = match.group(3)
    if value:
        return value  # an invented command with an argument: keep the argument
    rest = _BR_RE.sub("", raw[match.end() :]).strip(" \t\r\n.,!?;:")
    if not rest:
        return ""  # nothing follows it: a straggling command, not a word
    return match.group(1)  # a stray slash in front of an ordinary word


def _emphasised(segments: Iterable[tuple[str, int]]) -> Iterator[tuple[str, int]]:
    """Split markdown emphasis out of *segments*, cycling :data:`EMPHASIS_COLOURS`.

    ``*whirr*`` / ``**clang**`` becomes its own ``(text, colour)`` segment with
    the markers stripped, so the display can shout an action beat even though it
    has no bold or italic. An explicit ``/color`` wins — only plain (white) text
    is recoloured, and only a recoloured span advances the cycle, so the first
    beat of a reply is always red.
    """
    colours = cycle(EMPHASIS_COLOURS)
    for text, colour in segments:
        pos = 0
        for match in _EMPHASIS_RE.finditer(text):
            yield text[pos : match.start()], colour
            phrase = match.group("strong") or match.group("emphasis")
            yield phrase, colour if colour != WHITE else next(colours)
            pos = match.end()
        yield text[pos:], colour


def _finish(segments: list[tuple[str, int]], found: list[str], skip: bool) -> ParsedReply:
    """Tidy raw ``(text, colour)`` segments into a :class:`ParsedReply`."""
    runs: list[Run] = []
    for raw_text, colour in _emphasised(segments):
        text = _SPACE_RE.sub(" ", _clean(raw_text))
        if not runs:
            text = text.lstrip(" ")  # leading space of the whole message
        elif runs[-1].text.endswith(" "):
            text = text.lstrip(" ")  # no double space across a run boundary
        if text.strip():
            if runs and runs[-1].colour == colour:
                runs[-1] = Run(runs[-1].text + text, colour)
            else:
                runs.append(Run(text, colour))
        elif text and runs and not runs[-1].text.endswith(" "):
            # A gap between two coloured runs — the space between adjacent
            # ``*beats*`` — belongs to the run before it; dropping it entirely
            # (as a plain whitespace segment) would fuse the neighbouring words.
            runs[-1] = Run(runs[-1].text + " ", runs[-1].colour)
    if runs:
        runs[-1] = Run(runs[-1].text.rstrip(" "), runs[-1].colour)
    return ParsedReply(runs=tuple(runs), skip=skip or not runs, found=tuple(found))


def parse(raw: str) -> ParsedReply:
    """Split *raw* into colour runs and apply its directives."""
    colour = WHITE
    skip = False
    found: list[str] = []
    segments: list[tuple[str, int]] = []
    pos = 0

    for match in _CMD_RE.finditer(raw):
        segments.append((raw[pos : match.start()], colour))
        pos = match.end()
        token = match.group(0)
        name = match.group(1).lower()
        value = match.group(3)
        found.append(token)
        if name == "notfunny":
            skip = True
        elif name in ("color", "colour"):
            if not value:
                log.warning("colour command %r has no colour; resetting to white", token)
                colour = WHITE
                continue
            split = _split_colour(value)
            if split is None:
                log.warning("ignoring unknown colour %r in %r", value, token)
                continue
            colour, trailing = split
            if trailing:
                segments.append((trailing, colour))
        else:
            log.warning("recovering unknown command %r as text", token)
            recovered = _recover(match, raw)
            if recovered:
                segments.append((recovered, colour))
    segments.append((raw[pos:], colour))

    return _finish(segments, found, skip)
