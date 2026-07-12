from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
import wave

import pytest
import pytest_asyncio

from stackchan_mcp.audio_stream import (
    handle_audio_frame,
    is_recording,
    recording_owner,
    stop_recording,
)
from stackchan_mcp.beat import mode as beat_mode
from stackchan_mcp.beat.mode import BeatModeConfig
from stackchan_mcp.stt import EngineRegistry, STTEngine, listen_and_transcribe
from stackchan_mcp.stt.audio_utils import DEVICE_SAMPLE_RATE


class _FakeESP32:
    def __init__(self) -> None:
        self.device_connected = True
        self.connection = SimpleNamespace(
            protocol_version=1,
            session_id="beat-session",
        )
        self.listen_lock = asyncio.Lock()
        self.listen_states: list[tuple[str, str | None]] = []
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        self.listen_states.append((state, mode if state == "start" else None))

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], None]:
        self.tool_calls.append((name, dict(arguments)))
        return {"ok": True}, None


class _FakeGateway:
    def __init__(self) -> None:
        self.esp32 = _FakeESP32()


class _NoopEngine(STTEngine):
    name = "faster-whisper"

    async def transcribe(self, pcm: bytes, **opts: Any) -> dict[str, Any]:
        return {"text": "", "language": opts.get("language") or "ja"}


@pytest.fixture
def fake_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    def decode(frames: list[bytes], **kwargs: Any) -> bytes:
        return b"".join(frames)

    monkeypatch.setattr(beat_mode, "decode_opus_frames", decode)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_beat_mode() -> Any:
    await beat_mode.stop_beat_mode()
    if is_recording():
        stop_recording()
    beat_mode._mode = None
    yield
    await beat_mode.stop_beat_mode()
    if is_recording():
        stop_recording()
    beat_mode._mode = None


@pytest.mark.asyncio
async def test_start_stop_are_idempotent_and_release_recording(fake_decode) -> None:
    gateway = _FakeGateway()

    started = await beat_mode.start_beat_mode(
        gateway,
        BeatModeConfig(motion_intensity=0.5),
    )
    started_again = await beat_mode.start_beat_mode(
        gateway,
        BeatModeConfig(motion_intensity=0.8),
    )

    assert started["active"] is True
    assert started_again["active"] is True
    assert started_again["motion"]["intensity"] == 0.8
    assert is_recording()
    assert recording_owner() == beat_mode.BEAT_MODE_OWNER
    assert gateway.esp32.listen_states == [("start", "manual")]

    stopped = await beat_mode.stop_beat_mode()
    stopped_again = await beat_mode.stop_beat_mode()

    assert stopped["active"] is False
    assert stopped_again["active"] is False
    assert not is_recording()
    assert gateway.esp32.listen_states == [
        ("start", "manual"),
        ("stop", None),
    ]


@pytest.mark.asyncio
async def test_listen_fails_fast_while_beat_mode_owns_mic(fake_decode) -> None:
    gateway = _FakeGateway()
    await beat_mode.start_beat_mode(gateway, BeatModeConfig())

    reg = EngineRegistry()
    reg.register(_NoopEngine())

    with pytest.raises(RuntimeError, match="beat mode"):
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=gateway,
            registry=reg,
        )

    assert gateway.esp32.listen_states == [("start", "manual")]


@pytest.mark.asyncio
async def test_beat_mode_does_not_hold_audio_lock_during_capture(fake_decode) -> None:
    gateway = _FakeGateway()
    await beat_mode.start_beat_mode(gateway, BeatModeConfig())

    await asyncio.wait_for(gateway.esp32.listen_lock.acquire(), timeout=0.1)
    gateway.esp32.listen_lock.release()


@pytest.mark.asyncio
async def test_clip_save_writes_recent_pcm_as_wav(fake_decode) -> None:
    gateway = _FakeGateway()
    await beat_mode.start_beat_mode(gateway, BeatModeConfig())

    pcm = b"\x01\x00" * int(0.25 * DEVICE_SAMPLE_RATE)
    await handle_audio_frame(pcm, session_id="beat-session")
    for _ in range(20):
        if beat_mode.get_beat_mode_snapshot()["frames_decoded"] >= 1:
            break
        await asyncio.sleep(0)

    result = await beat_mode.save_beat_clip(1.0)

    with wave.open(result["path"], "rb") as wav:
        assert wav.getframerate() == DEVICE_SAMPLE_RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == int(0.25 * DEVICE_SAMPLE_RATE)

    assert result["seconds"] == 0.25
    assert result["active"] is True


@pytest.mark.asyncio
async def test_update_applies_runtime_parameters(fake_decode) -> None:
    gateway = _FakeGateway()
    await beat_mode.start_beat_mode(gateway, BeatModeConfig())

    status = await beat_mode.update_beat_mode(
        motion_intensity=0.2,
        color=(255, 64, 0),
        blink_rate=2.0,
        motion_enabled=False,
        led_enabled=False,
    )

    assert status["motion"]["intensity"] == 0.2
    assert status["motion"]["enabled"] is False
    assert status["led"]["color"] == [255, 64, 0]
    assert status["led"]["blink_rate"] == 2.0
    assert status["led"]["enabled"] is False


def test_config_validation_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="motion_intensity"):
        BeatModeConfig(motion_intensity=1.5)
    with pytest.raises(ValueError, match="color channels"):
        BeatModeConfig(color=(256, 0, 0))
    with pytest.raises(ValueError, match="blink_rate"):
        BeatModeConfig(blink_rate=0.1)
