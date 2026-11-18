"""The audio seam: fake source, line reading, device selection, the queue bridge."""

from __future__ import annotations

import io
import logging
import sys
import types

import pytest

from dankbot.audio import (
    _MOONSHINE_ARCHES,
    AudioError,
    AudioIn,
    FakeTranscriptSource,
    Utterance,
    read_lines,
    resolve_device,
)


def test_fake_source_yields_utterances_and_closes():
    source = FakeTranscriptSource(["a", Utterance("b", 1.5)])
    assert [u.text for u in source.lines()] == ["a", "b"]
    assert [u.duration_s for u in source.lines()] == [None, 1.5]
    assert source.closed is False
    source.close()
    assert source.closed is True


def test_read_lines_trims_and_drops_blanks():
    assert read_lines(io.StringIO("a\n\n  b  \n")) == ["a", "b"]


def test_friendly_arch_names_all_map_to_streaming():
    for name in ("tiny", "base", "small", "medium"):
        assert _MOONSHINE_ARCHES[name].endswith("STREAMING")


def test_resolve_device_prefers_an_explicit_device():
    assert resolve_device("respeaker", device=5) == 5
    assert resolve_device("respeaker", device="hw:2,0") == "hw:2,0"


def test_resolve_device_coerces_a_bare_numeric_index():
    # `--device 5` arrives as a string; an index must not degrade to the name "5".
    assert resolve_device("respeaker", device="5") == 5
    assert resolve_device("respeaker", device=" 12 ") == 12
    assert resolve_device("respeaker", device="hw:2,0") == "hw:2,0"


def _install_fake_sounddevice(monkeypatch, devices):
    module = types.SimpleNamespace(query_devices=lambda: devices)
    monkeypatch.setitem(sys.modules, "sounddevice", module)


def test_resolve_device_matches_by_name(monkeypatch):
    _install_fake_sounddevice(
        monkeypatch,
        [
            {"name": "HD-Audio Generic: ALC294 Analog (hw:1,0)", "max_input_channels": 2},
            {"name": "reSpeaker XVF3800 4-Mic Array: USB Audio (hw:2,0)", "max_input_channels": 2},
        ],
    )
    assert resolve_device("respeaker") == 1


def test_resolve_device_ignores_output_only_devices(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [{"name": "reSpeaker Output", "max_input_channels": 0}])
    assert resolve_device("respeaker") is None


def test_resolve_device_without_a_hint_uses_the_default(monkeypatch):
    assert resolve_device(None) is None


class _FakeStream:
    """A ``sounddevice.InputStream`` stand-in: only what the liveness probe reads.

    With ``times`` the clock returns that scripted sequence — so ``[2.0, 2.0]``
    reads as frozen; without it the clock advances a second per probe, the way a
    live stream's does.
    """

    def __init__(self, *, times=None, active: bool = True, closed: bool = False) -> None:
        self.active = active
        self.closed = closed
        self._script = None if times is None else list(times)
        self._clock = 0.0

    @property
    def time(self) -> float:
        if self._script:
            self._clock = self._script.pop(0)
        else:
            self._clock += 1.0
        return self._clock


def _install_fake_moonshine(monkeypatch, script):
    """Install a fake ``moonshine_voice`` whose ``MicTranscriber`` follows *script*.

    One entry per construction: ``lines`` are handed to ``on_line`` when
    ``start()`` runs (so they are queued before ``lines()`` yields), ``times``
    scripts the capture clock, ``start_error`` makes ``start()`` fail (the
    "not plugged back in yet" case) and ``no_stream`` hides the PortAudio
    stream. Returns the fakes built, in order, so a test can check the lifecycle.
    """
    built: list = []
    pending = list(script)

    class FakeMic:
        def __init__(self) -> None:
            self.spec = pending.pop(0) if pending else {}
            self.closed = False
            self.line_cb = None
            self.blocksize_frames = None
            self._sd_stream = (
                None
                if self.spec.get("no_stream")
                else _FakeStream(times=self.spec.get("times"), active=self.spec.get("active", True))
            )
            built.append(self)

        def language(self, code):
            return self

        def model_arch(self, arch):
            return self

        def device(self, index):
            return self

        def channels(self, count):
            return self

        def blocksize(self, frames):
            self.blocksize_frames = frames
            return self

        def models_from(self, directory):
            return self

        def options(self, options):
            return self

        def on_line(self, callback):
            self.line_cb = callback
            return self

        def on_error(self, callback):
            return self

        def load(self):
            return self

        def start(self):
            if self.spec.get("start_error"):
                raise RuntimeError(self.spec["start_error"])
            for text in self.spec.get("lines", []):
                self.line_cb(types.SimpleNamespace(text=text, duration=1.0))
            return self

        def stop(self):
            pass

        def close(self):
            self.closed = True

    fake = types.SimpleNamespace(
        MicTranscriber=FakeMic, ModelArch=types.SimpleNamespace(SMALL_STREAMING=4)
    )
    monkeypatch.setitem(sys.modules, "moonshine_voice", fake)
    return built


def test_on_line_bridges_finished_lines_onto_the_queue():
    audio_in = AudioIn(model_arch="small")
    audio_in._on_line(types.SimpleNamespace(text="  the bread is done  ", duration=1.2))
    audio_in._on_line(types.SimpleNamespace(text="   ", duration=0.1))  # blank -> ignored
    assert audio_in._queue.get() == Utterance("the bread is done", 1.2)
    assert audio_in._queue.empty()


def test_unknown_model_arch_is_an_error():
    audio_in = AudioIn(model_arch="gigantic")
    with pytest.raises(AudioError) as excinfo:
        next(audio_in.lines())
    assert "gigantic" in str(excinfo.value)


def test_on_error_is_logged_as_a_warning(caplog):
    audio_in = AudioIn(model_arch="small")
    with caplog.at_level(logging.WARNING, logger="dankbot.audio"):
        audio_in._on_error(RuntimeError("boom"))
    assert "speech-to-text error: boom" in caplog.text


def test_build_wires_on_error_and_releases_the_engine_when_start_fails(monkeypatch):
    """A `start()` failure after `load()` must not leak the microphone."""
    created: list = []

    class FakeMic:
        def __init__(self):
            created.append(self)
            self.calls: list = []
            self.closed = False

        def language(self, code):
            self.calls.append(("language", code))
            return self

        def model_arch(self, arch):
            self.calls.append(("model_arch", arch))
            return self

        def device(self, index):
            # Reached only when a real mic is visible (resolve_device finds one).
            self.calls.append(("device", index))
            return self

        def on_line(self, cb):
            self.calls.append(("on_line", cb))
            return self

        def on_error(self, cb):
            self.calls.append(("on_error", cb))
            return self

        def load(self):
            self.calls.append(("load",))
            return self

        def start(self):
            raise RuntimeError("no mic")

        def close(self):
            self.closed = True

    fake = types.SimpleNamespace(
        MicTranscriber=FakeMic,
        ModelArch=types.SimpleNamespace(SMALL_STREAMING=4),
    )
    monkeypatch.setitem(sys.modules, "moonshine_voice", fake)
    _install_fake_sounddevice(monkeypatch, [])  # no mic visible -> no device() call

    audio_in = AudioIn(model_arch="small")
    with pytest.raises(AudioError) as excinfo:
        next(audio_in.lines())

    assert "no mic" in str(excinfo.value)
    assert [m.closed for m in created] == [True]  # released on the way out
    calls = created[0].calls
    assert any(name == "on_error" for name, *_ in calls)  # error sink attached
    assert calls.index(("load",)) > calls.index(("on_error", audio_in._on_error))


# -- supervision: an unplugged mic must not leave the loop mute --------------


def test_a_capture_blocksize_reaches_the_transcriber(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [])
    built = _install_fake_moonshine(monkeypatch, [{}])
    audio_in = AudioIn(model_arch="small", blocksize=4096)
    audio_in._build(audio_in._engine())
    assert built[0].blocksize_frames == 4096


def test_without_a_blocksize_the_library_default_stands(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [])
    built = _install_fake_moonshine(monkeypatch, [{}])
    audio_in = AudioIn(model_arch="small")
    audio_in._build(audio_in._engine())
    assert built[0].blocksize_frames is None  # we never override Moonshine's own default


def test_capture_probe_assumes_the_mic_is_fine_when_it_cannot_tell():
    audio_in = AudioIn(model_arch="small")
    assert audio_in._capture_alive() is True  # no transcriber open at all
    audio_in._mic = types.SimpleNamespace(_sd_stream=None)
    assert audio_in._capture_alive() is True  # the library hid its stream
    audio_in._mic = types.SimpleNamespace()  # no _sd_stream attribute at all
    assert audio_in._capture_alive() is True


def test_capture_probe_calls_a_stopped_stream_dead():
    audio_in = AudioIn(model_arch="small")
    audio_in._mic = types.SimpleNamespace(_sd_stream=_FakeStream(active=False))
    assert audio_in._capture_alive() is False
    audio_in._mic = types.SimpleNamespace(_sd_stream=_FakeStream(closed=True))
    assert audio_in._capture_alive() is False


def test_capture_probe_notices_a_frozen_clock():
    audio_in = AudioIn(model_arch="small")
    audio_in._mic = types.SimpleNamespace(_sd_stream=_FakeStream(times=[2.0, 2.0, 2.0]))
    assert audio_in._capture_alive() is True  # the first probe only sets the clock
    assert audio_in._capture_alive() is False  # ...and it has not moved since


def test_capture_probe_is_happy_with_a_moving_clock():
    audio_in = AudioIn(model_arch="small")
    audio_in._mic = types.SimpleNamespace(_sd_stream=_FakeStream())
    assert audio_in._capture_alive() is True
    assert audio_in._capture_alive() is True


def test_an_error_asks_for_a_reconnect():
    audio_in = AudioIn(model_arch="small")
    audio_in._on_error(RuntimeError("boom"))
    assert audio_in._reconnect.is_set()


def test_close_interrupts_a_reconnect_wait():
    audio_in = AudioIn(model_arch="small", reconnect_delay_s=30.0)
    audio_in.close()
    assert audio_in._wait_for_device() is True  # returns at once, no 30 s sleep


def test_a_non_positive_delay_switches_supervision_off():
    """The escape hatch: open once and block, ignoring a stream that dies."""
    audio_in = AudioIn(model_arch="small", reconnect_delay_s=0)
    assert audio_in._wait_for_device() is True  # never sleeps between attempts
    audio_in._mic = types.SimpleNamespace(_sd_stream=_FakeStream(active=False))
    audio_in._queue.put(Utterance("still heard"))
    gen = audio_in._drain()
    try:
        assert next(gen) == Utterance("still heard")  # a dead stream goes unnoticed
    finally:
        audio_in.close()
        gen.close()


def test_the_microphone_is_reopened_after_the_capture_dies(monkeypatch):
    _install_fake_sounddevice(monkeypatch, [])
    built = _install_fake_moonshine(
        monkeypatch,
        [
            {"lines": ["first"], "times": [1.0, 1.0]},  # drains, then the clock freezes
            {"lines": ["second"]},  # a fresh, healthy capture
        ],
    )
    audio_in = AudioIn(model_arch="small", reconnect_delay_s=0.01)
    gen = audio_in.lines()
    try:
        assert next(gen).text == "first"
        assert next(gen).text == "second"  # heard through the re-opened mic
    finally:
        audio_in.close()
        gen.close()
    assert len(built) == 2  # the dead transcriber was replaced, not just released
    assert built[0].closed is True


def test_a_reconnect_waits_for_the_device_to_come_back(monkeypatch, caplog):
    _install_fake_sounddevice(monkeypatch, [])
    built = _install_fake_moonshine(
        monkeypatch,
        [
            {"lines": ["first"], "times": [1.0, 1.0]},  # dies
            {"start_error": "no such device"},  # the replug has not landed yet
            {"lines": ["back"]},  # the array is back
        ],
    )
    audio_in = AudioIn(model_arch="small", reconnect_delay_s=0.01)
    gen = audio_in.lines()
    try:
        assert next(gen).text == "first"
        with caplog.at_level(logging.WARNING, logger="dankbot.audio"):
            assert next(gen).text == "back"  # retried until it could open one
    finally:
        audio_in.close()
        gen.close()
    assert len(built) == 3
    assert built[1].closed is True  # the half-built attempt was released
    assert "microphone unavailable" in caplog.text
