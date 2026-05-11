"""Tests for the TTS orchestrator pipeline (Issue #70 PR2)."""

from __future__ import annotations

import array
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
        # Issue #85: per-frame envelopes recorded as (frame_id, rms)
        # tuples. The orchestrator emits one envelope before each
        # audio frame so the firmware can drive amplitude-based mouth
        # shapes; tests can assert the count and ordering.
        self.envelopes: list[tuple[int, float]] = []
        # Records the relative order in which audio frames, envelopes,
        # and TTS state notifications were dispatched, so tests can
        # assert that ``start`` precedes any frame and ``stop`` trails
        # them, and that each envelope precedes its corresponding
        # audio frame.
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

    async def send_tts_envelope(
        self,
        frame_id: int,
        rms: float,
        *,
        lock_acquire_timeout: float | None = None,
    ) -> bool:
        # ``lock_acquire_timeout`` is honoured implicitly: this fake
        # never blocks, so acquire-timeout never fires.
        del lock_acquire_timeout
        self.envelopes.append((frame_id, rms))
        self.events.append(("envelope", (frame_id, rms)))
        return True


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
    # Issue #85: each envelope is awaited inline before its
    # corresponding audio frame so the WebSocket write order is
    # guaranteed (firmware needs envelope[N] to land before audio
    # frame[N] for the per-frame mouth shape map to track the right
    # frame). The middle slice should therefore be a strict
    # ``envelope, frame`` interleave.
    middle = esp32.events[1:-1]
    assert len(esp32.envelopes) == len(esp32.frames)
    pair_kinds = [kind for kind, _ in middle]
    assert pair_kinds == ["envelope", "frame"] * len(esp32.frames), (
        f"expected strict envelope-then-frame interleave, got {pair_kinds}"
    )
    # frame_id sequence is contiguous starting from 0.
    sent_frame_ids = [fid for fid, _ in esp32.envelopes]
    assert sent_frame_ids == list(range(len(esp32.frames)))


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
            self.envelopes: list[tuple[int, float]] = []
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

        async def send_tts_envelope(
            self,
            frame_id: int,
            rms: float,
            *,
            lock_acquire_timeout: float | None = None,
        ) -> bool:
            del lock_acquire_timeout
            # Envelope delivery is best-effort; record but don't fail
            # so we exercise the same disconnect path on send_audio_frame.
            self.envelopes.append((frame_id, rms))
            return True

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

        async def send_tts_envelope(
            self,
            frame_id: int,
            rms: float,
            *,
            lock_acquire_timeout: float | None = None,
        ) -> bool:
            del lock_acquire_timeout
            raise AssertionError(
                "envelope should not be attempted after start failure"
            )

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


# ---------------------------------------------------------------------------
# Issue #85 — per-frame audio amplitude envelope channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_emits_envelope_with_real_rms(fake_encode):
    """Envelopes carry the actual RMS of each PCM frame, not a placeholder.

    Builds two frames with very different amplitudes (silence vs. a
    near-full-scale square wave) so the test can assert that the
    second frame's envelope is much larger than the first. This guards
    against an implementation that always sends ``0.0`` (e.g. a stub
    that records the call shape but never wires up
    :func:`compute_pcm_frame_rms`).
    """
    samples_per_frame = (
        DEVICE_SAMPLE_RATE * DEVICE_FRAME_DURATION_MS // 1000
    )
    # Frame 0: pure silence -> RMS = 0
    silence = b"\x00\x00" * samples_per_frame
    # Frame 1: alternating ±10000 (near full-scale) -> RMS ≈ 10000/32768
    loud_samples = array.array(
        "h", [10000 if i % 2 == 0 else -10000 for i in range(samples_per_frame)]
    )
    loud = loud_samples.tobytes()
    pcm = silence + loud

    engine = _PCMEngine(pcm)
    esp32 = _FakeESP32(connected=True)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    await synthesize_and_send(
        {"text": "envelope-check"}, gateway=gateway, registry=reg
    )

    assert len(esp32.envelopes) == 2
    frame_id_0, rms_0 = esp32.envelopes[0]
    frame_id_1, rms_1 = esp32.envelopes[1]
    assert frame_id_0 == 0
    assert frame_id_1 == 1
    assert rms_0 == 0.0
    # Loud frame's RMS should be roughly 10000/32768 ≈ 0.305.
    assert rms_1 == pytest.approx(10000 / 32768, rel=0.01)


@pytest.mark.asyncio
async def test_pipeline_envelope_send_failure_is_nonfatal(fake_encode):
    """A ConnectionError on send_tts_envelope must not abort the utterance.

    Envelope delivery is best-effort: the firmware falls back to a
    fixed mouth cycle when envelopes stop arriving, so a transient
    failure on the envelope channel is invisible to the user. The
    audio frames must still be pushed and the stop notification
    must still fire so the device leaves ``kDeviceStateSpeaking``.
    """

    class EnvelopeFailingESP32:
        device_connected = True

        def __init__(self) -> None:
            self.frames: list[bytes] = []
            self.tts_states: list[str] = []
            self.envelope_attempts = 0
            self.tts_lock = asyncio.Lock()

        async def send_audio_frame(self, frame: bytes) -> None:
            self.frames.append(frame)

        async def send_tts_state(self, state: str) -> None:
            self.tts_states.append(state)

        async def send_tts_envelope(
            self,
            frame_id: int,
            rms: float,
            *,
            lock_acquire_timeout: float | None = None,
        ) -> bool:
            del lock_acquire_timeout
            self.envelope_attempts += 1
            raise ConnectionError("envelope channel down")

    pcm = b"\x01\x00" * 1440  # 1.5 -> 2 frames
    engine = _PCMEngine(pcm)
    esp32 = EnvelopeFailingESP32()
    gateway = _FakeGateway(esp32)  # type: ignore[arg-type]

    reg = EngineRegistry()
    reg.register(engine)

    result = await synthesize_and_send(
        {"text": "hello"}, gateway=gateway, registry=reg
    )

    assert result["frame_count"] == 2
    assert len(esp32.frames) == 2
    assert esp32.tts_states == ["start", "stop"]
    # Every frame still attempted an envelope before its audio.
    assert esp32.envelope_attempts == 2


@pytest.mark.asyncio
async def test_pipeline_envelope_length_mismatch_skips_envelope_channel(
    fake_encode, monkeypatch
):
    """Defensive guard: opus/envelope length mismatch falls back gracefully.

    The two helpers (:func:`chunk_pcm_into_frames` and
    :func:`encode_opus_frames`) share ``samples_per_frame`` so a
    mismatch should be impossible in production, but a future refactor
    of either one shouldn't silently misalign envelopes against the
    wrong audio frame. The orchestrator detects the mismatch and skips
    the envelope channel for that utterance instead of corrupting
    lip-sync; this test pins that contract.
    """
    import stackchan_mcp.tts.orchestrator as orchestrator

    real_chunk = orchestrator.chunk_pcm_into_frames

    def short_chunks(pcm, **kwargs):
        # Drop the first chunk so envelopes is one shorter than
        # opus_frames (which the fake_encode fixture sizes correctly).
        chunks = list(real_chunk(pcm, **kwargs))
        return iter(chunks[1:])

    monkeypatch.setattr(orchestrator, "chunk_pcm_into_frames", short_chunks)

    pcm = b"\x01\x00" * 1440  # -> 2 opus frames, but only 1 envelope chunk
    engine = _PCMEngine(pcm)
    esp32 = _FakeESP32(connected=True)
    gateway = _FakeGateway(esp32)

    reg = EngineRegistry()
    reg.register(engine)

    result = await synthesize_and_send(
        {"text": "hello"}, gateway=gateway, registry=reg
    )

    # Audio still pushed in full.
    assert result["frame_count"] == 2
    assert len(esp32.frames) == 2
    # Envelope channel skipped entirely for this utterance.
    assert esp32.envelopes == []


@pytest.mark.asyncio
async def test_pipeline_envelope_lock_contention_skipped_silently(fake_encode):
    """Envelope drops cleanly when the connection's send lock is busy.

    Regression for the codex findings on Issue #85: an unbounded
    inline ``await send_tts_envelope`` would chain into the audio
    frame's pacing sleep math, so a slow envelope (e.g. WiFi
    backpressure or another sender holding the connection's send
    lock) could push every iteration past the 60 ms audio pacing
    budget. The orchestrator now passes ``lock_acquire_timeout=
    TTS_ENVELOPE_SEND_TIMEOUT_S`` to ``send_tts_envelope``, which
    silently returns ``False`` on the timeout path instead of
    raising or cancelling an in-flight ``ws.send()`` (the
    ``websockets`` library documents that cancellation as unsafe).
    The firmware's freshness check then falls back to its fixed
    lip-sync cycle, hiding the dropped envelope from the user.
    """

    class LockBusyESP32:
        device_connected = True

        def __init__(self) -> None:
            self.frames: list[bytes] = []
            self.tts_states: list[str] = []
            self.envelopes: list[tuple[int, float]] = []
            self.envelope_attempts = 0
            self.tts_lock = asyncio.Lock()
            self._loop = asyncio.get_event_loop()
            self._prev_frame_at: float | None = None
            self.frame_intervals_ms: list[float] = []

        async def send_audio_frame(self, frame: bytes) -> None:
            now = self._loop.time()
            if self._prev_frame_at is not None:
                self.frame_intervals_ms.append(
                    (now - self._prev_frame_at) * 1000.0
                )
            self._prev_frame_at = now
            self.frames.append(frame)

        async def send_tts_state(self, state: str) -> None:
            self.tts_states.append(state)

        async def send_tts_envelope(
            self,
            frame_id: int,
            rms: float,
            *,
            lock_acquire_timeout: float | None = None,
        ) -> bool:
            self.envelope_attempts += 1
            # Simulate the ``lock_acquire_timeout`` path inside
            # ``ESP32Connection.send_tts_envelope``: the lock could
            # not be acquired in time, so the envelope is dropped
            # without ever touching the wire. ``False`` tells the
            # caller "skipped, firmware fallback covers it".
            if lock_acquire_timeout is not None:
                await asyncio.sleep(lock_acquire_timeout)
                return False
            self.envelopes.append((frame_id, rms))
            return True

    pcm = b"\x01\x00" * (960 * 5)  # 5 frames @ 60 ms
    engine = _PCMEngine(pcm)
    esp32 = LockBusyESP32()
    gateway = _FakeGateway(esp32)  # type: ignore[arg-type]

    reg = EngineRegistry()
    reg.register(engine)

    result = await synthesize_and_send(
        {"text": "hello"}, gateway=gateway, registry=reg
    )

    assert result["frame_count"] == 5
    # Each frame attempted an envelope but every one was skipped.
    assert esp32.envelope_attempts == 5
    assert esp32.envelopes == []
    # Audio pacing must stay within budget despite the lock-acquire
    # timeout eating ~30 ms per frame. Worst case per frame is the
    # lock timeout (30 ms) + the 60 ms pacing target; allow 25 ms
    # slack for event-loop scheduling jitter on a busy CI runner.
    from stackchan_mcp.tts.orchestrator import TTS_ENVELOPE_SEND_TIMEOUT_S

    assert len(esp32.frame_intervals_ms) == 4
    budget_ms = (TTS_ENVELOPE_SEND_TIMEOUT_S * 1000) + 60 + 25
    for interval in esp32.frame_intervals_ms:
        assert interval < budget_ms, (
            f"audio pacing slipped to {interval:.1f} ms "
            f"(budget {budget_ms:.0f} ms); envelope skip path may "
            f"have regressed"
        )
    # Stop still fires after all frames.
    assert esp32.tts_states[-1] == "stop"


@pytest.mark.asyncio
async def test_pipeline_envelope_precedes_audio_per_frame(fake_encode):
    """Each envelope reaches the wire before its corresponding audio frame.

    The firmware's per-frame mouth shape map (Issue #85) only works
    if envelope[N] arrives at-or-before audio[N] — otherwise the
    mouth update for frame N races the audio playback or lands one
    frame late, eroding the per-frame sync the protocol promises.
    The orchestrator awaits ``send_tts_envelope`` inline (with a
    small timeout cap to bound pacing slip) precisely so this
    ordering is guaranteed at the WebSocket write layer; this test
    pins it for a 3-frame utterance with the envelope deliberately
    delayed slightly to make any reordering observable.
    """

    class OrderedESP32:
        device_connected = True

        def __init__(self) -> None:
            self.events: list[str] = []
            self.tts_lock = asyncio.Lock()

        async def send_audio_frame(self, frame: bytes) -> None:
            self.events.append("frame")

        async def send_tts_state(self, state: str) -> None:
            self.events.append(f"state:{state}")

        async def send_tts_envelope(
            self,
            frame_id: int,
            rms: float,
            *,
            lock_acquire_timeout: float | None = None,
        ) -> bool:
            del lock_acquire_timeout
            # Tiny artificial delay so any reordering bug surfaces
            # rather than being masked by both sends completing
            # synchronously. Stays well under the orchestrator's
            # envelope timeout so the send completes normally.
            await asyncio.sleep(0.001)
            self.events.append(f"envelope:{frame_id}")
            return True

    pcm = b"\x01\x00" * (960 * 3)
    engine = _PCMEngine(pcm)
    esp32 = OrderedESP32()
    gateway = _FakeGateway(esp32)  # type: ignore[arg-type]

    reg = EngineRegistry()
    reg.register(engine)

    await synthesize_and_send(
        {"text": "hello"}, gateway=gateway, registry=reg
    )

    # Expected event sequence — strict envelope-then-frame interleave
    # bracketed by the start/stop notifications.
    assert esp32.events == [
        "state:start",
        "envelope:0",
        "frame",
        "envelope:1",
        "frame",
        "envelope:2",
        "frame",
        "state:stop",
    ]
