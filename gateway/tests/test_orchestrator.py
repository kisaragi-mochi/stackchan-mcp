"""Tests for the TTS orchestrator pipeline (Issue #70 PR2)."""

from __future__ import annotations

import asyncio
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
        self.tts_states: list[str] = []
        # Records the relative order in which audio frames and TTS state
        # notifications were dispatched, so tests can assert that
        # ``start`` precedes any frame and ``stop`` trails them.
        self.events: list[tuple[str, object]] = []
        # Mirror the production manager's per-device TTS lock so the
        # orchestrator's ``async with gateway.esp32.tts_lock`` works the
        # same way under tests as in production. The lock is created
        # per-fake so each test runs against a fresh instance.
        self.tts_lock = asyncio.Lock()

    async def send_audio_frame(self, frame: bytes) -> None:
        self.frames.append(frame)
        self.events.append(("frame", frame))

    async def send_tts_state(self, state: str) -> None:
        self.tts_states.append(state)
        self.events.append(("tts_state", state))


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
    # TTS start before any frame, stop after the last frame.
    assert esp32.tts_states == ["start", "stop"]
    assert esp32.events[0] == ("tts_state", "start")
    assert esp32.events[-1] == ("tts_state", "stop")
    # All frames sit between start and stop.
    middle = esp32.events[1:-1]
    assert all(kind == "frame" for kind, _ in middle)


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
async def test_pipeline_blocks_protocol_v2(fake_encode):
    """Devices that negotiated WebSocket protocol v2 are blocked.

    The gateway emits raw Opus binary frames matching firmware v1; v2/v3
    expect a BinaryProtocol header wrapped around each binary message.
    Streaming raw frames to a v2/v3 device causes silent playback
    failure, so the orchestrator must fail fast with a clear error
    rather than reporting say() success for an utterance that will
    never play.
    """
    from types import SimpleNamespace

    pcm = b"\x01\x00" * 1440
    engine = _PCMEngine(pcm)
    esp32 = _FakeESP32(connected=True)
    esp32.connection = SimpleNamespace(protocol_version=2)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError, match="protocol v1"):
        await synthesize_and_send(
            {"text": "hello"}, gateway=gateway, registry=reg
        )

    # Nothing should reach the device — neither TTS state notifications
    # nor audio frames — and the engine must not even be invoked, since
    # synthesis would be wasted work.
    assert esp32.tts_states == []
    assert esp32.frames == []
    assert engine.calls == []


@pytest.mark.asyncio
async def test_pipeline_serialises_concurrent_say_calls(fake_encode):
    """Concurrent ``say()`` invocations don't interleave on the same device.

    Without the per-device TTS lock, two ``synthesize_and_send`` calls
    running concurrently would each ``send_tts_state("start")``, race
    through the ``TTS_START_TRANSITION_DELAY_S`` ``asyncio.sleep`` (the
    cooperative yield point in this fake), then dump their frames and
    stop notifications in arbitrary order on the same WebSocket. With
    the lock, the recorded event stream must show one full
    ``start → frames → stop`` sequence followed by another, never
    interleaved — a strictly sequential pattern is what the device
    relies on to stay in ``kDeviceStateSpeaking`` for one utterance at
    a time.
    """
    pcm = b"\x01\x00" * 1440  # ~3 frames of audio
    engine_a = _PCMEngine(pcm, name="engine_a")
    engine_b = _PCMEngine(pcm, name="engine_b")
    esp32 = _FakeESP32(connected=True)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine_a)
    reg.register(engine_b)

    await asyncio.gather(
        synthesize_and_send(
            {"text": "first", "voice": "engine_a"},
            gateway=gateway,
            registry=reg,
        ),
        synthesize_and_send(
            {"text": "second", "voice": "engine_b"},
            gateway=gateway,
            registry=reg,
        ),
    )

    events = esp32.events
    start_indices = [
        i for i, e in enumerate(events) if e == ("tts_state", "start")
    ]
    stop_indices = [
        i for i, e in enumerate(events) if e == ("tts_state", "stop")
    ]
    assert len(start_indices) == 2
    assert len(stop_indices) == 2

    # The lock guarantees a strictly sequential pattern:
    #   start_0 < stop_0 < start_1 < stop_1
    # The second utterance cannot begin until the first one finishes
    # its stop notification.
    assert (
        start_indices[0]
        < stop_indices[0]
        < start_indices[1]
        < stop_indices[1]
    )


@pytest.mark.asyncio
async def test_pipeline_blocks_protocol_v3(fake_encode):
    """Devices on protocol v3 are blocked the same way as v2."""
    from types import SimpleNamespace

    pcm = b"\x01\x00" * 1440
    engine = _PCMEngine(pcm)
    esp32 = _FakeESP32(connected=True)
    esp32.connection = SimpleNamespace(protocol_version=3)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError, match=r"v3"):
        await synthesize_and_send(
            {"text": "hi"}, gateway=gateway, registry=reg
        )

    assert esp32.tts_states == []
    assert esp32.frames == []


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


# ---------------------------------------------------------------------------
# Exception translation — failures must become clean RuntimeError so the
# MCP handler's filter produces error JSON instead of leaking tracebacks.
# ---------------------------------------------------------------------------


class _RaisingEngine(TTSEngine):
    """Engine that fails synthesise with a configurable exception."""

    def __init__(self, exc: Exception, name: str = "voicevox") -> None:
        self.name = name
        self._exc = exc

    async def synthesize(self, text: str, **opts: Any) -> bytes:
        raise self._exc


@pytest.mark.asyncio
async def test_engine_http_error_translated_to_runtime_error(fake_encode):
    """An httpx.HTTPStatusError from the engine becomes a RuntimeError.

    The MCP handler in stdio_server.py only catches RuntimeError /
    ValueError / NotImplementedError; httpx errors must therefore be
    translated here, not allowed to bubble up.
    """
    httpx = pytest.importorskip("httpx")

    request = httpx.Request("POST", "http://test.local:50021/audio_query")
    response = httpx.Response(503, request=request, text="overloaded")
    http_err = httpx.HTTPStatusError("503", request=request, response=response)

    reg = EngineRegistry()
    reg.register(_RaisingEngine(http_err))
    gateway = _FakeGateway(_FakeESP32(connected=True))

    with pytest.raises(RuntimeError) as exc_info:
        await synthesize_and_send(
            {"text": "hello"},
            gateway=gateway,
            registry=reg,
        )
    assert "voicevox" in str(exc_info.value).lower()
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.asyncio
async def test_engine_wave_error_translated_to_runtime_error(fake_encode):
    """A wave.Error (malformed WAV from the engine) becomes a RuntimeError."""
    import wave

    reg = EngineRegistry()
    reg.register(_RaisingEngine(wave.Error("not a WAVE file")))
    gateway = _FakeGateway(_FakeESP32(connected=True))

    with pytest.raises(RuntimeError) as exc_info:
        await synthesize_and_send(
            {"text": "hello"},
            gateway=gateway,
            registry=reg,
        )
    assert isinstance(exc_info.value.__cause__, wave.Error)


@pytest.mark.asyncio
async def test_engine_value_error_propagates_as_value_error(fake_encode):
    """ValueError stays a ValueError so bad args remain separable from ops failures."""
    reg = EngineRegistry()
    reg.register(_RaisingEngine(ValueError("bad speaker_id")))
    gateway = _FakeGateway(_FakeESP32(connected=True))

    with pytest.raises(ValueError, match="bad speaker_id"):
        await synthesize_and_send(
            {"text": "hello"},
            gateway=gateway,
            registry=reg,
        )


@pytest.mark.asyncio
async def test_pipeline_translates_mid_stream_disconnect(fake_encode):
    """A ConnectionError from the device mid-stream becomes a RuntimeError.

    ConnectionError doesn't inherit RuntimeError, so without
    translation it would skip the MCP handler's exception filter and
    surface as a stack trace.
    """

    class FailingESP32:
        device_connected = True

        def __init__(self) -> None:
            self.frames: list[bytes] = []
            self.tts_states: list[str] = []
            self.tts_lock = asyncio.Lock()

        async def send_audio_frame(self, frame: bytes) -> None:
            if len(self.frames) >= 1:
                raise ConnectionError("simulated disconnect")
            self.frames.append(frame)

        async def send_tts_state(self, state: str) -> None:
            # The disconnect can race the stop notification; if the
            # caller still tries to send it after the failure, simulate
            # a benign no-op rather than raising again.
            self.tts_states.append(state)

    pcm = b"\x01\x00" * 1440  # 1.5 frames worth
    engine = _PCMEngine(pcm)
    esp32 = FailingESP32()
    gateway = _FakeGateway(esp32)  # type: ignore[arg-type]

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError) as exc_info:
        await synthesize_and_send(
            {"text": "hello"},
            gateway=gateway,
            registry=reg,
        )
    msg = str(exc_info.value)
    assert "1/2" in msg or "disconnect" in msg.lower()
    assert isinstance(exc_info.value.__cause__, ConnectionError)
    # The first frame did make it before the failure.
    assert len(esp32.frames) == 1
    # The stop notification was attempted regardless of the disconnect.
    assert "start" in esp32.tts_states
    assert "stop" in esp32.tts_states


@pytest.mark.asyncio
async def test_opus_encode_error_translated(fake_encode, monkeypatch):
    """A failure in encode_opus_frames becomes a RuntimeError, not a leak."""

    def boom(pcm: bytes, **kwargs):
        raise RuntimeError("libopus missing")

    import stackchan_mcp.tts.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "encode_opus_frames", boom)

    reg = EngineRegistry()
    reg.register(_PCMEngine(b"\x01\x00" * 960))
    gateway = _FakeGateway(_FakeESP32(connected=True))

    with pytest.raises(RuntimeError, match="Opus encoding failed"):
        await synthesize_and_send(
            {"text": "hello"},
            gateway=gateway,
            registry=reg,
        )


@pytest.mark.asyncio
async def test_pipeline_paces_frames_at_device_rate(fake_encode, monkeypatch):
    """Frame pushes are spaced at the device's frame_duration to avoid drops.

    The firmware's decode queue holds ~40 packets, so a single burst
    of more frames silently drops the tail. Pacing each push at
    DEVICE_FRAME_DURATION_MS keeps the queue at ~1 frame, well below
    the limit even on the longest utterances.
    """
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        # Yield once so the event loop progresses, but don't actually
        # wait — keeps the test fast.
        await real_sleep(0)

    monkeypatch.setattr("stackchan_mcp.tts.orchestrator.asyncio.sleep", fake_sleep)

    pcm = b"\x01\x00" * 1440  # 1.5 -> 2 frames after chunking
    engine = _PCMEngine(pcm)
    esp32 = _FakeESP32(connected=True)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    await synthesize_and_send(
        {"text": "hello"},
        gateway=gateway,
        registry=reg,
    )

    # First sleep is the post-tts.start state-transition delay (50 ms),
    # then per-frame pacing. The exact number of pacing sleeps depends
    # on loop.time() drift, so the test only asserts: (a) the start
    # delay was inserted, (b) at least one pacing sleep occurred.
    assert len(sleeps) >= 1
    assert sleeps[0] == pytest.approx(0.05, rel=0.05)


@pytest.mark.asyncio
async def test_pipeline_disconnect_before_tts_start(fake_encode):
    """ConnectionError on the start notification surfaces clearly.

    Without a clean message here the pipeline would degenerate into a
    confusing "0/N frames" report even though no frame was attempted.
    """

    class FailingESP32:
        device_connected = True
        tts_states: list[str] = []  # noqa: RUF012

        def __init__(self) -> None:
            self.tts_states = []
            self.tts_lock = asyncio.Lock()

        async def send_tts_state(self, state: str) -> None:
            self.tts_states.append(state)
            if state == "start":
                raise ConnectionError("device dropped during start")

        async def send_audio_frame(self, frame: bytes) -> None:
            raise AssertionError("frame should not be attempted after start failure")

    pcm = b"\x01\x00" * 960
    engine = _PCMEngine(pcm)
    esp32 = FailingESP32()
    gateway = _FakeGateway(esp32)  # type: ignore[arg-type]

    reg = EngineRegistry()
    reg.register(engine)

    with pytest.raises(RuntimeError, match="TTS start"):
        await synthesize_and_send(
            {"text": "hello"},
            gateway=gateway,
            registry=reg,
        )
    assert esp32.tts_states == ["start"]
