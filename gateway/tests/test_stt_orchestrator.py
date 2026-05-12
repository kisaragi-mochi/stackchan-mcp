"""Tests for the STT orchestrator pipeline (Issue #91).

Symmetric to :mod:`tests.test_orchestrator` (the TTS counterpart).
Focuses on the pipeline shape — argument validation, listen-state
notifications, protocol-v1 gate, listen_lock serialisation, empty
captures, and clean error translation — without depending on the
heavy ML engines or libopus.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import stackchan_mcp.stt.orchestrator as orchestrator
from stackchan_mcp.audio_stream import is_recording, stop_recording
from stackchan_mcp.stt import EngineRegistry, STTEngine, listen_and_transcribe
from stackchan_mcp.stt.audio_utils import DEVICE_FRAME_DURATION_MS, DEVICE_SAMPLE_RATE


class _CapturingEngine(STTEngine):
    """Engine that returns fixed text and records what it received."""

    def __init__(self, text: str = "こんにちは", name: str = "faster-whisper") -> None:
        self.name = name
        self._text = text
        self.calls: list[tuple[bytes, dict[str, Any]]] = []

    async def transcribe(self, pcm: bytes, **opts: Any) -> dict[str, Any]:
        self.calls.append((pcm, dict(opts)))
        return {"text": self._text, "language": opts.get("language") or "ja"}


class _RaisingEngine(STTEngine):
    """Engine that always raises a configured exception."""

    def __init__(self, exc: Exception, name: str = "faster-whisper") -> None:
        self.name = name
        self._exc = exc

    async def transcribe(self, pcm: bytes, **opts: Any) -> dict[str, Any]:
        raise self._exc


class _FakeESP32:
    def __init__(
        self,
        *,
        connected: bool = True,
        protocol_version: int = 1,
        frames_to_inject: list[bytes] | None = None,
        injection_delay_s: float = 0.0,
    ) -> None:
        self.device_connected = connected
        self.connection = SimpleNamespace(
            protocol_version=protocol_version,
            session_id="session-test",
        )
        self.listen_states: list[tuple[str, str | None]] = []
        self.events: list[tuple[str, Any]] = []
        self.listen_lock = asyncio.Lock()
        self._frames_to_inject = list(frames_to_inject or [])
        self._injection_delay_s = injection_delay_s

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        self.listen_states.append((state, mode if state == "start" else None))
        self.events.append(("listen_state", state))
        if state == "start" and self._frames_to_inject:
            # Schedule frame injection while the orchestrator is in the
            # capture window; we deliberately use create_task so the
            # injection runs concurrently with the orchestrator's
            # asyncio.sleep(duration_ms).
            asyncio.create_task(self._inject_frames())

    async def _inject_frames(self) -> None:
        # Delay slightly so the orchestrator has had time to mark
        # recording active. The transition-delay sleep in the
        # orchestrator (50 ms) is plenty in practice; we yield once
        # here to keep tests deterministic regardless of scheduling.
        await asyncio.sleep(self._injection_delay_s)
        from stackchan_mcp.audio_stream import handle_audio_frame

        for frame in self._frames_to_inject:
            await handle_audio_frame(frame, "session-test")


class _FakeGateway:
    def __init__(self, esp32: _FakeESP32) -> None:
        self.esp32 = esp32


@pytest.fixture
def fake_decode(monkeypatch):
    """Replace decode_opus_frames so tests don't need libopus.

    Concatenates frame payloads as-is; for the orchestrator's purposes
    the exact PCM contents don't matter beyond "non-empty when frames
    arrived, empty when none did".
    """

    def fake(frames, **kwargs):
        return b"".join(frames)

    monkeypatch.setattr(orchestrator, "decode_opus_frames", fake)
    return fake


@pytest.fixture(autouse=True)
def _cleanup_recording_slot():
    """Always release the module-level recording slot between tests.

    The orchestrator opens/closes the slot itself, but a failed
    test that bypasses ``finally`` would leak state into the next
    test; this fixture defends against that.
    """
    yield
    if is_recording():
        stop_recording()


@pytest.mark.asyncio
async def test_pipeline_drives_listen_state_and_returns_text(fake_decode, monkeypatch):
    """Happy path: start/stop notifications fire, frames decode, engine runs."""
    # Compress the duration sleep so the test is fast without losing
    # the orchestrator's actual behaviour.
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast_sleep)

    engine = _CapturingEngine(text="やっほー")
    frames = [b"opus_frame_0", b"opus_frame_1", b"opus_frame_2"]
    esp32 = _FakeESP32(frames_to_inject=frames)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    result = await listen_and_transcribe(
        {"duration_ms": 500, "engine": "faster-whisper", "language": "ja"},
        gateway=gateway,
        registry=reg,
    )

    assert [s[0] for s in esp32.listen_states] == ["start", "stop"]
    # start was sent with mode="manual"; stop carries no mode.
    assert esp32.listen_states[0] == ("start", "manual")
    assert esp32.listen_states[1] == ("stop", None)

    assert result["engine"] == "faster-whisper"
    assert result["text"] == "やっほー"
    assert result["language"] == "ja"
    assert result["frame_count"] == 3
    assert result["duration_ms"] == 3 * DEVICE_FRAME_DURATION_MS
    assert result["sample_rate"] == DEVICE_SAMPLE_RATE

    # Engine saw the concatenated PCM (our fake decode just glued the
    # frame payloads together).
    assert len(engine.calls) == 1
    pcm_arg, opts = engine.calls[0]
    assert pcm_arg == b"".join(frames)
    assert opts["language"] == "ja"

    # Recording slot was closed cleanly.
    assert not is_recording()


@pytest.mark.asyncio
async def test_pipeline_returns_empty_text_on_no_frames(fake_decode, monkeypatch):
    """An empty capture (no frames) returns text='' rather than erroring.

    Useful when a user goes silent for the full window: faster-whisper
    on an empty buffer would otherwise spend cycles producing noise,
    and treating "no frames" as a failure would surface as a confusing
    MCP error.
    """
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast_sleep)

    engine = _CapturingEngine()
    esp32 = _FakeESP32(frames_to_inject=[])
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    result = await listen_and_transcribe(
        {"duration_ms": 200},
        gateway=gateway,
        registry=reg,
    )

    assert result["frame_count"] == 0
    assert result["duration_ms"] == 0
    assert result["text"] == ""
    # Engine is NOT invoked when the buffer is empty — wasted work.
    assert engine.calls == []


@pytest.mark.asyncio
async def test_pipeline_blocks_protocol_v2(fake_decode):
    """Devices that negotiated WebSocket protocol v2 are blocked."""
    engine = _CapturingEngine()
    esp32 = _FakeESP32(protocol_version=2)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError, match=r"v1"):
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=gateway,
            registry=reg,
        )

    # No notifications, no engine call, slot stays clean.
    assert esp32.listen_states == []
    assert engine.calls == []
    assert not is_recording()


@pytest.mark.asyncio
async def test_pipeline_raises_when_device_disconnected():
    """Disconnected device fails fast without invoking the engine."""
    engine = _CapturingEngine()
    esp32 = _FakeESP32(connected=False)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError, match="ESP32"):
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=gateway,
            registry=reg,
        )

    assert engine.calls == []
    assert not is_recording()


@pytest.mark.asyncio
async def test_pipeline_translates_disconnect_before_listen_start(fake_decode, monkeypatch):
    """ConnectionError on listen.start surfaces as a clear RuntimeError."""

    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast_sleep)

    class FailingESP32(_FakeESP32):
        async def send_listen_state(self, state: str, mode: str = "manual") -> None:
            self.listen_states.append((state, mode if state == "start" else None))
            if state == "start":
                raise ConnectionError("device dropped during listen.start")

    engine = _CapturingEngine()
    esp32 = FailingESP32()
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError, match="listen.start"):
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=gateway,
            registry=reg,
        )

    # Recording slot must be closed even when start fails.
    assert not is_recording()
    assert engine.calls == []


@pytest.mark.asyncio
async def test_pipeline_translates_engine_error_to_runtime_error(fake_decode, monkeypatch):
    """Engine failure surfaces as RuntimeError with the cause preserved."""

    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast_sleep)

    cause = TimeoutError("model load timed out")
    engine = _RaisingEngine(cause)
    esp32 = _FakeESP32(frames_to_inject=[b"opus_a"])
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError) as exc_info:
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=gateway,
            registry=reg,
        )

    assert "faster-whisper" in str(exc_info.value).lower()
    assert exc_info.value.__cause__ is cause
    # listen.stop was attempted even though transcribe failed (frames
    # arrived, slot needs to drain on the device side).
    assert ("stop", None) in esp32.listen_states
    assert not is_recording()


@pytest.mark.asyncio
async def test_pipeline_value_error_propagates_as_value_error(fake_decode, monkeypatch):
    """ValueError from the engine stays a ValueError."""

    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast_sleep)

    engine = _RaisingEngine(ValueError("bad language hint"))
    esp32 = _FakeESP32(frames_to_inject=[b"opus_a"])
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(ValueError, match="language"):
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=gateway,
            registry=reg,
        )


@pytest.mark.asyncio
async def test_pipeline_sends_listen_stop_on_cancellation(fake_decode):
    """A cancelled listen() call still tells the device to stop.

    Without ``asyncio.shield`` around the listen.stop send, the
    cancellation would propagate before the stop reached the wire and
    the firmware would stay in ``kDeviceStateListening`` with the
    microphone open until an unrelated button press / wake-word
    eventually pulled it back to idle. The shielded stop guarantees
    the device receives the cleanup notification even when the
    orchestrator coroutine itself is being torn down.
    """
    engine = _CapturingEngine()
    esp32 = _FakeESP32()  # no frame injection; the sleep will be cancelled
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    task = asyncio.create_task(
        listen_and_transcribe(
            {"duration_ms": 30000},  # long window; we will cancel mid-flight
            gateway=gateway,
            registry=reg,
        )
    )
    # Yield once so the task starts, lands in listen.start, then
    # enters the capture sleep.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Despite the cancellation, the orchestrator must have delivered
    # both listen.start and listen.stop to the device so the firmware
    # leaves listening mode cleanly.
    state_seq = [s for s, _ in esp32.listen_states]
    assert "start" in state_seq
    assert "stop" in state_seq
    # The recording slot must also be released — leaving it open
    # would corrupt the next listen() call's buffer.
    assert not is_recording()
    # Engine is not invoked because the cancellation prevents the
    # post-capture transcribe step.
    assert engine.calls == []


@pytest.mark.asyncio
async def test_pipeline_serialises_concurrent_listen_calls(fake_decode, monkeypatch):
    """Concurrent listen() calls don't share the recording slot.

    Without the listen_lock, both calls would race ``start_recording`` /
    ``stop_recording`` against the single module-level slot, producing
    a mixed transcription. The lock guarantees a strictly sequential
    pattern: start_0 < stop_0 < start_1 < stop_1.
    """
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast_sleep)

    engine = _CapturingEngine()
    esp32 = _FakeESP32(frames_to_inject=[b"opus_a"])
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    await asyncio.gather(
        listen_and_transcribe(
            {"duration_ms": 200}, gateway=gateway, registry=reg
        ),
        listen_and_transcribe(
            {"duration_ms": 200}, gateway=gateway, registry=reg
        ),
    )

    state_seq = [s for s, _ in esp32.listen_states]
    start_indices = [i for i, s in enumerate(state_seq) if s == "start"]
    stop_indices = [i for i, s in enumerate(state_seq) if s == "stop"]
    assert len(start_indices) == 2
    assert len(stop_indices) == 2
    # The lock guarantees: start_0 < stop_0 < start_1 < stop_1.
    assert (
        start_indices[0]
        < stop_indices[0]
        < start_indices[1]
        < stop_indices[1]
    )
    assert not is_recording()
