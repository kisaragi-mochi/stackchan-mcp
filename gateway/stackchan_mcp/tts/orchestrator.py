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

import asyncio
import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from .audio_utils import (
    DEVICE_FRAME_DURATION_MS,
    DEVICE_SAMPLE_RATE,
    encode_opus_frames,
    resample_pcm16_linear,
)
from .base import EngineRegistry, get_registry

if TYPE_CHECKING:
    from ..gateway import Gateway

#: Delay between the ``tts.start`` notification and the first audio
#: frame, in seconds. Firmware dispatches the state transition through
#: ``Schedule()`` (queued onto the main task), so the first frame can
#: race the ``kDeviceStateSpeaking`` transition and be discarded by
#: ``OnIncomingAudio``. 50 ms is well above typical scheduling latency
#: but well below human-perceptible delay.
TTS_START_TRANSITION_DELAY_S = 0.05

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

    # WebSocket protocol version gate. The firmware decodes raw Opus
    # binary frames only on protocol v1; v2/v3 wrap each binary message
    # in a BinaryProtocol header that this gateway does not yet emit.
    # Streaming raw frames to a v2/v3 device makes the firmware parse
    # Opus bytes as header fields, so the audio never plays — yet
    # without this check ``say()`` would still report success. Fail
    # fast with a clear, actionable error instead. BinaryProtocol
    # header wrapping is tracked as a follow-up to Issue #70.
    connection = getattr(gateway.esp32, "connection", None)
    proto_version = getattr(connection, "protocol_version", 1)
    if proto_version != 1:
        raise RuntimeError(
            f"TTS requires WebSocket protocol v1, but the connected "
            f"device negotiated v{proto_version}. Rebuild the firmware "
            "with v1 (the default for this repository) — v2/v3 "
            "BinaryProtocol header wrapping is not yet supported."
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

    # Hand the PCM off to the shared encode-and-push path. Engines that
    # have already resampled to DEVICE_SAMPLE_RATE (the documented
    # TTSEngine contract) need no further conversion here.
    result = await send_pcm_audio(
        gateway,
        pcm,
        source_label=f"engine:{voice}",
    )

    logger.info(
        "say(): engine=%s speaker=%s frames=%d duration_ms=%d",
        voice,
        speaker_id if speaker_id is not None else "default",
        result["frame_count"],
        result["duration_ms"],
    )

    return {
        "engine": voice,
        "text": text,
        "speaker_id": speaker_id,
        "frame_count": result["frame_count"],
        "sample_rate": result["sample_rate"],
        "frame_duration_ms": result["frame_duration_ms"],
        "duration_ms": result["duration_ms"],
    }


async def send_pcm_audio(
    gateway: "Gateway",
    pcm: bytes,
    *,
    source_rate: int = DEVICE_SAMPLE_RATE,
    source_label: str = "external",
) -> dict[str, Any]:
    """Encode mono PCM and push as Opus frames to the connected device.

    This is the shared back-half of the TTS pipeline. ``synthesize_and_send``
    delegates here after running its engine; external producers (an HTTP
    PCM bridge, a sound-effect player, another voice stack like the SAIVerse
    voice-tts addon) can call this directly to push pre-synthesised audio
    without going through a registered :class:`TTSEngine`.

    Args:
        gateway: The :class:`Gateway` instance whose
            :attr:`Gateway.esp32` the audio frames are pushed through.
        pcm: Signed-16-bit little-endian mono PCM bytes. Must be
            non-empty.
        source_rate: Sample rate of ``pcm``. Defaults to
            :data:`DEVICE_SAMPLE_RATE` (16 kHz). When the source is at a
            different rate (e.g. voice-tts produces 32 kHz) the bytes
            are resampled linearly before Opus encoding; engines that
            already resample to the device rate internally should leave
            this at the default.
        source_label: Label that appears in the orchestrator log line so
            external callers can be traced separately from engine-driven
            synthesis (e.g. ``"voice-tts"``, ``"sfx:notification"``).

    Returns:
        Dict describing the push: ``source``, ``frame_count``,
        ``sample_rate``, ``frame_duration_ms``, ``duration_ms``.
        ``sample_rate`` is always :data:`DEVICE_SAMPLE_RATE` because that
        is what the device actually decoded, regardless of the source
        rate.

    Raises:
        RuntimeError: if ``pcm`` is empty, ``gateway`` is missing, no
            device is connected, the negotiated protocol is not v1, Opus
            encoding fails, or the device disconnects mid-stream.
    """
    if not pcm:
        # Surface empty input as a clear bug rather than silently doing
        # nothing — same reasoning as the "engine produced no PCM" guard
        # in synthesize_and_send.
        raise RuntimeError(
            f"send_pcm_audio: PCM payload was empty (source={source_label!r})."
        )

    # Validate source_rate before it reaches resample_pcm16_linear.
    # The resampler computes ``n_dst = n_src * dst_rate // src_rate``,
    # which raises ZeroDivisionError on 0 and produces nonsense for
    # negatives — neither of which the caller's narrow ``RuntimeError``
    # filter translates cleanly to an MCP-facing error. Catch invalid
    # rates here so non-engine producers (HTTP /pcm bridges,
    # external voice stacks) that forward unvalidated request params
    # get a deterministic error instead of a raw stack trace.
    if not isinstance(source_rate, int) or source_rate <= 0:
        raise RuntimeError(
            f"send_pcm_audio: source_rate must be a positive integer, "
            f"got {source_rate!r}."
        )

    if gateway is None:
        raise RuntimeError(
            "send_pcm_audio requires a 'gateway' argument to push audio "
            "frames; this call appears to be a validation probe without one."
        )

    if not gateway.esp32.device_connected:
        raise RuntimeError(
            "No ESP32 device connected; cannot deliver audio."
        )

    # WebSocket protocol version gate. The firmware decodes raw Opus
    # binary frames only on protocol v1; v2/v3 wrap each binary message
    # in a BinaryProtocol header that this gateway does not yet emit.
    connection = getattr(gateway.esp32, "connection", None)
    proto_version = getattr(connection, "protocol_version", 1)
    if proto_version != 1:
        raise RuntimeError(
            f"send_pcm_audio requires WebSocket protocol v1, but the "
            f"connected device negotiated v{proto_version}. Rebuild the "
            "firmware with v1 (the default for this repository) — v2/v3 "
            "BinaryProtocol header wrapping is not yet supported."
        )

    # Resample to the device's rate before Opus encoding. ``encode_opus_frames``
    # expects samples at DEVICE_SAMPLE_RATE; passing a different rate would
    # produce frames that play back too fast / too slow on the device.
    if source_rate != DEVICE_SAMPLE_RATE:
        pcm = resample_pcm16_linear(pcm, source_rate, DEVICE_SAMPLE_RATE)

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
    # silently discarded.
    #
    # The whole start → frames → stop block runs under the device's
    # TTS lock so two concurrent pushes can't interleave their Opus
    # frames on the same WebSocket or overlap their state notifications.
    tts_lock = getattr(gateway.esp32, "tts_lock", None)
    lock_ctx = tts_lock if tts_lock is not None else nullcontext()

    sent = 0
    push_error: ConnectionError | None = None
    async with lock_ctx:
        try:
            await gateway.esp32.send_tts_state("start")
        except ConnectionError as exc:
            raise RuntimeError(
                f"Device disconnected before TTS start notification: {exc}"
            ) from exc

        # Wait for the firmware's state machine to land in
        # kDeviceStateSpeaking before sending the first frame.
        await asyncio.sleep(TTS_START_TRANSITION_DELAY_S)

        # Frame pacing: the device's decode queue holds at most ~40
        # frames (firmware MAX_DECODE_PACKETS_IN_QUEUE = 2400 /
        # OPUS_FRAME_DURATION_MS), and pushes that exceed it are
        # dropped silently. Send each frame at roughly the device's
        # consumption rate (one frame per frame_duration_ms) so a long
        # utterance never overflows. We let the loop drift by a single
        # interval if the network is slow — the wall clock is the
        # reference, not the loop iteration count.
        frame_period_s = DEVICE_FRAME_DURATION_MS / 1000.0
        loop = asyncio.get_event_loop()

        try:
            next_send_time = loop.time()
            for frame in opus_frames:
                now = loop.time()
                if now < next_send_time:
                    await asyncio.sleep(next_send_time - now)
                try:
                    await gateway.esp32.send_audio_frame(frame)
                except ConnectionError as exc:
                    # Stop pushing on the first disconnect, but fall
                    # through to the stop notification (see finally) so
                    # that *if* the device is somehow still listening
                    # it returns to idle rather than staying in speaking
                    # forever.
                    push_error = exc
                    break
                sent += 1
                next_send_time += frame_period_s
        finally:
            try:
                await gateway.esp32.send_tts_state("stop")
            except ConnectionError:
                # If the device dropped, it'll return to idle on its
                # own when the WebSocket close lands; nothing to do
                # here.
                pass

    if push_error is not None:
        raise RuntimeError(
            f"Device disconnected after sending "
            f"{sent}/{len(opus_frames)} frames: {push_error}"
        ) from push_error

    duration_ms = sent * DEVICE_FRAME_DURATION_MS

    logger.info(
        "send_pcm_audio: source=%s frames=%d duration_ms=%d",
        source_label,
        sent,
        duration_ms,
    )

    return {
        "source": source_label,
        "frame_count": sent,
        "sample_rate": DEVICE_SAMPLE_RATE,
        "frame_duration_ms": DEVICE_FRAME_DURATION_MS,
        "duration_ms": duration_ms,
    }
