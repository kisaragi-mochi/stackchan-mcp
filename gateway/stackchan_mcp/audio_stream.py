"""Opus audio frame handling for the gateway <-> device link.

Outbound (TTS) frames are produced by
:mod:`stackchan_mcp.tts.audio_utils` and pushed here to the connected
ESP32 via :meth:`stackchan_mcp.esp32_client.ESP32Manager.send_audio_frame`.

The inbound side (STT pipeline, Phase 4 / Issue #8) is still a stub —
binary frames coming up from the device are logged and discarded for
now. Wiring that up belongs to the STT half of Phase 4.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .esp32_client import ESP32Manager

logger = logging.getLogger(__name__)


async def handle_audio_frame(data: bytes, session_id: str) -> None:
    """Process an incoming binary Opus frame from the device (stub).

    The STT half of Phase 4 will pipe this into a recogniser; until
    then we just log the size at debug level.
    """
    logger.debug(
        "audio_frame session=%s bytes=%d (discarded — STT not wired up)",
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
