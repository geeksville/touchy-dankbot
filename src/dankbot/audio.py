"""Microphone capture + speech-to-text — the "audio in" stage.

This is the only module that imports ``moonshine_voice`` / ``sounddevice`` (and
it does so lazily, so importing :mod:`dankbot` — and the test suite — never
needs them). Everything else talks to the tiny :class:`TranscriptSource` seam
below, which is what makes the loop testable with no mic, no model and no
network.

Mic: a Seeed reSpeaker XVF3800 USB array in production, addressed by name so it
is found whatever PortAudio index it lands on. Engine: Moonshine Voice's
``MicTranscriber``, which already does VAD, streaming and its own worker-thread
capture + resampling to 16 kHz — we only configure it and bridge its finished
lines onto a queue. See ``docs/plans/audioin.md``.

The capture is **supervised** (:class:`AudioIn`): PortAudio only *prints* a
callback error, so an unplugged array would otherwise leave the transcriber
open but silent forever, with no way to notice. We poll the stream and re-open
the mic when it dies — re-resolving the device by name, since a replug moves it
to a new PortAudio index. See ``docs/plans/micreconnect.md``.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO, runtime_checkable

from .config import DEFAULT_MIC_HINT, DEFAULT_MODEL_ARCH, DEFAULT_RECONNECT_DELAY_S
from .server import DankbotError

log = logging.getLogger(__name__)


class AudioError(DankbotError):
    """The microphone could not be opened, or the STT engine could not start."""


@dataclass(frozen=True)
class Utterance:
    """One finished thing a person said, as produced by the STT engine."""

    text: str
    duration_s: float | None = None


_CLOSED = object()


@runtime_checkable
class TranscriptSource(Protocol):
    """The audio seam: a blocking stream of finished utterances."""

    def lines(self) -> Iterator[Utterance]:
        """Block and yield finished utterances until :meth:`close`."""
        ...

    def close(self) -> None:
        """Stop the source and release the microphone."""
        ...


class FakeTranscriptSource:
    """Replay a scripted list of utterances — tests and ``listen --fake``.

    Needs no audio hardware and no model, so it drives the whole loop in CI.
    """

    def __init__(self, utterances: Iterable[str | Utterance]) -> None:
        self._items = [u if isinstance(u, Utterance) else Utterance(str(u)) for u in utterances]
        self.closed = False

    def lines(self) -> Iterator[Utterance]:
        yield from self._items

    def close(self) -> None:
        self.closed = True


def read_lines(stream: TextIO) -> list[str]:
    """The non-blank, whitespace-trimmed lines of *stream* (for ``--fake``)."""
    return [stripped for raw in stream if (stripped := raw.strip())]


#: Friendly ``--model-arch`` names -> Moonshine ``ModelArch`` attribute. The
#: streaming variants are what we want; every English alias maps to one.
_MOONSHINE_ARCHES = {
    "tiny": "TINY_STREAMING",
    "tiny-streaming": "TINY_STREAMING",
    "tinystreaming": "TINY_STREAMING",
    "base": "BASE_STREAMING",
    "base-streaming": "BASE_STREAMING",
    "basestreaming": "BASE_STREAMING",
    "small": "SMALL_STREAMING",
    "small-streaming": "SMALL_STREAMING",
    "smallstreaming": "SMALL_STREAMING",
    "medium": "MEDIUM_STREAMING",
    "medium-streaming": "MEDIUM_STREAMING",
    "mediumstreaming": "MEDIUM_STREAMING",
}


def _as_device(value: int | str | None) -> int | str | None:
    """Normalise a PortAudio device spec.

    ``MicTranscriber.device()`` (and sounddevice) treat a *string* as a
    case-insensitive name substring and an *int* as a device index. The CLI's
    ``--device`` is a string, so ``--device 5`` would otherwise be passed
    through as the name ``"5"`` and latch onto whatever device happened to
    contain a 5 (``hw:5,0`` …). Coerce a bare integer string back to an index.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return value


def resolve_device(hint: str | None, *, device: int | str | None = None) -> int | str | None:
    """Pick a PortAudio capture device.

    An explicit *device* (index or name substring) always wins. Otherwise the
    first input device whose name contains *hint* (case-insensitive) is used —
    that is how the reSpeaker XVF3800 is found whatever index it gets. ``None``
    means "let PortAudio pick the default".
    """
    if device is not None:
        return _as_device(device)
    if not hint:
        return None
    try:
        import sounddevice as sd

        for index, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) > 0 and hint.lower() in info["name"].lower():
                log.info("using input device %d: %s", index, info["name"])
                return index
    except Exception as exc:  # noqa: BLE001 - any PortAudio/backend error is fine here
        log.warning("could not query audio devices (%s); using the default", exc)
        return None
    log.warning("no input device matched %r; using PortAudio's default", hint)
    return None


def _close_quietly(mic) -> None:
    """Best-effort release of a ``MicTranscriber`` that failed partway through.

    ``load()`` opens the model and its capture stream before ``start()`` can
    fail, so a half-built transcriber still holds the microphone; ``close()``
    is safe on a partially constructed one. Never mask the real error.
    """
    try:
        mic.close()
    except Exception as exc:  # noqa: BLE001 - cleanup must not raise
        log.debug("mic.close() while cleaning up: %s", exc)


class AudioIn:
    """A :class:`TranscriptSource` backed by the mic and a Moonshine model.

    ``MicTranscriber`` does the heavy lifting (VAD, streaming, its own capture
    worker, resampling to 16 kHz); we configure it, bridge its ``on_line``
    callback onto a queue (and log its ``on_error`` stream), and hand finished
    lines out through :meth:`lines`.

    The microphone is **supervised**, because losing it is otherwise invisible
    (see the module docstring): :meth:`lines` watches the PortAudio stream,
    tears the transcriber down when it stops, and opens a fresh one — resolving
    the device *again*, since a replug lands the array on a new index. The
    first open still fails loudly (an appliance with no mic should say so);
    only a *re*open waits and retries, until the device is back or
    :meth:`close` interrupts it.
    """

    def __init__(
        self,
        *,
        device: int | str | None = None,
        mic_hint: str | None = DEFAULT_MIC_HINT,
        model_arch: str = DEFAULT_MODEL_ARCH,
        language: str = "en",
        models_dir: str | Path | None = None,
        channels: int | None = None,
        blocksize: int | None = None,
        options: dict | None = None,
        reconnect_delay_s: float = DEFAULT_RECONNECT_DELAY_S,
    ) -> None:
        self._device_arg = device
        self._mic_hint = mic_hint
        self._device: int | str | None = None
        self._model_arch = model_arch
        self._language = language
        self._models_dir = models_dir
        self._channels = channels
        self._blocksize = blocksize
        self._options = options or {}
        # How long to wait between liveness checks / re-open attempts; <= 0
        # turns supervision off and keeps the old "open once, block" behaviour.
        self._reconnect_delay = float(reconnect_delay_s)
        self._last_stream_time: float | None = None
        self._reconnect = threading.Event()  # a dead pipeline asks to be rebuilt
        self._queue: queue.Queue = queue.Queue()
        self._mic = None
        self._closed = False
        self._wake = threading.Event()  # interrupts a reconnect wait

    # -- construction ------------------------------------------------------

    def _engine(self):
        """The Moonshine classes for our arch, or an ``AudioError``.

        A bad ``--model-arch`` or a missing engine is a *configuration*
        problem, not a device one, so it is resolved once, before the reconnect
        loop: it should fail loudly rather than be retried forever.
        """
        arch_name = _MOONSHINE_ARCHES.get(self._model_arch.lower().replace("_", "-"))
        if arch_name is None:
            known = ", ".join(sorted(set(_MOONSHINE_ARCHES)))
            raise AudioError(f"unknown model arch {self._model_arch!r} (expected one of: {known})")

        try:
            from moonshine_voice import MicTranscriber, ModelArch
        except Exception as exc:  # pragma: no cover - dependency missing
            raise AudioError(f"speech-to-text engine unavailable: {exc}") from exc
        return MicTranscriber, getattr(ModelArch, arch_name)

    def _build(self, engine):
        """Build, load and start a fresh Moonshine mic transcriber.

        Raises ``AudioError`` for anything that keeps the microphone from coming
        up — a device that is absent, busy, or simply not plugged back in yet —
        which :meth:`lines` treats as transient. The device is resolved *here*
        rather than in ``__init__`` so every reconnect sees the current
        PortAudio index.
        """
        MicTranscriber, arch = engine
        self._device = resolve_device(self._mic_hint, device=self._device_arg)

        mic = MicTranscriber().language(self._language).model_arch(arch)
        if self._device is not None:
            mic.device(self._device)
        if self._channels is not None:
            mic.channels(self._channels)
        if self._blocksize is not None:
            # Bigger blocks = a bigger driver ring buffer = more slack before an
            # input overflow drops audio (see DEFAULT_MIC_BLOCKSIZE).
            mic.blocksize(self._blocksize)
        if self._models_dir is not None:
            mic.models_from(str(self._models_dir))
        if self._options:
            # The escape hatch for engine tuning, e.g. {"vad_threshold": 0.35}:
            # how long a slight pause may be before a line is called finished
            # (see DEFAULT_VAD_THRESHOLD in config.py).
            log.debug("transcriber options: %s", self._options)
            mic.options(self._options)
        mic.on_line(self._on_line)
        mic.on_error(self._on_error)
        try:
            log.info("loading the %s speech-to-text model…", self._model_arch)
            mic.load()
            mic.start()
        except Exception as exc:
            # load() may already have opened the model and its stream; a failed
            # start() must not leak the microphone for the life of the process.
            _close_quietly(mic)
            raise AudioError(f"could not start the microphone: {exc}") from exc
        return mic

    def _on_line(self, line) -> None:
        """Called from Moonshine's worker thread for each finished line."""
        text = (getattr(line, "text", "") or "").strip()
        if text:
            self._queue.put(Utterance(text, getattr(line, "duration", None)))

    def _on_error(self, error) -> None:
        """Called from Moonshine's worker thread when the pipeline raises.

        The library delivers ``Error`` events (a capture/inference failure
        inside its audio worker) to ``on_error`` and otherwise drops them, so
        attaching this is what keeps a broken stream from failing silently. A
        pipeline that raised is also asked to be rebuilt — a broken stream will
        not fix itself, and re-opening is cheap next to staying mute.
        """
        log.warning("speech-to-text error: %s", error)
        self._reconnect.set()

    # -- TranscriptSource --------------------------------------------------

    def lines(self) -> Iterator[Utterance]:
        """Yield utterances, reopening the microphone whenever it goes away."""
        engine = self._engine()
        first = True
        while not self._closed:
            try:
                self._mic = self._build(engine)
            except AudioError as exc:
                if first or self._reconnect_delay <= 0:
                    raise  # nothing to listen to yet — say so, don't spin
                log.warning(
                    "microphone unavailable (%s); retrying in %.1fs", exc, self._reconnect_delay
                )
                if self._wait_for_device():
                    return
                continue
            first = False
            self._last_stream_time = None
            self._reconnect.clear()
            try:
                yield from self._drain()
            finally:
                self._teardown()

    def _drain(self) -> Iterator[Utterance]:
        """Yield until closed, until the capture dies, or until a reconnect is asked for.

        The inner half of :meth:`lines`: with supervision on, each quiet spell
        is also a liveness check on the capture stream.
        """
        while not self._closed and not self._reconnect.is_set():
            if self._reconnect_delay <= 0:  # supervision off: block, as we always did
                item = self._queue.get()
            else:
                try:
                    item = self._queue.get(timeout=self._reconnect_delay)
                except queue.Empty:
                    if not self._capture_alive():
                        log.warning("microphone capture stopped; reconnecting")
                        break
                    continue
            if item is _CLOSED:
                return
            yield item

    def _capture_alive(self) -> bool:
        """Is the open PortAudio capture stream still running?

        ``MicTranscriber`` keeps its capture stream private and only *prints* a
        PortAudio callback error, so a vanished device is otherwise invisible:
        the transcriber just goes quiet. Peek at the stream and call it dead
        when it is closed, no longer ``active``, or its clock has stopped
        advancing across two probes (a live stream's clock always moves). Being
        wrong here only costs one reconnect, so the probe errs that way; if the
        library ever hides ``_sd_stream`` we cannot tell, and assume it is fine.
        """
        stream = getattr(self._mic, "_sd_stream", None)
        if stream is None:
            return True
        try:
            if stream.closed or not stream.active:
                return False
            now = float(stream.time)
        except Exception as exc:  # noqa: BLE001 - a failed probe is not proof of life
            log.debug("capture-stream probe failed (%s); reconnecting", exc)
            return False
        stalled = self._last_stream_time is not None and now <= self._last_stream_time
        self._last_stream_time = now
        return not stalled

    def _wait_for_device(self) -> bool:
        """Wait between reconnect attempts; ``True`` if :meth:`close` cut it short."""
        if self._reconnect_delay <= 0:
            return True
        return self._wake.wait(self._reconnect_delay)

    def close(self) -> None:
        # Wake lines() (if running — even mid-reconnect-wait) and stop the mic
        # now, so a ctrl-c that interrupts the loop still releases the device.
        self._closed = True
        self._wake.set()
        self._queue.put(_CLOSED)
        self._teardown()

    def _teardown(self) -> None:
        mic, self._mic = self._mic, None
        self._last_stream_time = None
        if mic is None:
            return
        for step in ("stop", "close"):
            try:
                getattr(mic, step)()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup, never fatal
                log.debug("mic.%s() while closing: %s", step, exc)
