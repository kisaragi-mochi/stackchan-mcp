"""TTS framework for Phase 4 (Issue #70).

This package provides the engine-agnostic skeleton for the gateway-side
``say(text)`` MCP tool plus the concrete VOICEVOX engine. The Irodori
voice-cloning engine arrives in a follow-up PR (``irodori.py``, PR3).

The package exports :class:`TTSEngine`, an :class:`EngineRegistry`, the
:func:`synthesize_and_send` orchestrator, and registers the default
VOICEVOX engine at import time. Engines whose modules require optional
extras to import are registered behind ``try / except ImportError`` so
the framework still works when the corresponding extra is missing.
"""

from __future__ import annotations

import logging
from typing import Callable

from .base import EngineRegistry, TTSEngine, get_registry
from .orchestrator import (
    DEFAULT_VOICE,
    send_pcm_audio,
    send_pcm_stream,
    synthesize_and_send,
)

_logger = logging.getLogger(__name__)


def _try_register(register_fn: Callable[[], None], engine_label: str) -> None:
    """Run ``register_fn`` and swallow ImportErrors.

    Used so an engine whose top-level module needs an optional extra
    (e.g. PR3's Irodori importing torch / transformers) can fail to
    register cleanly without breaking the rest of the framework. The
    VOICEVOX engine module itself imports fine without any extras —
    httpx is only imported inside :meth:`VoicevoxEngine.synthesize`.
    """
    try:
        register_fn()
    except ImportError as exc:
        _logger.debug("Skipping %s engine registration: %s", engine_label, exc)


def _register_voicevox() -> None:
    from .voicevox import VoicevoxEngine

    get_registry().register(VoicevoxEngine())


def _register_irodori() -> None:
    from .irodori import IrodoriEngine

    get_registry().register(IrodoriEngine())


_try_register(_register_voicevox, "voicevox")
_try_register(_register_irodori, "irodori")


__all__ = [
    "DEFAULT_VOICE",
    "EngineRegistry",
    "TTSEngine",
    "get_registry",
    "send_pcm_audio",
    "send_pcm_stream",
    "synthesize_and_send",
]
