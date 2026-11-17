"""End-to-end: ``display.py`` → ``touchy_open`` → TCP → simulator.

Uses a **standalone** :class:`touchy_pad.sim.server.SimServer` (no in-process sim
*registry*, so ``TOUCHY_SIM_URL`` is the only candidate and a real pad attached to
the machine is never touched) with a throwaway ``tmp_path`` filesystem. That
exercises the real transport, framing and the sim's property engine. Skipped when
the host library isn't installed. See ``docs/plans/ledout.md`` (phase D2).
"""

from __future__ import annotations

import pytest

pytest.importorskip("touchy_pad.api")

from touchy_pad.api import LongMode
from touchy_pad.sim.device import _find_widget
from touchy_pad.sim.server import SimServer

from voicebot.commands import COLOURS, Run
from voicebot.display import LABEL_ID, STARTUP_TEXT, PadDisplay


@pytest.fixture
def sim(monkeypatch, tmp_path):
    """A loopback sim on an ephemeral port, reachable through ``TOUCHY_SIM_URL``."""
    with SimServer(port=0, fs_root=tmp_path, display_size=(32, 8)) as server:
        monkeypatch.setenv("TOUCHY_SIM_URL", f"tcp://127.0.0.1:{server.port}")
        yield server


@pytest.fixture
def lcd_sim(monkeypatch, tmp_path):
    """The same loopback sim, provisioned as the 480x272 LCD panel."""
    with SimServer(port=0, fs_root=tmp_path, display_size=(480, 272)) as server:
        monkeypatch.setenv("TOUCHY_SIM_URL", f"tcp://127.0.0.1:{server.port}")
        yield server


def _label(server):
    screen = server.device.active_screen
    assert screen is not None, "the sim has no active screen"
    return _find_widget(screen, LABEL_ID)


def test_a_coloured_quip_lands_on_the_simulated_panel(sim):
    display = PadDisplay(clear_after=0)
    try:
        display.show([Run("Danger!", COLOURS["red"])])
        label = _label(sim)
        assert label is not None, "the voicebot label was never built"
        assert label.label.text == "#FF0000 Danger!#"
        assert label.label.recolor is True
    finally:
        display.close()


def test_the_banner_shows_then_an_empty_quip_clears(sim):
    display = PadDisplay(clear_after=0)
    try:
        display.start()
        assert _label(sim).label.text == STARTUP_TEXT
        display.show([])  # an empty quip blanks the marquee
        assert _label(sim).label.text == ""
    finally:
        display.close()


def test_a_tall_panel_gets_a_wrapped_text_field(lcd_sim):
    """The same display code picks the LCD layout from the reported height."""
    display = PadDisplay(clear_after=0)
    try:
        display.show([Run("Danger!", COLOURS["red"])])
        label = _label(lcd_sim)
        assert label is not None, "the voicebot label was never built"
        assert label.label.text == "#FF0000 Danger!#"
        assert label.label.recolor is True
        assert label.label.long_mode == LongMode.LONG_MODE_DOTS
    finally:
        display.close()
