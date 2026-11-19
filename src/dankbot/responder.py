"""The responder: one overheard line in, one witty :class:`Reply` out.

Deliberately transport-free — it is handed a ``chat`` callable
(``messages -> text``) so tests use a fake and never touch the network. The
:class:`~dankbot.server.ChatClient` supplies the real one.

The model's raw output is run through the :mod:`dankbot.commands` parser, so a
:class:`Reply` carries both the plain (command-free) text and the ordered colour
:class:`~dankbot.commands.Run` list the display renders.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .commands import Run, parse
from .persona import DEFAULT_PERSONA

log = logging.getLogger(__name__)

#: ``messages -> assistant text``. ``messages`` is the OpenAI chat shape.
ChatFn = Callable[[list[dict[str, str]]], str]


@dataclass(frozen=True)
class Reply:
    """The responder's verdict for one overheard line.

    ``text`` is the line to *show* (plain, commands stripped) — empty when
    :attr:`skip` is set, so callers can uniformly print ``text``. ``runs`` is the
    same text as ordered colour runs (for the display); ``raw`` is the model's
    untrimmed answer (for ``--json`` / debugging).
    """

    source: str
    text: str
    skip: bool
    model: str
    runs: tuple[Run, ...] = ()
    raw: str = ""

    def as_dict(self) -> dict[str, object]:
        """The ``--json`` shape: ``{input, reply, skip, model, raw}``."""
        return {
            "input": self.source,
            "reply": self.text,
            "skip": self.skip,
            "model": self.model,
            "raw": self.raw,
        }


class Responder:
    """Builds the chat request for each line and applies the ``/command`` rules."""

    def __init__(
        self,
        chat: ChatFn,
        *,
        model: str,
        persona: str = DEFAULT_PERSONA,
    ) -> None:
        self._chat = chat
        self._model = model
        self._persona = persona

    def messages_for(self, line: str) -> list[dict[str, str]]:
        """The ``[system, user]`` message list for one overheard line."""
        return [
            {"role": "system", "content": self._persona},
            {"role": "user", "content": line},
        ]

    def respond(self, line: str) -> Reply:
        """Ask for a reply to *line* and parse its commands + colour runs.

        Logs the round trip at DEBUG — the prompt and reply sizes plus the time
        the chat call took — which is what shows *what* is being sent (and, via
        the sizes, whether anything is growing) when the listen latency climbs.
        """
        if not line.strip():
            # Unreachable in practice — ``AudioIn._on_line`` drops blank lines
            # and the gate needs content words — but an empty prompt is the one
            # input that silently burns a whole round trip, so never guess.
            log.warning("asked the model about an empty line")
        started = time.monotonic()
        raw = self._chat(self.messages_for(line)).strip()
        log.debug(
            "model round trip: %d chars in → %d chars out in %.2fs",
            len(line),
            len(raw),
            time.monotonic() - started,
        )
        parsed = parse(raw)
        return Reply(
            source=line,
            text=parsed.text,
            skip=parsed.skip,
            model=self._model,
            runs=parsed.runs,
            raw=raw,
        )
