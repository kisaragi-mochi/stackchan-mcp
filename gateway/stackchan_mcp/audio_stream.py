"""Opus audio frame handling for the gateway <-> device link.

Outbound (TTS) frames are produced by
:mod:`stackchan_mcp.tts.audio_utils` and pushed here to the connected
ESP32 via :meth:`stackchan_mcp.esp32_client.ESP32Manager.send_audio_frame`.

The inbound side (STT pipeline, Phase 4 / Issue #91) is now wired:
binary frames coming up from the device land in
:func:`handle_audio_frame`, which buffers them into a module-level
recording slot when one is active. The
:mod:`stackchan_mcp.stt.orchestrator` opens the slot via
:func:`start_recording` before sending ``listen.start`` to the device
and closes it via :func:`stop_recording` after the capture window;
outside an active recording, inbound frames are still discarded.

The recording slot is intentionally a module-level singleton: the
device's :class:`stackchan_mcp.esp32_client.ESP32Manager` only manages
one connection, and the STT orchestrator serialises ``listen()`` calls
through :attr:`ESP32Manager.listen_lock`, so concurrent captures
cannot race the buffer. If multi-device support lands later, this
should move onto the connection object.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .esp32_client import ESP32Manager

logger = logging.getLogger(__name__)


# --- Recording slot (inbound STT capture) ---------------------------------
#
# A single capture at a time is enforced by the orchestrator's
# ``listen_lock``; this module only owns the buffer itself.

_recording_session_id: str | None = None
_recording_frames: list[bytes] = []


def start_recording(session_id: str) -> None:
    """Open a fresh recording slot for ``session_id``.

    Any frames already buffered are discarded so a previous call that
    crashed before ``stop_recording`` cannot leak into the next
    capture. The orchestrator wraps start/stop in a try/finally to
    guarantee the slot is closed even on error.
    """
    global _recording_session_id, _recording_frames
    if _recording_session_id is not None:
        # Defensive: the lock should prevent this, but if it ever
        # fires we leak no audio — just log loudly so the regression
        # is visible.
        logger.warning(
            "start_recording called while session=%s was still active; "
            "dropping %d buffered frames",
            _recording_session_id,
            len(_recording_frames),
        )
    _recording_session_id = session_id
    _recording_frames = []


def stop_recording() -> list[bytes]:
    """Close the recording slot and return the buffered Opus frames.

    Returns an empty list if no recording was active. The slot is
    cleared whether or not frames were captured so the next call to
    :func:`start_recording` starts clean.
    """
    global _recording_session_id, _recording_frames
    frames = _recording_frames
    _recording_session_id = None
    _recording_frames = []
    return frames


def is_recording() -> bool:
    """Return ``True`` when a recording slot is currently open."""
    return _recording_session_id is not None


async def handle_audio_frame(data: bytes, session_id: str) -> None:
    """Process an incoming binary Opus frame from the device.

    When a recording slot is active (see :func:`start_recording`),
    appends the frame to the in-memory buffer for later decoding by
    the STT orchestrator. Outside of an active recording the frame is
    logged at debug level and discarded — the device may emit audio
    on its own (e.g. after an autonomous wake-word detection) and the
    gateway has no STT pipeline running for those frames yet.
    """
    if _recording_session_id is not None:
        _recording_frames.append(data)
        logger.debug(
            "audio_frame session=%s bytes=%d buffered (recording active)",
            session_id,
            len(data),
        )
        return
    logger.debug(
        "audio_frame session=%s bytes=%d (discarded — no active recording)",
        session_id,
        len(data),
    )


async def push_opus_frames(
    esp32: ESP32Manager,
    frames: Iterable[bytes],
) -> int:
    """Push Opus frames to the connected ESP32.

    Returns the number of frames sent so the caller can report this to
    the MCP client. Raises :class:`ConnectionError` (via
    :meth:`ESP32Manager.send_audio_frame`) if the device disconnects
    mid-stream — the orchestrator turns that into a clean MCP error
    rather than letting it bubble up as a stack trace.
    """
    sent = 0
    for frame in frames:
        await esp32.send_audio_frame(frame)
        sent += 1
    return sent
