"""Tests for the gateway audio_stream helpers (Issue #70 PR2)."""

from __future__ import annotations

import pytest

from stackchan_mcp.audio_stream import handle_audio_frame, push_opus_frames


class _FakeESP32:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.frames: list[bytes] = []
        self._fail_after = fail_after

    async def send_audio_frame(self, frame: bytes) -> None:
        if self._fail_after is not None and len(self.frames) >= self._fail_after:
            raise ConnectionError("simulated mid-stream disconnect")
        self.frames.append(frame)


@pytest.mark.asyncio
async def test_handle_audio_frame_is_a_silent_stub():
    """The inbound stub does not raise — STT is wired separately."""
    await handle_audio_frame(b"\x00\x01\x02", session_id="session-1")


@pytest.mark.asyncio
async def test_push_opus_frames_sends_each_frame_in_order():
    """All frames reach the device in the order they were given."""
    esp32 = _FakeESP32()
    frames = [b"a", b"b", b"c"]

    sent = await push_opus_frames(esp32, frames)

    assert sent == 3
    assert esp32.frames == frames


@pytest.mark.asyncio
async def test_push_opus_frames_propagates_disconnect():
    """A mid-stream ConnectionError surfaces, with the partial count visible."""
    esp32 = _FakeESP32(fail_after=2)
    frames = [b"a", b"b", b"c", b"d"]

    with pytest.raises(ConnectionError):
        await push_opus_frames(esp32, frames)

    # The first two frames did make it to the device before the failure.
    assert esp32.frames == [b"a", b"b"]


@pytest.mark.asyncio
async def test_push_opus_frames_empty_iterable_returns_zero():
    """Pushing zero frames is a no-op rather than an error."""
    esp32 = _FakeESP32()

    sent = await push_opus_frames(esp32, [])

    assert sent == 0
    assert esp32.frames == []
