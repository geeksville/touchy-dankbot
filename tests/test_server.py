"""ChatClient's model-fetch helpers, with requests faked — never touches the network."""

from __future__ import annotations

import pytest

from voicebot.config import DEFAULT_PULL_TIMEOUT, Settings
from voicebot.server import ChatClient, VoicebotError


class FakeResponse:
    """The little bit of ``requests.Response`` the pull path uses."""

    def __init__(self, payload: dict | None = None, error: Exception | None = None):
        self._payload = payload or {}
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> dict:
        return self._payload


def test_pull_model_posts_the_native_pull(monkeypatch):
    seen: dict[str, object] = {}

    def post(url, json, timeout):
        seen.update(url=url, json=json, timeout=timeout)
        return FakeResponse({"status": "success"})

    monkeypatch.setattr("voicebot.server.requests.post", post)
    ChatClient(Settings(url="http://ollama:11434")).pull_model("hf.co/x/y:Q8_0")
    assert seen == {
        "url": "http://ollama:11434/api/pull",
        "json": {"model": "hf.co/x/y:Q8_0", "stream": False},
        "timeout": DEFAULT_PULL_TIMEOUT,
    }


def test_pull_model_accepts_a_custom_timeout(monkeypatch):
    seen: dict[str, object] = {}

    def post(url, json, timeout):
        seen.update(timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr("voicebot.server.requests.post", post)
    ChatClient(Settings()).pull_model("m", timeout=5)
    assert seen["timeout"] == 5.0


def test_pull_model_normalises_request_errors(monkeypatch):
    def post(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr("voicebot.server.requests.post", post)
    with pytest.raises(VoicebotError, match="RuntimeError: nope"):
        ChatClient(Settings()).pull_model("m")


def test_pull_model_reports_an_error_field(monkeypatch):
    monkeypatch.setattr(
        "voicebot.server.requests.post",
        lambda *args, **kwargs: FakeResponse({"error": "model not found"}),
    )
    with pytest.raises(VoicebotError, match="model not found"):
        ChatClient(Settings()).pull_model("m")


def test_has_model_tolerates_the_implicit_latest_tag(monkeypatch):
    client = ChatClient(Settings())
    monkeypatch.setattr(client, "list_models", lambda: ["a:latest", "b", "c:Q8_0"])
    assert client.has_model("a")
    assert client.has_model("a:latest")
    assert client.has_model("b")
    assert client.has_model("c:Q8_0")
    assert not client.has_model("c")  # only ':latest' is implicit
    assert not client.has_model("missing")


def test_has_model_surfaces_server_failures(monkeypatch):
    client = ChatClient(Settings())

    def boom():
        raise VoicebotError("down")

    monkeypatch.setattr(client, "list_models", boom)
    with pytest.raises(VoicebotError, match="down"):
        client.has_model("a")
