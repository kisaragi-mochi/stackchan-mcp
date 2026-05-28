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
from collections.abc import AsyncIterator
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from .audio_utils import (
    DEVICE_CHANNELS,
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


async def send_pcm_stream(
    gateway: "Gateway",
    pcm_chunks: AsyncIterator[bytes],
    *,
    source_rate: int = DEVICE_SAMPLE_RATE,
    source_label: str = "stream",
) -> dict[str, Any]:
    """Encode and push PCM as it arrives from an async iterator.

    Where :func:`send_pcm_audio` buffers all PCM before encoding,
    ``send_pcm_stream`` accepts an :class:`~collections.abc.AsyncIterator`
    of PCM byte chunks and starts pushing Opus frames to the device as
    soon as enough samples have accumulated for one Opus frame. This
    keeps long utterances (multi-minute TTS, live audio mixes) playing
    on the device with low latency, without holding the entire PCM in
    memory.

    The Opus encoder instance is reused across chunks so the codec's
    internal state (predictors, gain) stays continuous — a fresh encoder
    per chunk would produce audible discontinuities at chunk
    boundaries.

    Args:
        gateway: The :class:`Gateway` instance whose
            :attr:`Gateway.esp32` the audio frames are pushed through.
        pcm_chunks: Async iterator yielding signed-16-bit LE mono PCM
            byte chunks. Chunk sizes need not be aligned to any boundary;
            the function buffers partial frames internally. Empty chunks
            are skipped without error so producers can use them as a
            "still alive" heartbeat. Iteration finishing (with no
            chunks left) flushes any trailing partial frame as
            zero-padded audio and ends the stream cleanly.
        source_rate: Sample rate of incoming PCM. Each chunk is
            resampled to :data:`DEVICE_SAMPLE_RATE` independently via
            linear interpolation; boundary discontinuities are
            negligible for speech-rate inputs.
        source_label: Label used in the orchestrator log so streaming
            producers can be traced separately (e.g.
            ``"voice-tts:msg_abc123"``).

    Returns:
        Dict describing the push: ``source``, ``frame_count``,
        ``sample_rate``, ``frame_duration_ms``, ``duration_ms``. Zero
        frames is a valid (logged-warning) outcome — e.g. the producer
        was cancelled before yielding any audio.

    Raises:
        RuntimeError: if ``gateway`` is missing, no device is connected,
            the negotiated protocol is not v1, opuslib is unavailable,
            Opus encoding fails, or the device disconnects mid-stream.
    """
    if gateway is None:
        raise RuntimeError(
            "send_pcm_stream requires a 'gateway' argument to push audio "
            "frames; this call appears to be a validation probe without one."
        )

    if not gateway.esp32.device_connected:
        raise RuntimeError(
            "No ESP32 device connected; cannot deliver streamed audio."
        )

    # WebSocket protocol version gate (same reasoning as send_pcm_audio).
    connection = getattr(gateway.esp32, "connection", None)
    proto_version = getattr(connection, "protocol_version", 1)
    if proto_version != 1:
        raise RuntimeError(
            f"send_pcm_stream requires WebSocket protocol v1, but the "
            f"connected device negotiated v{proto_version}. Rebuild the "
            "firmware with v1 (the default for this repository) — v2/v3 "
            "BinaryProtocol header wrapping is not yet supported."
        )

    # opuslib is the same optional extra used by ``encode_opus_frames``;
    # we hold the encoder instance across chunks here so importing
    # eagerly inside this function (rather than going via
    # ``encode_opus_frames``) gives the clearest install hint when the
    # extra is missing.
    try:
        import opuslib  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "opuslib is not installed. Install with "
            "'pip install stackchan-mcp[tts]' to enable streamed audio."
        ) from exc

    samples_per_frame = (
        DEVICE_SAMPLE_RATE * DEVICE_FRAME_DURATION_MS // 1000
    )
    bytes_per_frame = samples_per_frame * 2  # 16-bit
    # Number of source-rate samples that produce exactly one device-rate
    # opus frame after resampling. When the input is already at the device
    # rate this equals ``samples_per_frame`` and the resample step is a
    # no-op; otherwise we drain whole source-rate frames into the
    # resampler, which avoids the rounding-error and odd-byte issues that
    # per-chunk resampling has when transport chunk sizes are arbitrary.
    src_samples_per_frame = (
        source_rate * DEVICE_FRAME_DURATION_MS // 1000
    )
    if src_samples_per_frame <= 0:
        raise RuntimeError(
            f"source_rate {source_rate} is too low for "
            f"{DEVICE_FRAME_DURATION_MS} ms frames"
        )
    bytes_per_src_frame = src_samples_per_frame * 2  # 16-bit
    encoder = opuslib.Encoder(
        DEVICE_SAMPLE_RATE, DEVICE_CHANNELS, opuslib.APPLICATION_VOIP
    )

    tts_lock = getattr(gateway.esp32, "tts_lock", None)
    lock_ctx = tts_lock if tts_lock is not None else nullcontext()

    sent = 0
    push_error: ConnectionError | None = None
    # ``buffer`` accumulates source-rate PCM bytes. Chunks may be odd-byte
    # (HTTP chunked uploads can split a 16-bit sample across two transport
    # chunks), so we accumulate raw bytes here and only resample / encode
    # when the buffer holds at least one full source-rate frame's worth.
    buffer = bytearray()

    async def _push(opus_frame: bytes) -> bool:
        """Pace, send, advance counters. Returns False on disconnect."""
        nonlocal sent, push_error, next_send_time
        now = loop.time()
        if now < next_send_time:
            await asyncio.sleep(next_send_time - now)
        try:
            await gateway.esp32.send_audio_frame(opus_frame)
        except ConnectionError as exc:
            push_error = exc
            return False
        sent += 1
        # Advance the schedule from whichever is later: the previous
        # target (when we kept up — preserves the underlying 20 ms
        # cadence and absorbs sub-frame jitter), or the actual current
        # time (when the upstream producer paused — re-anchors pacing
        # so the next yielded chunk does not burst frames back-to-back
        # to "catch up" the stale schedule). Without this re-anchor a
        # producer pause longer than ``frame_period_s`` lets multiple
        # post-pause frames fire in one event-loop turn, exceeding the
        # firmware's ~40-packet decode queue and silently dropping
        # audio. The streaming use cases this helper is designed for
        # (HTTP chunked uploads, real-time TTS synthesis jitter)
        # routinely produce such pauses.
        next_send_time = max(next_send_time, loop.time()) + frame_period_s
        return True

    async with lock_ctx:
        try:
            await gateway.esp32.send_tts_state("start")
        except ConnectionError as exc:
            raise RuntimeError(
                f"Device disconnected before TTS start notification: {exc}"
            ) from exc

        await asyncio.sleep(TTS_START_TRANSITION_DELAY_S)

        frame_period_s = DEVICE_FRAME_DURATION_MS / 1000.0
        loop = asyncio.get_event_loop()
        next_send_time = loop.time()

        try:
            async for chunk in pcm_chunks:
                if not chunk:
                    # Empty chunk = heartbeat / cancellation tick; keep
                    # the loop alive without advancing the audio.
                    continue

                # Accumulate raw source-rate bytes. Resampling per chunk
                # used to live here but produced two bugs noted in PR
                # review: (a) ``resample_pcm16_linear`` raises ValueError
                # on odd-byte chunks because transport chunk boundaries
                # can split a 16-bit sample, and (b) rounding inside
                # ``resample_pcm16_linear`` (``n_dst = max(1, n_src *
                # dst_rate // src_rate)``) accumulates duration error
                # when called on small chunks repeatedly. Accumulating
                # to whole source-rate frames before resampling fixes
                # both.
                buffer.extend(chunk)

                # Drain as many full source-rate frames as the buffer
                # now holds. Each whole source frame resamples to
                # exactly ``samples_per_frame`` device samples, so the
                # rounding stays consistent across chunks regardless of
                # transport chunking.
                while len(buffer) >= bytes_per_src_frame:
                    src_frame = bytes(buffer[:bytes_per_src_frame])
                    del buffer[:bytes_per_src_frame]
                    if source_rate != DEVICE_SAMPLE_RATE:
                        pcm_frame = resample_pcm16_linear(
                            src_frame, source_rate, DEVICE_SAMPLE_RATE
                        )
                        # Resampler should produce exactly one device
                        # frame; pad / truncate defensively so the
                        # opus encoder gets the size it expects.
                        if len(pcm_frame) > bytes_per_frame:
                            pcm_frame = pcm_frame[:bytes_per_frame]
                        elif len(pcm_frame) < bytes_per_frame:
                            pcm_frame = pcm_frame + b"\x00" * (
                                bytes_per_frame - len(pcm_frame)
                            )
                    else:
                        pcm_frame = src_frame
                    try:
                        opus_frame = encoder.encode(
                            pcm_frame, samples_per_frame
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"Opus encoding failed: {exc}"
                        ) from exc

                    if not await _push(opus_frame):
                        break  # device disconnected mid-stream

                if push_error is not None:
                    break

            # Stream ended cleanly: flush any trailing partial frame as
            # zero-padded audio so the last few milliseconds of speech
            # aren't silently dropped. We zero-pad in the source rate
            # space first (down to 16-bit sample alignment, then up to
            # one source-rate frame), then resample once to a device
            # frame, mirroring the per-frame logic above.
            if push_error is None and len(buffer) > 0:
                tail_src = bytes(buffer)
                if len(tail_src) % 2 != 0:
                    # Drop a stray byte rather than crash. The producer
                    # protocol expects 16-bit aligned PCM; a half sample
                    # at EOS has no defined interpretation.
                    tail_src = tail_src[:-1]
                if len(tail_src) > 0:
                    if len(tail_src) < bytes_per_src_frame:
                        tail_src = tail_src + b"\x00" * (
                            bytes_per_src_frame - len(tail_src)
                        )
                    if source_rate != DEVICE_SAMPLE_RATE:
                        tail = resample_pcm16_linear(
                            tail_src, source_rate, DEVICE_SAMPLE_RATE
                        )
                        if len(tail) > bytes_per_frame:
                            tail = tail[:bytes_per_frame]
                        elif len(tail) < bytes_per_frame:
                            tail = tail + b"\x00" * (
                                bytes_per_frame - len(tail)
                            )
                    else:
                        tail = tail_src
                    try:
                        opus_frame = encoder.encode(
                            tail, samples_per_frame
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"Opus encoding failed: {exc}"
                        ) from exc
                    await _push(opus_frame)
        finally:
            try:
                await gateway.esp32.send_tts_state("stop")
            except ConnectionError:
                pass

    if push_error is not None:
        raise RuntimeError(
            f"Device disconnected after sending {sent} frames: {push_error}"
        ) from push_error

    duration_ms = sent * DEVICE_FRAME_DURATION_MS

    if sent == 0:
        logger.warning(
            "send_pcm_stream: source=%s yielded no audio (producer "
            "cancelled or empty stream)",
            source_label,
        )
    else:
        logger.info(
            "send_pcm_stream: source=%s frames=%d duration_ms=%d",
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
