"""Defaults and the ``Settings`` value object.

Config resolution is *CLI flag → environment variable → default*; click implements
that precedence for us (``default=`` + ``envvar=`` on every option), so this
module only owns the default values and the frozen dataclass they feed.

Env names mirror ``launch-funny.sh``'s ``TOUCHY_BOT_*`` but are rebranded
``DANKBOT_*`` (the repo is finally called touchy-dankbot). The script's
``TOUCHY_BOT_NGL`` — GPU layers to offload — has no remote analogue and is
dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The Ollama server: the *local* one, i.e. `ollama serve` on the same box
#: (verified 2026-11-18, Ollama 0.40.15). The original remote box — the
#: Tailscale host ``https://kai.pygmy-map.ts.net``, verified 2026-11-04 — still
#: works; point ``--url`` / ``DANKBOT_URL`` at it to use it instead.
DEFAULT_URL = "http://localhost:11434"

#: Model default. Rocinante-X 12B (Q4_K_M) is the current pick after the
#: jokemark bake-off: the funniest of the batch and small enough (Q4_K_M) for
#: the Pi. Human-Like Mistral Nemo (12B, Q8_0) is the runner-up and Hermes-3-8B
#: the disciplined fallback:
#: ``--model hf.co/bartowski/Human-Like-Mistral-Nemo-Instruct-2407-GGUF:Q8_0`` /
#: ``--model hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF:latest``.
DEFAULT_MODEL = "hf.co/TheDrummer/Rocinante-X-12B-v1-GGUF:Q4_K_M"

#: Loosened sampler — the launch-funny.sh lesson: wit needs creative word choice.
#: 0.9 suits the 12B picks (see ``jokemark.md`` for the bake-off).
DEFAULT_TEMPERATURE = 0.9
DEFAULT_MIN_P = 0.05
DEFAULT_CTX = 4096
DEFAULT_MAX_TOKENS = 120

#: HTTP timeout (seconds). Generous: a cold 12B+ model can take a while to load.
DEFAULT_TIMEOUT = 120.0

#: Model-pull timeout (seconds), for jokemark's fetch-if-missing. Much longer
#: than a chat request: pulling the 12B Q8_0 GGUF is ~13 GB, minutes over a home
#: uplink, not seconds.
DEFAULT_PULL_TIMEOUT = 3600.0

#: Fetch a model the server does not have yet? ``reply`` / ``listen`` ask what
#: the server has at startup and pull the model when it is missing, so a fresh
#: box works on the first quip instead of failing with a 404. A multi-GB
#: download is a surprise, hence the escape hatch (``--no-pull`` /
#: ``DANKBOT_PULL=0``). jokemark has the same flag for the same reason.
DEFAULT_PULL = True


@dataclass(frozen=True)
class Settings:
    """Everything the :class:`~dankbot.server.ChatClient` needs to talk to the server."""

    url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    min_p: float = DEFAULT_MIN_P
    ctx: int = DEFAULT_CTX
    max_tokens: int = DEFAULT_MAX_TOKENS
    native: bool = False
    timeout: float = DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# Audio in (the "listen" stage) — see docs/plans/audioin.md
# ---------------------------------------------------------------------------

#: Case-insensitive substring of the capture device name to auto-select. The
#: reSpeaker XVF3800 shows up as e.g. "reSpeaker XVF3800 4-Mic Array: USB Audio
#: (hw:2,0)", so a short hint finds it whatever PortAudio index it lands on.
DEFAULT_MIC_HINT = "respeaker"

#: Moonshine streaming model size: tiny (34M) | base | small (123M) | medium
#: (245M). Small is the Pi-friendly default; see the plan's Q2.
DEFAULT_MODEL_ARCH = "small"

#: Moonshine's VAD sensitivity (``vad_threshold``), i.e. where the probability
#: of speech has to sit for the engine to call a segment finished. The library's
#: own default: "lower → longer segments; higher → shorter chunks". A speaker
#: who pauses mid-sentence gets split into separate ``LineCompleted`` events at
#: the default, so lower it (0.35–0.4) to keep a slight pause inside one line;
#: 0 disables VAD entirely and lets ``vad_max_segment_duration`` (15 s) chunk the
#: audio instead. Too low starts treating room noise as speech. See
#: https://moonshine-voice.readthedocs.io/en/latest/api/options/.
DEFAULT_VAD_THRESHOLD = 0.2

#: Seconds of VAD scores Moonshine averages when deciding whether someone is
#: speaking (the library's own default is 0.5). A longer window rides through a
#: brief dip in speech probability, so a slight mid-sentence pause no longer
#: ends the line — 1.0 s is the default here. The ~0.5 s of detection lag that
#: introduces is what ``vad_look_behind_sample_count`` (8192 samples ≈ 0.51 s at
#: 16 kHz) prepends, so the first word is not clipped. It costs that much extra
#: latency before a line starts; much past 1 s risks eating the onset. A value
#: ``<= 0`` is rejected by the CLI (it would average no scores at all).
DEFAULT_VAD_WINDOW_DURATION = 1.0

#: Answer only utterances with at least this many *content* words (filler
#: stripped) — "a text snippet of at least a few words" (plan Q3).
DEFAULT_MIN_WORDS = 3

#: Also ignore utterances shorter than this many seconds (when the duration is
#: known; the --fake replay has none).
DEFAULT_MIN_DURATION_S = 0.6

#: Suppress an utterance identical to the previous one within this many seconds
#: (plan Q9).
DEFAULT_DEBOUNCE_S = 5.0

#: Seconds between microphone liveness checks *and* between attempts to re-open
#: the capture device after it goes away. ``dankbot listen`` watches its
#: PortAudio stream and reconnects, because an unplugged USB array would
#: otherwise leave the transcriber open but silent for the life of the process
#: (see ``docs/plans/micreconnect.md``). A value ``<= 0`` disables supervision
#: and keeps the old "open once, block forever" behaviour.
DEFAULT_RECONNECT_DELAY_S = 2.0

#: Frames per microphone capture callback (Moonshine's own default). Bigger
#: means fewer, longer blocks — and, because PortAudio sizes the ALSA ring
#: buffer from the period count, more slack before an *input overflow* makes the
#: driver drop audio (``MicTranscriber: input overflow``: the callback was not
#: serviced in time, which is a CPU-contention symptom, not an audio one). At
#: the array's 48 kHz, 1024 frames is ~21 ms per block and 4096 is ~85 ms — the
#: capture→transcript delay grows by the same amount, which is nothing beside a
#: model round trip. Raise it when overflow lines are continuous rather than a
#: startup burst; see ``--mic-blocksize``.
DEFAULT_MIC_BLOCKSIZE = 1024


@dataclass(frozen=True)
class AudioSettings:
    """Everything the ``listen`` stage's capture + gate needs."""

    device: int | str | None = None
    mic_hint: str | None = DEFAULT_MIC_HINT
    model_arch: str = DEFAULT_MODEL_ARCH
    vad_threshold: float = DEFAULT_VAD_THRESHOLD
    vad_window_duration: float = DEFAULT_VAD_WINDOW_DURATION
    min_words: int = DEFAULT_MIN_WORDS
    min_duration_s: float = DEFAULT_MIN_DURATION_S
    debounce_s: float = DEFAULT_DEBOUNCE_S
    reconnect_delay_s: float = DEFAULT_RECONNECT_DELAY_S
    mic_blocksize: int = DEFAULT_MIC_BLOCKSIZE


# ---------------------------------------------------------------------------
# Display out (the "LED out" / "LCD out" stage) — see docs/plans/ledout.md and
# docs/plans/lcdout.md
# ---------------------------------------------------------------------------

#: Which layout to build on the pad: ``auto`` picks by panel height
#: (``<= 32`` px scrolls a one-line marquee, taller wraps in a text field);
#: ``scroll`` / ``multiline`` force one. Lives here so the CLI's default follows
#: the same flag → env → default convention as everything else.
DEFAULT_DISPLAY_MODE = "auto"
