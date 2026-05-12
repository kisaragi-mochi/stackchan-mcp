"""STT orchestration: drive listening, collect frames, decode, transcribe.

The orchestrator is the glue between the ``listen`` MCP tool (defined
in :mod:`stackchan_mcp.stdio_server`) and the STT engine implementations
registered in :mod:`stackchan_mcp.stt`. For each call it:

1. Validates arguments.
2. Looks up the requested engine.
3. Acquires the device's listen lock so two concurrent ``listen()``
   invocations cannot overlap (they would otherwise both buffer
   inbound Opus frames into the same capture and produce a mixed
   transcription).
4. Switches the audio_stream module into recording mode so binary
   frames stop being discarded and start being buffered.
5. Sends ``{"type":"listen","state":"start","mode":"manual"}`` to put
   the device firmware into listening state and stream microphone
   Opus frames up the existing WebSocket.
6. Waits ``duration_ms`` (the capture window).
7. Sends ``{"type":"listen","state":"stop"}`` to drop the device back
   to idle and stop the inbound frame stream.
8. Decodes the buffered Opus frames into 16 kHz mono PCM and hands
   the blob off to the engine for transcription.
9. Returns ``{ text, duration_ms, language, frame_count }`` to the
   MCP client.

Symmetric to :mod:`stackchan_mcp.tts.orchestrator` — same error-class
discipline (``ValueError`` for bad arguments, ``NotImplementedError``
for missing engine, ``RuntimeError`` for runtime failures) and the
same protocol-v1 gate.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from ..audio_stream import start_recording, stop_recording
from .audio_utils import DEVICE_FRAME_DURATION_MS, DEVICE_SAMPLE_RATE, decode_opus_frames
from .base import EngineRegistry, get_registry

if TYPE_CHECKING:
    from ..gateway import Gateway

logger = logging.getLogger(__name__)


#: Default engine name when ``engine`` is omitted from the tool call.
#: faster-whisper runs locally and matches the "works offline out of
#: the box" stance (Issue #91).
DEFAULT_ENGINE = "faster-whisper"

#: Minimum capture window. Below this Whisper has too little signal to
#: produce anything useful, and the listen() round-trip starts to be
#: dominated by setup overhead.
MIN_DURATION_MS = 100

#: Maximum capture window. 30 seconds is enough for any single-shot
#: utterance and caps Python memory at ~960 KB of PCM (16000 * 2 *
#: 30) which is safe even on a Raspberry Pi gateway.
MAX_DURATION_MS = 30000

#: Small grace period after sending ``listen.start`` before we start
#: counting the capture window, mirrored from the TTS orchestrator's
#: ``TTS_START_TRANSITION_DELAY_S``. The firmware dispatches the state
#: transition through ``Schedule()`` (queued onto the main task) so
#: the first inbound frame can race the ``kDeviceStateListening``
#: transition; 50 ms is well above typical scheduling latency.
LISTEN_START_TRANSITION_DELAY_S = 0.05


async def listen_and_transcribe(
    arguments: dict[str, Any],
    *,
    gateway: "Gateway | None" = None,
    registry: EngineRegistry | None = None,
) -> dict[str, Any]:
    """Capture a short utterance from the device and transcribe it.

    Args:
        arguments: MCP tool arguments. Recognised keys:

            * ``duration_ms`` (optional, default 5000): capture window
              in milliseconds, clamped to
              [:data:`MIN_DURATION_MS`, :data:`MAX_DURATION_MS`].
            * ``engine``: engine name; defaults to
              :data:`DEFAULT_ENGINE`.
            * ``language``: ISO 639-1 code (e.g. ``"ja"``) or ``None``
              for autodetect.
            * ``model``: engine-specific model identifier (e.g.
              ``"base"`` / ``"small"`` for faster-whisper).

        gateway: The :class:`Gateway` instance whose ESP32 manager
            this call drives. Required for the pipeline; left optional
            in the signature so callers can inspect validation errors
            without setting up a gateway (e.g. argument-validation
            tests).

        registry: Engine registry to look up ``engine`` in. Defaults
            to the process-wide registry. Tests inject a fresh
            registry to avoid leaking state across cases.

    Returns:
        Dict describing the transcription: ``engine``, ``text``,
        ``language``, ``duration_ms``, ``frame_count``.

    Raises:
        ValueError: bad arguments.
        NotImplementedError: requested engine not registered.
        RuntimeError: no gateway / no device / wrong protocol /
            device disconnected mid-capture.
    """
    duration_raw = arguments.get("duration_ms", 5000)
    if isinstance(duration_raw, bool) or not isinstance(duration_raw, int):
        raise ValueError(
            "'duration_ms' must be an integer; got " + repr(duration_raw)
        )
    if duration_raw < MIN_DURATION_MS or duration_raw > MAX_DURATION_MS:
        raise ValueError(
            f"'duration_ms' must be between {MIN_DURATION_MS} and "
            f"{MAX_DURATION_MS}; got {duration_raw}"
        )

    engine_raw = arguments.get("engine", DEFAULT_ENGINE)
    engine_name = (
        engine_raw if isinstance(engine_raw, str) and engine_raw else DEFAULT_ENGINE
    )

    reg = registry if registry is not None else get_registry()
    engine = reg.get(engine_name)
    if engine is None:
        available = reg.names()
        raise NotImplementedError(
            f"STT engine '{engine_name}' is not registered. "
            f"Available engines: {available or '(none)'}. "
            "Install the relevant extra (e.g. "
            "'pip install stackchan-mcp[stt-faster-whisper]' for the "
            "default local engine, or 'pip install "
            "stackchan-mcp[stt-openai]' for the OpenAI Whisper API)."
        )

    if gateway is None:
        raise RuntimeError(
            "listen_and_transcribe requires a 'gateway' argument to "
            "drive the device's listening state; this call appears to "
            "be a validation probe without one."
        )

    if not gateway.esp32.device_connected:
        raise RuntimeError(
            "No ESP32 device connected; cannot capture audio for STT."
        )

    # Protocol version gate, identical in spirit to the TTS side
    # (PR #75). The gateway's inbound binary handler decodes raw Opus
    # only on protocol v1; v2/v3 wrap the binary message in a
    # BinaryProtocol header that this gateway does not yet parse on
    # the inbound side either, so the buffered frames would be
    # unusable.
    connection = getattr(gateway.esp32, "connection", None)
    proto_version = getattr(connection, "protocol_version", 1)
    if proto_version != 1:
        raise RuntimeError(
            f"listen() requires WebSocket protocol v1, but the connected "
            f"device negotiated v{proto_version}. Rebuild the firmware "
            "with v1 (the default for this repository) — v2/v3 "
            "BinaryProtocol header wrapping is not yet supported on the "
            "STT path."
        )

    # Acquire the device's listen lock so two concurrent listen() calls
    # cannot interleave their capture windows. Same getattr fallback
    # pattern as the TTS orchestrator's ``tts_lock`` so test fakes that
    # don't expose the attribute keep working.
    listen_lock = getattr(gateway.esp32, "listen_lock", None)
    lock_ctx = listen_lock if listen_lock is not None else nullcontext()

    duration_ms = int(duration_raw)
    language = arguments.get("language", "ja")
    model = arguments.get("model")

    frame_count = 0
    pcm = b""
    actual_duration_ms = 0

    async with lock_ctx:
        connection = gateway.esp32.connection
        session_id = getattr(connection, "session_id", "") if connection else ""

        # Switch the audio_stream module into recording mode BEFORE
        # sending listen.start so we don't drop the first frame the
        # device emits the moment it lands in kDeviceStateListening.
        start_recording(session_id)
        listen_start_sent = False
        try:
            try:
                await gateway.esp32.send_listen_state("start", mode="manual")
                listen_start_sent = True
            except ConnectionError as exc:
                raise RuntimeError(
                    f"Device disconnected before listen.start: {exc}"
                ) from exc

            # Wait for the firmware's state machine to land in
            # kDeviceStateListening before we start counting the
            # capture window (same rationale as the TTS pipeline's
            # ``TTS_START_TRANSITION_DELAY_S``).
            await asyncio.sleep(LISTEN_START_TRANSITION_DELAY_S)

            await asyncio.sleep(duration_ms / 1000.0)
        finally:
            # Cancellation-safe listen.stop. If the request is
            # cancelled mid-capture (or any exception unwinds here)
            # after listen.start has been delivered, the device is
            # still in ``kDeviceStateListening`` with the microphone
            # open — without a best-effort stop the firmware would
            # stay there until an unrelated user action (button /
            # wake-word) eventually pulled it back to idle.
            # ``asyncio.shield`` protects the stop send from the
            # cancellation that's already propagating through the
            # outer await, so the device receives the stop even
            # though the orchestrator coroutine itself is being torn
            # down. The shielded send still completes synchronously
            # before this ``finally`` block returns.
            if listen_start_sent:
                try:
                    await asyncio.shield(
                        gateway.esp32.send_listen_state("stop")
                    )
                except (ConnectionError, asyncio.CancelledError):
                    # Device dropped, or our awaiter was cancelled
                    # after shield released the send back to us. In
                    # both cases the partial buffer is still worth
                    # transcribing, but the operator should know the
                    # firmware may need a manual nudge.
                    logger.warning(
                        "listen.stop did not reach device cleanly "
                        "(cancellation or disconnect); firmware may "
                        "stay in listening mode until a button press "
                        "or wake-word"
                    )
                except Exception as exc:
                    logger.warning(
                        "best-effort listen.stop failed: %s", exc
                    )
            frames = stop_recording()

        frame_count = len(frames)
        if frame_count == 0:
            # Distinguish "device disconnected with no frames" (likely
            # protocol mismatch / firmware not yet supporting listen)
            # from "spoken nothing". The latter is a legitimate empty
            # transcription and not surfaced as an error.
            logger.info(
                "listen(): no Opus frames received during %d ms window",
                duration_ms,
            )

        try:
            pcm = decode_opus_frames(frames)
        except Exception as exc:
            raise RuntimeError(f"Opus decode failed: {exc}") from exc

        actual_duration_ms = frame_count * DEVICE_FRAME_DURATION_MS

        if pcm:
            try:
                result = await engine.transcribe(
                    pcm,
                    language=language,
                    model=model,
                )
            except ValueError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"STT engine '{engine_name}' failed: {exc}"
                ) from exc
        else:
            # Empty capture — return an empty transcription rather
            # than failing the call. ``language`` falls back to the
            # caller's hint (or empty string).
            result = {
                "text": "",
                "language": (
                    language if isinstance(language, str) and language else ""
                ),
            }

    logger.info(
        "listen(): engine=%s frames=%d duration_ms=%d text=%r",
        engine_name,
        frame_count,
        actual_duration_ms,
        (result.get("text", "") or "")[:80],
    )

    return {
        "engine": engine_name,
        "text": result.get("text", ""),
        "language": result.get("language", ""),
        "duration_ms": actual_duration_ms,
        "frame_count": frame_count,
        "sample_rate": DEVICE_SAMPLE_RATE,
    }
