"""Opt-in smoke test against the real remote server.

Skipped unless ``DANKBOT_LIVE=1`` is set, so CI stays network-free. Run with:

    DANKBOT_LIVE=1 just test
"""

from __future__ import annotations

import os

import pytest

from dankbot.config import Settings
from dankbot.responder import Responder
from dankbot.server import ChatClient

pytestmark = pytest.mark.live

_LIVE = os.environ.get("DANKBOT_LIVE") == "1"


@pytest.mark.skipif(not _LIVE, reason="set DANKBOT_LIVE=1 to hit the remote server")
def test_remote_server_answers():
    client = ChatClient(Settings())
    responder = Responder(client.chat, model=client.model)
    reply = responder.respond("the bread is done baking")
    assert not reply.skip
    assert reply.text
