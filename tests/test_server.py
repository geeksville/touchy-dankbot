"""ChatClient's model-fetch helpers, with requests faked — never touches the network."""

from __future__ import annotations

import logging
import types

import pytest

from dankbot.config import DEFAULT_PULL_TIMEOUT, Settings
from dankbot.server import ChatClient, DankbotError


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

    monkeypatch.setattr("dankbot.server.requests.post", post)
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

    monkeypatch.setattr("dankbot.server.requests.post", post)
    ChatClient(Settings()).pull_model("m", timeout=5)
    assert seen["timeout"] == 5.0


def test_pull_model_normalises_request_errors(monkeypatch):
    def post(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr("dankbot.server.requests.post", post)
    with pytest.raises(DankbotError, match="RuntimeError: nope"):
        ChatClient(Settings()).pull_model("m")


def test_pull_model_reports_an_error_field(monkeypatch):
    monkeypatch.setattr(
        "dankbot.server.requests.post",
        lambda *args, **kwargs: FakeResponse({"error": "model not found"}),
    )
    with pytest.raises(DankbotError, match="model not found"):
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
        raise DankbotError("down")

    monkeypatch.setattr(client, "list_models", boom)
    with pytest.raises(DankbotError, match="down"):
        client.has_model("a")


def test_ensure_model_fetches_only_a_model_the_server_lacks(monkeypatch, caplog):
    client = ChatClient(Settings(url="http://ollama:11434"))
    monkeypatch.setattr(client, "list_models", lambda: ["a:latest"])
    pulled: list[str] = []
    monkeypatch.setattr(client, "pull_model", pulled.append)
    with caplog.at_level(logging.WARNING, logger="dankbot.server"):
        assert client.ensure_model("a") is False  # already pulled
        assert client.ensure_model("b") is True
    assert pulled == ["b"]
    # The fetch blocks for minutes, so it says so before it starts.
    assert "model b is not on http://ollama:11434" in caplog.text


def test_ensure_model_does_not_pull_when_the_server_cannot_be_asked(monkeypatch):
    """A down (or non-Ollama) server must not turn into a failed startup."""
    client = ChatClient(Settings())

    def boom():
        raise DankbotError("down")

    monkeypatch.setattr(client, "list_models", boom)
    monkeypatch.setattr(client, "pull_model", lambda name: pytest.fail("must not pull"))
    assert client.ensure_model("a") is False


def test_ensure_model_survives_a_failed_fetch(monkeypatch, caplog):
    """A pull that fails is logged, not raised: the chat call reports it."""
    client = ChatClient(Settings())
    monkeypatch.setattr(client, "list_models", list)

    def boom(name):
        raise DankbotError("no such model")

    monkeypatch.setattr(client, "pull_model", boom)
    with caplog.at_level(logging.WARNING, logger="dankbot.server"):
        assert client.ensure_model("a") is False
    assert "could not fetch a: no such model" in caplog.text


# -- chat instrumentation: what we send, and for how long ---------------------


def test_chat_logs_the_prompt_and_reply_sizes_at_debug(monkeypatch, caplog):
    client = ChatClient(Settings())
    monkeypatch.setattr(client, "_chat_openai", lambda messages: "a quip")
    with caplog.at_level(logging.DEBUG, logger="dankbot.server"):
        assert client.chat([{"role": "user", "content": "hello there"}]) == "a quip"
    assert "11 prompt chars (11 heard)" in caplog.text
    assert "6 reply chars" in caplog.text


def test_chat_closes_the_openai_client_it_built(monkeypatch):
    """A client per request must be closed, or a long listen session leaks it."""
    closed: list[bool] = []

    class FakeCompletions:
        def create(self, **kwargs):
            message = types.SimpleNamespace(content="a quip")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append(True)

    monkeypatch.setattr("dankbot.server.OpenAI", FakeClient)
    assert ChatClient(Settings()).chat([{"role": "user", "content": "hi"}]) == "a quip"
    assert closed == [True]
