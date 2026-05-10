"""Tests for the TTS orchestrator pipeline (Issue #70 PR2)."""

from __future__ import annotations

from typing import Any

import pytest

from stackchan_mcp.tts import EngineRegistry, TTSEngine, synthesize_and_send
from stackchan_mcp.tts.audio_utils import (
    DEVICE_FRAME_DURATION_MS,
    DEVICE_SAMPLE_RATE,
)


class _PCMEngine(TTSEngine):
    """Engine that returns a fixed PCM buffer and records the call."""

    def __init__(self, pcm: bytes, name: str = "voicevox") -> None:
        self.name = name
        self._pcm = pcm
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def synthesize(self, text: str, **opts: Any) -> bytes:
        self.calls.append((text, dict(opts)))
        return self._pcm


class _FakeESP32:
    def __init__(self, *, connected: bool = True) -> None:
        self.device_connected = connected
        self.frames: list[bytes] = []

    async def send_audio_frame(self, frame: bytes) -> None:
        self.frames.append(frame)


class _FakeGateway:
    def __init__(self, esp32: _FakeESP32) -> None:
        self.esp32 = esp32


@pytest.fixture
def fake_encode(monkeypatch):
    """Replace encode_opus_frames so tests don't need libopus.

    Each chunk of ``DEVICE_SAMPLE_RATE * DEVICE_FRAME_DURATION_MS / 1000``
    samples becomes one fake Opus frame; the last partial chunk is
    counted as a full frame too (matches the real encoder + chunker).
    """

    def fake(pcm: bytes, **kwargs):
        samples_per_frame = (
            DEVICE_SAMPLE_RATE * DEVICE_FRAME_DURATION_MS // 1000
        )
        bytes_per_frame = samples_per_frame * 2
        n_full = len(pcm) // bytes_per_frame
        n_partial = 1 if len(pcm) % bytes_per_frame else 0
        n_total = n_full + n_partial
        return iter(
            f"opus_frame_{i}".encode() for i in range(n_total)
        )

    import stackchan_mcp.tts.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "encode_opus_frames", fake)
    return fake


@pytest.mark.asyncio
async def test_pipeline_synthesises_encodes_and_pushes(fake_encode):
    """A full happy-path call synthesises, encodes, and pushes to the device."""
    # 90 ms of PCM @ 16 kHz mono = 1440 samples = 2880 bytes
    pcm = b"\x01\x00" * 1440
    engine = _PCMEngine(pcm)
    esp32 = _FakeESP32(connected=True)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    result = await synthesize_and_send(
        {"text": "こんにちは", "voice": "voicevox", "speaker_id": 7},
        gateway=gateway,
        registry=reg,
    )

    # 1440 / 960 = 1.5 -> 2 frames (the second is zero-padded internally)
    assert result["frame_count"] == 2
    assert result["sample_rate"] == DEVICE_SAMPLE_RATE
    assert result["frame_duration_ms"] == DEVICE_FRAME_DURATION_MS
    assert result["duration_ms"] == 2 * DEVICE_FRAME_DURATION_MS
    assert result["engine"] == "voicevox"
    assert result["text"] == "こんにちは"
    assert result["speaker_id"] == 7

    assert esp32.frames == [b"opus_frame_0", b"opus_frame_1"]
    assert engine.calls[0][0] == "こんにちは"
    assert engine.calls[0][1]["speaker_id"] == 7


@pytest.mark.asyncio
async def test_pipeline_passes_reference_audio_through(fake_encode):
    """reference_audio is forwarded to engines that support voice cloning."""
    engine = _PCMEngine(b"\x00\x00" * 960)
    esp32 = _FakeESP32(connected=True)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    await synthesize_and_send(
        {
            "text": "hello",
            "voice": "voicevox",
            "reference_audio": "/tmp/sample.wav",
        },
        gateway=gateway,
        registry=reg,
    )

    assert engine.calls[0][1]["reference_audio"] == "/tmp/sample.wav"


@pytest.mark.asyncio
async def test_pipeline_raises_when_device_disconnected(fake_encode):
    """Disconnected device fails fast before invoking the engine."""
    engine = _PCMEngine(b"\x00\x00" * 960)
    esp32 = _FakeESP32(connected=False)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError, match="ESP32"):
        await synthesize_and_send(
            {"text": "hello"},
            gateway=gateway,
            registry=reg,
        )

    # Engine never gets called when there's no device to send to.
    assert engine.calls == []


@pytest.mark.asyncio
async def test_pipeline_raises_when_engine_returns_no_pcm(fake_encode):
    """An engine returning empty PCM is a bug, surfaced as a RuntimeError."""
    engine = _PCMEngine(b"")
    esp32 = _FakeESP32(connected=True)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError, match="no PCM"):
        await synthesize_and_send(
            {"text": "hello"},
            gateway=gateway,
            registry=reg,
        )

    # Nothing pushed to the device when synthesis produced nothing.
    assert esp32.frames == []
