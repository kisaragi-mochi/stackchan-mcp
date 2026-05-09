"""TTS framework for Phase 4 (Issue #70).

This package provides the engine-agnostic skeleton for the gateway-side
``say(text)`` MCP tool. Concrete engine implementations land in follow-up
PRs:

- ``voicevox.py`` — VOICEVOX HTTP API (PR2)
- ``irodori.py`` — Irodori-TTS-500M voice cloning (PR3)

The framework here only exposes the abstract :class:`TTSEngine`, an
:class:`EngineRegistry`, and :func:`synthesize_and_send` — the orchestrator
that the ``say`` tool will call once at least one engine is registered.
"""

from .base import EngineRegistry, TTSEngine, get_registry
from .orchestrator import DEFAULT_VOICE, synthesize_and_send

__all__ = [
    "DEFAULT_VOICE",
    "EngineRegistry",
    "TTSEngine",
    "get_registry",
    "synthesize_and_send",
]
