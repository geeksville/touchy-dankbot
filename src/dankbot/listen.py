"""The audio-in loop: utterances -> gate -> responder -> emit(log) + display.

Framework-free and hardware-free: it drives a :class:`~dankbot.audio.
TranscriptSource` through an :class:`~dankbot.gate.UtteranceGate` and an
injected :class:`~dankbot.responder.Responder`, calling ``respond`` on a
**worker thread** so the audio side never blocks on the LLM round-trip
(when the worker is busy the *older* pending utterance is dropped in favour of
the newest). ``inline=True`` runs it synchronously — tests use that for
deterministic ordering.

Two sinks fire per event: ``emit`` (log / ``--json``, always) and ``display``
(optional — :mod:`dankbot.display`'s pad, only on a ``KIND_REPLY``).

Every reply also carries its **pipeline latency**: the wall time from the
utterance arriving here to the quip being ready. That is the reply-queue wait
(it is a one-deep queue on a worker thread, so a slow model backs it up) plus
the model round trip — which is exactly what you want when asking "how far
behind is the machine?". The log line shows it (``REPLY: … (45s latency)``) and
``--json`` reports it as ``latency_s``. A *gate* skip never reached the model,
so it has no latency; a ``/notfunny`` skip does, and says so.

See ``docs/plans/audioin.md`` and ``docs/plans/ledout.md``.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import logs
from .audio import Utterance
from .commands import Run
from .display import DisplaySink
from .gate import UtteranceGate
from .responder import Responder

log = logging.getLogger(__name__)

KIND_HEARD = "heard"
KIND_REPLY = "reply"
KIND_SKIP = "skip"

_CLOSED = object()


@dataclass(frozen=True)
class ListenEvent:
    """One thing that happened while listening — the unit we log/emit."""

    kind: str  # KIND_HEARD | KIND_REPLY | KIND_SKIP
    text: str = ""  # the utterance as heard
    reply: str = ""  # the quip (kind == KIND_REPLY)
    reason: str = ""  # skip reason (kind == KIND_SKIP); "keep" otherwise
    model: str = ""
    runs: tuple[Run, ...] = ()  # the quip as colour runs (kind == KIND_REPLY)
    #: Seconds from the utterance arriving to the quip being ready — the
    #: reply-queue wait plus the model round trip. ``None`` for ``HEARD`` and
    #: for a gate ``SKIP``, which never reached the responder.
    latency_s: float | None = None

    def as_dict(self) -> dict[str, object]:
        """The ``--json`` shape."""
        return {
            "event": self.kind,
            "heard": self.text,
            "reply": self.reply,
            "reason": self.reason,
            "model": self.model,
            "latency_s": None if self.latency_s is None else round(self.latency_s, 2),
        }


@dataclass(frozen=True)
class _Pending:
    """A queued utterance plus the clock reading it reached the loop at.

    Kept beside :class:`Utterance` rather than added to it: it is the *loop's*
    bookkeeping (how long this one has been in flight), not a property of what
    was said.
    """

    utterance: Utterance
    heard_at: float


Emitter = Callable[[ListenEvent], None]


def log_emitter(event: ListenEvent) -> None:
    """The default emitter: ``HEARD`` / ``REPLY`` / ``SKIP`` log lines.

    The lines are :mod:`rich` texts (see :mod:`dankbot.logs`), so a ``REPLY``
    shows the quip in the colours the display scrolls while its plain text stays
    ``REPLY: …``; the one addition is a dim ``(45s latency)`` suffix when the
    event carries one.
    """
    if event.kind == KIND_REPLY:
        log.info(logs.reply_line(event.runs, event.reply, latency_s=event.latency_s))
    elif event.kind == KIND_SKIP:
        log.info(logs.skip_line(event.reason, event.text, latency_s=event.latency_s))
    else:
        log.info(logs.heard_line(event.text))


def json_emitter(event: ListenEvent) -> None:
    """Emit each event as one JSON object on stdout."""
    print(json.dumps(event.as_dict()), flush=True)


class Listener:
    """Drive a :class:`TranscriptSource` through the gate and the responder.

    The responder call (an HTTP round-trip) runs on a worker thread so the
    audio side never blocks on the network; the queue holds one item in flight
    plus a small backlog, and drops the oldest pending item if it ever fills.
    ``inline=True`` runs it synchronously on the calling thread instead — used
    for deterministic replay (``listen --fake``) and by tests.
    """

    def __init__(
        self,
        responder: Responder,
        gate: UtteranceGate,
        *,
        emit: Emitter | None = None,
        display: DisplaySink | None = None,
        max_queued: int = 2,
        inline: bool = False,
    ) -> None:
        self._responder = responder
        self._gate = gate
        self._emit = emit or log_emitter
        self._display = display
        self._inline = inline
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(max_queued)))
        self._thread: threading.Thread | None = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def _start(self) -> None:
        if self._inline or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._worker, name="dankbot-reply", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Drain in-flight replies and stop the worker (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if self._thread is None:
            return
        self._queue.put(_CLOSED)  # blocks only until a slot frees — we drain
        self._thread.join()
        self._thread = None

    # -- the loop ----------------------------------------------------------

    def run(self, source) -> None:
        """Consume *source* to exhaustion, then close it and the worker."""
        self._start()
        try:
            for utterance in source.lines():
                self._handle(utterance)
        finally:
            try:
                source.close()
            finally:
                self.close()

    def _handle(self, utterance: Utterance) -> None:
        heard_at = time.monotonic()  # the clock the reply's latency is measured from
        self._emit(ListenEvent(KIND_HEARD, text=utterance.text))
        decision = self._gate.decide(utterance.text, duration_s=utterance.duration_s)
        if not decision.keep:
            self._emit(ListenEvent(KIND_SKIP, text=utterance.text, reason=decision.reason))
            return
        self._submit(_Pending(utterance, heard_at))

    def _submit(self, pending: _Pending) -> None:
        if self._inline:
            self._answer(pending)
            return
        try:
            self._queue.put_nowait(pending)
        except queue.Full:
            # Drop the *oldest* pending utterance and keep the newest.
            try:
                self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - race, nothing to drop
                pass
            try:
                self._queue.put_nowait(pending)
            except queue.Full:  # pragma: no cover - worker ate the slot
                pass

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is _CLOSED:
                return
            try:
                self._answer(item)
            except Exception:  # a bad reply must not kill the loop
                log.exception("reply failed")

    def _answer(self, pending: _Pending) -> None:
        utterance = pending.utterance
        reply = self._responder.respond(utterance.text)
        # Read the clock *after* the model answered: the wait in the queue plus
        # the round trip is the number the log reports.
        latency_s = time.monotonic() - pending.heard_at
        if reply.skip:
            self._emit(
                ListenEvent(
                    KIND_SKIP,
                    text=utterance.text,
                    reason="notfunny",
                    model=reply.model,
                    latency_s=latency_s,
                )
            )
        else:
            self._emit(
                ListenEvent(
                    KIND_REPLY,
                    text=utterance.text,
                    reply=reply.text,
                    model=reply.model,
                    runs=reply.runs,
                    latency_s=latency_s,
                )
            )
            if self._display is not None:
                try:
                    self._display(reply.runs)
                except Exception:
                    log.exception("display update failed")
