"""TTS orchestration: pick an engine, synthesise, encode, and push.

The orchestrator is the glue between the ``say`` MCP tool (defined in
:mod:`stackchan_mcp.stdio_server`) and the engine implementations
registered in :mod:`stackchan_mcp.tts`. It validates arguments, looks
up an engine, runs the synthesis, encodes the result to Opus, and
hands the frames off to :mod:`stackchan_mcp.audio_stream` for delivery.

The framework half (Engine ABC, registry, validation surface) shipped
in PR1 of Issue #70; PR2 wires the actual VOICEVOX → PCM → Opus →
WebSocket pipeline. The signature stays back-compatible with PR1's
tests: ``gateway`` is keyword-only and may be omitted, in which case
calls that pass validation surface a clear error instead of silently
synthesising audio with no destination.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .audio_utils import (
    DEVICE_FRAME_DURATION_MS,
    DEVICE_SAMPLE_RATE,
    encode_opus_frames,
)
from .base import EngineRegistry, get_registry

if TYPE_CHECKING:
    from ..gateway import Gateway

logger = logging.getLogger(__name__)


#: Default engine name when ``voice`` is omitted from the tool call.
#: VOICEVOX is the canonical default (Issue #70); the concrete engine
#: ships in PR2 of that Issue.
DEFAULT_VOICE = "voicevox"


async def synthesize_and_send(
    arguments: dict[str, Any],
    *,
    gateway: "Gateway | None" = None,
    registry: EngineRegistry | None = None,
) -> dict[str, Any]:
    """Synthesise text via a registered engine and push it to the device.

    Args:
        arguments: MCP tool arguments. Recognised keys:

            * ``text`` (required): non-empty string to speak.
            * ``voice``: engine name; defaults to :data:`DEFAULT_VOICE`.
            * ``speaker_id``: engine-specific speaker identifier
              (e.g. VOICEVOX speaker).
            * ``reference_audio``: path to a reference audio sample
              (e.g. for Irodori voice cloning, PR3).

        gateway: The :class:`Gateway` instance whose
            :attr:`Gateway.esp32` the audio frames are pushed through.
            Required for the pipeline; left optional in the signature
            so callers can inspect validation errors without setting
            up a gateway (e.g. argument-validation tests).

        registry: Engine registry to look up ``voice`` in. Defaults to
            the process-wide registry. Tests inject a fresh registry
            here to avoid leaking state across cases.

    Returns:
        Dict describing the synthesis: ``engine``, ``text``,
        ``speaker_id``, ``frame_count``, ``sample_rate``,
        ``frame_duration_ms``, ``duration_ms``.

    Raises:
        ValueError: if ``text`` is missing / empty / non-string.
        NotImplementedError: if no engine is registered under ``voice``.
            The message lists the registered engines so callers can
            tell whether they need to install an extra (e.g.
            ``pip install stackchan-mcp[tts]``) or pick a different
            ``voice``.
        RuntimeError: if ``gateway`` is omitted, or if no ESP32 device
            is connected when the orchestrator tries to push frames.
    """
    # Validation runs first so callers can probe argument shape without
    # a real gateway / engine.
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
            "Install the relevant extra (e.g. "
            "'pip install stackchan-mcp[tts]' for VOICEVOX) and ensure "
            "the corresponding service (e.g. the VOICEVOX HTTP engine) "
            "is reachable."
        )

    if gateway is None:
        raise RuntimeError(
            "synthesize_and_send requires a 'gateway' argument to push "
            "audio frames; this call appears to be a validation probe "
            "without one."
        )

    if not gateway.esp32.device_connected:
        raise RuntimeError(
            "No ESP32 device connected; cannot deliver synthesised audio."
        )

    speaker_id = arguments.get("speaker_id")
    reference_audio = arguments.get("reference_audio")

    # Engine failures (HTTP errors from VOICEVOX, malformed WAV from
    # the synthesiser, etc.) are translated to RuntimeError so the
    # MCP layer's narrow exception filter still produces clean error
    # JSON. Validation errors (ValueError) are kept distinct so bad
    # arguments stay separable from operational degradation.
    try:
        pcm = await engine.synthesize(
            text,
            speaker_id=speaker_id,
            reference_audio=reference_audio,
        )
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"TTS engine '{voice}' failed: {exc}"
        ) from exc

    if not pcm:
        # An engine returning no PCM is a bug, not a runtime condition;
        # surface it to the caller rather than silently sending zero
        # frames (which would look like the device "ignored" the call).
        raise RuntimeError(
            f"Engine '{voice}' produced no PCM data for the given text."
        )

    # Encode -> push. Materialising the frame list before pushing keeps
    # the count reportable and makes it easy to short-circuit if Opus
    # encoding fails before any audio reaches the wire.
    try:
        opus_frames = list(encode_opus_frames(pcm))
    except Exception as exc:
        raise RuntimeError(f"Opus encoding failed: {exc}") from exc

    # Bracket the binary audio frames in TTS start/stop notifications.
    # The device firmware (Application::OnIncomingAudio) only accepts
    # binary audio frames while in kDeviceStateSpeaking, which is
    # entered on receipt of {"type":"tts","state":"start"} and exited
    # on "stop". Without these notifications the audio frames are
    # silently discarded and the say() tool returns success even
    # though nothing actually plays.
    try:
        await gateway.esp32.send_tts_state("start")
    except ConnectionError as exc:
        raise RuntimeError(
            f"Device disconnected before TTS start notification: {exc}"
        ) from exc

    sent = 0
    push_error: ConnectionError | None = None
    try:
        for frame in opus_frames:
            try:
                await gateway.esp32.send_audio_frame(frame)
            except ConnectionError as exc:
                # Stop pushing on the first disconnect, but fall through
                # to the stop notification (see finally) so that *if*
                # the device is somehow still listening it returns to
                # idle rather than staying in speaking forever.
                push_error = exc
                break
            sent += 1
    finally:
        try:
            await gateway.esp32.send_tts_state("stop")
        except ConnectionError:
            # If the device dropped, it'll return to idle on its own
            # when the WebSocket close lands; nothing to do here.
            pass

    if push_error is not None:
        raise RuntimeError(
            f"Device disconnected after sending "
            f"{sent}/{len(opus_frames)} frames: {push_error}"
        ) from push_error

    duration_ms = sent * DEVICE_FRAME_DURATION_MS

    logger.info(
        "say(): engine=%s speaker=%s frames=%d duration_ms=%d",
        voice,
        speaker_id if speaker_id is not None else "default",
        sent,
        duration_ms,
    )

    return {
        "engine": voice,
        "text": text,
        "speaker_id": speaker_id,
        "frame_count": sent,
        "sample_rate": DEVICE_SAMPLE_RATE,
        "frame_duration_ms": DEVICE_FRAME_DURATION_MS,
        "duration_ms": duration_ms,
    }
