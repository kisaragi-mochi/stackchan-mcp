"""TTS orchestration: pick an engine, synthesise, encode, and push.

The orchestrator is the glue between the ``say`` MCP tool (defined in
:mod:`stackchan_mcp.stdio_server`) and the concrete engines that arrive
in follow-up PRs of Issue #70.

PR1 (Issue #70) lands this skeleton only: calling
:func:`synthesize_and_send` succeeds at the validation stage but raises
``NotImplementedError`` once it would need to drive a real engine. The
intent is to nail down the public surface — argument names, validation
errors, and the return shape — so that PR2 (VOICEVOX) and PR3 (Irodori)
only have to wire engines into the registry.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import EngineRegistry, get_registry

logger = logging.getLogger(__name__)


#: Default engine name when ``voice`` is omitted from the tool call.
#: VOICEVOX is the planned default (Issue #70); the concrete engine
#: arrives in PR2.
DEFAULT_VOICE = "voicevox"


async def synthesize_and_send(
    arguments: dict[str, Any],
    *,
    registry: EngineRegistry | None = None,
) -> dict[str, Any]:
    """Synthesise text via a registered engine and (eventually) push to device.

    Args:
        arguments: MCP tool arguments. Recognised keys:

            * ``text`` (required): non-empty string to speak.
            * ``voice``: engine name; defaults to :data:`DEFAULT_VOICE`.
            * ``speaker_id``: engine-specific speaker identifier
              (e.g. VOICEVOX speaker).
            * ``reference_audio``: path to a reference audio sample
              (e.g. for Irodori voice cloning).

        registry: Engine registry to look up ``voice`` in. Defaults to
            the process-wide registry. Tests inject a fresh registry
            here to avoid leaking state.

    Returns:
        A dict describing the synthesis once concrete engines are wired
        up. PR1 never reaches that path.

    Raises:
        ValueError: if ``text`` is missing or empty.
        NotImplementedError: until a matching engine is registered.
            The message lists the registered engine names so callers
            can tell whether they need to install an extra (e.g.
            ``pip install stackchan-mcp[tts-voicevox]``) or pass a
            different ``voice``.
    """
    text = arguments.get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("'text' is required and must be a non-empty string")

    voice_raw = arguments.get("voice", DEFAULT_VOICE)
    voice = voice_raw if isinstance(voice_raw, str) and voice_raw else DEFAULT_VOICE

    reg = registry if registry is not None else get_registry()
    engine = reg.get(voice)

    if engine is None:
        available = reg.names()
        raise NotImplementedError(
            f"TTS engine '{voice}' is not registered. "
            f"Available engines: {available or '(none)'}. "
            "Concrete engines (VOICEVOX, Irodori) land in follow-up PRs "
            "of Issue #70; install the relevant extra "
            "(e.g. 'pip install stackchan-mcp[tts-voicevox]') once the "
            "PR is merged."
        )

    # The pipeline below is the contract that follow-up PRs must
    # satisfy. PR1 stops short of executing it so that merging the
    # skeleton does not appear to "almost work".
    #
    #   pcm = await engine.synthesize(
    #       text,
    #       speaker_id=arguments.get("speaker_id"),
    #       reference_audio=arguments.get("reference_audio"),
    #   )
    #   opus_frames = encode_opus_16k_mono(pcm)
    #   for frame in opus_frames:
    #       await gateway.esp32.send_audio_frame(frame)
    #   return {
    #       "engine": voice,
    #       "text": text,
    #       "frame_count": len(opus_frames),
    #       "duration_ms": ...,
    #   }
    raise NotImplementedError(
        "TTS orchestration pipeline (PCM synthesis, Opus encoding, "
        "WebSocket push) is not wired up yet. PR1 ships the framework "
        "only; engine implementations land in follow-up PRs of Issue #70."
    )
