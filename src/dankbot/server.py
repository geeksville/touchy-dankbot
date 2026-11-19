"""The only module that talks to the network.

Two surfaces are supported against the remote Ollama server:

* the **OpenAI-compatible** ``/v1`` API (default) — portable to any OpenAI-ish
  server (vLLM, a llama.cpp ``llama serve``, …). Ollama-specific sampler knobs
  ride along in an ``options`` object via ``extra_body``;
* Ollama's **native** ``/api/chat`` (``--native``) — needed when a knob such as
  ``min_p`` must be honoured *exactly* (``/v1`` may silently drop it).

Every failure — bad URL, timeout, HTTP error — is normalised to
:class:`DankbotError` so the CLI has one thing to catch.
"""

from __future__ import annotations

import logging
import os
import time

import requests
from openai import OpenAI

from .config import DEFAULT_PULL_TIMEOUT, Settings

log = logging.getLogger(__name__)


def _strip_latest(name: str) -> str:
    """``foo:latest`` → ``foo`` — Ollama adds the tag when a pull omits it."""
    return name.removesuffix(":latest")


def _open_fds() -> int | None:
    """The process's open file descriptors, when the OS can tell us.

    A cheap leak gauge for the DEBUG chat line: one socket per request that is
    never closed shows up here as a count that only climbs. ``None`` off Linux
    (or with ``/proc`` hidden) — the gauge is optional, never fatal.
    """
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:  # pragma: no cover - not Linux
        return None


class DankbotError(RuntimeError):
    """Anything that stops us getting a reply from the model server."""


class ChatClient:
    """A thin wrapper around the server, configured by a :class:`Settings`."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        #: Exposed so callers can stamp it onto each :class:`~dankbot.responder.Reply`.
        self.model = settings.model

    # -- chat --------------------------------------------------------------

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Return the assistant text for *messages* (OpenAI chat shape).

        Logs the sizes and the round-trip time at DEBUG — the companion to the
        listen loop's latency split: it shows the *prompt* (so an unexpectedly
        empty or growing request is visible) and the process's open file
        descriptors, which is where a leaked connection would show up.
        """
        heard = messages[-1].get("content", "") if messages else ""
        started = time.monotonic()
        if self._s.native:
            text = self._chat_native(messages)
        else:
            text = self._chat_openai(messages)
        fds = _open_fds()
        log.debug(
            "chat: %d prompt chars (%d heard) → %d reply chars in %.2fs%s",
            sum(len(message.get("content", "")) for message in messages),
            len(heard),
            len(text),
            time.monotonic() - started,
            "" if fds is None else f" [fds={fds}]",
        )
        return text

    def _chat_openai(self, messages: list[dict[str, str]]) -> str:
        # One client per request (the CLI builds a ChatClient per command), so
        # close it: an unclosed OpenAI holds an httpx connection pool, and a
        # long listen session would churn sockets it never releases.
        try:
            with OpenAI(
                base_url=f"{self._s.url}/v1",
                api_key="ollama",  # required by the SDK; ignored by Ollama
                timeout=self._s.timeout,
            ) as client:
                resp = client.chat.completions.create(
                    model=self._s.model,
                    messages=messages,
                    temperature=self._s.temperature,
                    max_tokens=self._s.max_tokens,
                    # Ollama forwards these; non-Ollama servers ignore them.
                    extra_body={"options": {"min_p": self._s.min_p, "num_ctx": self._s.ctx}},
                )
        except Exception as exc:
            raise DankbotError(f"{type(exc).__name__}: {exc}") from exc
        return resp.choices[0].message.content or ""

    def _chat_native(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self._s.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self._s.temperature,
                "min_p": self._s.min_p,
                "num_ctx": self._s.ctx,
                "num_predict": self._s.max_tokens,
            },
        }
        try:
            resp = requests.post(f"{self._s.url}/api/chat", json=payload, timeout=self._s.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise DankbotError(f"{type(exc).__name__}: {exc}") from exc
        return data.get("message", {}).get("content", "") or ""

    # -- models ------------------------------------------------------------

    def list_models(self) -> list[str]:
        """The model names the server currently has pulled."""
        if self._s.native:
            return self._models_native()
        return self._models_openai()

    def _models_openai(self) -> list[str]:
        client = OpenAI(base_url=f"{self._s.url}/v1", api_key="ollama", timeout=self._s.timeout)
        try:
            return [m.id for m in client.models.list().data]
        except Exception as exc:
            raise DankbotError(f"{type(exc).__name__}: {exc}") from exc

    def _models_native(self) -> list[str]:
        try:
            resp = requests.get(f"{self._s.url}/api/tags", timeout=self._s.timeout)
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        except Exception as exc:
            raise DankbotError(f"{type(exc).__name__}: {exc}") from exc

    # -- fetch -------------------------------------------------------------

    def has_model(self, name: str) -> bool:
        """True if *name* is already pulled (tolerating Ollama's implicit ``:latest``)."""
        wanted = _strip_latest(name)
        return any(_strip_latest(model) == wanted for model in self.list_models())

    def pull_model(self, name: str, *, timeout: float = DEFAULT_PULL_TIMEOUT) -> None:
        """Ask the server to pull *name* (blocking; ``stream`` off).

        ``/api/pull`` only exists on Ollama's native API — this is independent of
        the ``--native`` chat setting. A pull can take minutes for a multi-GB
        GGUF, hence its own (much longer) default timeout.
        """
        try:
            resp = requests.post(
                f"{self._s.url}/api/pull",
                json={"model": name, "stream": False},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise DankbotError(f"{type(exc).__name__}: {exc}") from exc
        if data.get("error"):
            raise DankbotError(f"pull {name}: {data['error']}")

    def ensure_model(self, name: str) -> bool:
        """Fetch *name* when the server does not have it yet — ``True`` if it did.

        ``dankbot reply`` / ``listen`` call this once at startup, so a fresh box
        pulls its model on first use instead of failing on the first quip with a
        404 from ``/v1``.

        Best-effort by design: it must never turn a *working* setup into a
        failure. A server we cannot ask — down, or not Ollama at all, where
        ``/api/pull`` does not exist (the ``/v1`` path stays portable to vLLM /
        ``llama.cpp``) — only earns a log line, and the chat call that follows
        reports the real problem.
        """
        try:
            if self.has_model(name):
                return False
        except DankbotError as exc:
            log.debug("cannot list the server's models (%s); not fetching %s", exc, name)
            return False
        # WARNING, not INFO: the download blocks for minutes and ``reply`` logs
        # at the default WARNING level, so a quieter line would leave the user
        # staring at a silent terminal wondering whether it had hung.
        log.warning(
            "model %s is not on %s — fetching it (this can take minutes)", name, self._s.url
        )
        try:
            self.pull_model(name)
        except DankbotError as exc:
            log.warning("could not fetch %s: %s", name, exc)
            return False
        return True
