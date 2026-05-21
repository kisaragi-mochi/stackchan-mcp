"""Tests for ESP32 client connection management."""

import asyncio
import gc
import json

import pytest
import pytest_asyncio
import websockets

from stackchan_mcp.esp32_client import ESP32Connection, ESP32Manager, _hardware_lane


@pytest_asyncio.fixture
async def manager():
    """Create and start an ESP32Manager on a free port."""
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)  # Port 0 = OS picks a free port

    # Get the actual port
    server = mgr._server
    port = server.sockets[0].getsockname()[1]
    mgr._test_port = port

    yield mgr
    await mgr.stop()


@pytest.mark.asyncio
async def test_manager_starts_and_stops():
    """Manager can start and stop cleanly."""
    mgr = ESP32Manager()
    await mgr.start("127.0.0.1", 0)
    assert mgr._server is not None
    await mgr.stop()
    assert mgr._server is None


@pytest.mark.asyncio
async def test_no_device_connected():
    """call_tool returns error when no device is connected."""
    mgr = ESP32Manager()
    result, error = await mgr.call_tool("self.robot.set_head_angles", {"yaw": 0, "pitch": 0})
    assert result is None
    assert error is not None
    assert "not connected" in error["message"].lower() or "No ESP32" in error["message"]


@pytest.mark.asyncio
async def test_get_status_disconnected():
    """get_status returns disconnected state."""
    mgr = ESP32Manager()
    status = mgr.get_status()
    assert status["connected"] is False
    assert status["device_id"] is None


@pytest.mark.asyncio
async def test_esp32_hello_handshake(manager):
    """ESP32 can connect and complete hello handshake."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        # Send hello
        hello = {
            "type": "hello",
            "version": 1,
            "features": {"mcp": True},
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
        await ws.send(json.dumps(hello))

        # Receive hello response
        resp_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        resp = json.loads(resp_raw)
        assert resp["type"] == "hello"
        assert resp["version"] == 1
        assert "session_id" in resp

        # Receive initialize request from gateway
        init_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        init_msg = json.loads(init_raw)
        assert init_msg["type"] == "mcp"
        assert init_msg["payload"]["method"] == "initialize"

        # Send initialize response
        init_resp = {
            "session_id": init_msg["session_id"],
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": init_msg["payload"]["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test-device", "version": "1.0.0"},
                },
            },
        }
        await ws.send(json.dumps(init_resp))

        # Receive tools/list request
        tools_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        tools_msg = json.loads(tools_raw)
        assert tools_msg["type"] == "mcp"
        assert tools_msg["payload"]["method"] == "tools/list"

        # Send tools/list response
        tools_resp = {
            "session_id": tools_msg["session_id"],
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": tools_msg["payload"]["id"],
                "result": {
                    "tools": [
                        {
                            "name": "self.robot.set_head_angles",
                            "description": "Set head angles",
                            "inputSchema": {"type": "object"},
                        }
                    ],
                    "nextCursor": "",
                },
            },
        }
        await ws.send(json.dumps(tools_resp))

        # Wait for manager to process
        await asyncio.sleep(0.2)

        # Verify connection is established
        assert manager.device_connected is True
        status = manager.get_status()
        assert status["connected"] is True
        assert status["tools_count"] == 1


@pytest.mark.asyncio
async def test_esp32_tool_call_relay(manager):
    """Gateway relays tool calls to ESP32."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        # Complete handshake
        await _complete_handshake(ws, tools=[
            {"name": "self.robot.set_head_angles", "description": "Set head", "inputSchema": {}}
        ])

        await asyncio.sleep(0.2)

        # Now call tool via manager
        call_task = asyncio.create_task(
            manager.call_tool("self.robot.set_head_angles", {"yaw": 45, "pitch": 10})
        )

        # ESP32 receives the request
        req_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        req_msg = json.loads(req_raw)
        assert req_msg["type"] == "mcp"
        assert req_msg["payload"]["method"] == "tools/call"
        assert req_msg["payload"]["params"]["name"] == "self.robot.set_head_angles"
        assert req_msg["payload"]["params"]["arguments"] == {"yaw": 45, "pitch": 10}

        # ESP32 sends response
        tool_resp = {
            "session_id": req_msg["session_id"],
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": req_msg["payload"]["id"],
                "result": {
                    "content": [{"type": "text", "text": "true"}],
                    "isError": False,
                },
            },
        }
        await ws.send(json.dumps(tool_resp))

        # Verify result
        result, error = await asyncio.wait_for(call_task, timeout=5.0)
        assert error is None
        assert result["content"][0]["text"] == "true"


@pytest.mark.asyncio
async def test_esp32_disconnect_handling(manager):
    """Manager handles ESP32 disconnection gracefully."""
    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await _complete_handshake(ws)
        await asyncio.sleep(0.2)
        assert manager.device_connected is True

    # Connection closed
    await asyncio.sleep(0.2)
    assert manager.device_connected is False


@pytest.mark.asyncio
async def test_auth_rejection(manager):
    """Unauthorized connections are rejected."""
    import os
    port = manager._test_port

    # Set token to require auth
    os.environ["STACKCHAN_TOKEN"] = "test-secret-token"
    try:
        # Try connecting without auth — should fail
        with pytest.raises(Exception):
            async with websockets.connect(
                f"ws://127.0.0.1:{port}",
                additional_headers={"Authorization": "Bearer wrong-token"},
            ) as ws:
                await ws.recv()
    finally:
        del os.environ["STACKCHAN_TOKEN"]


# ---------------------------------------------------------------------------
# Parallel hardware-lane dispatch (Issue #73)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "lane"),
    [
        ("self.robot.set_head_angles", "servo"),
        ("self.led.set_many", "led"),
        ("self.display.set_avatar", "avatar"),
        ("self.screen.set_brightness", "display"),
        ("self.audio_speaker.set_volume", "audio"),
        ("self.camera.take_photo", "camera"),
        ("self.touch.get_touch_state", "touch"),
        ("self.get_device_status", "status"),
        ("self.unknown.experimental", "default"),
    ],
)
def test_hardware_lane_covers_gateway_tool_routes(tool_name, lane):
    """Gateway-routed ESP32 tools map to explicit hardware lanes."""
    assert _hardware_lane(tool_name) == lane


@pytest.mark.asyncio
async def test_connection_pipelines_concurrent_tool_calls_before_first_response():
    """Concurrent tools/call requests are sent before either response arrives."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-parallel")  # type: ignore[arg-type]

    servo_task = asyncio.create_task(
        conn.call_tool("self.robot.set_head_angles", {"yaw": 10, "pitch": 30})
    )
    led_task = asyncio.create_task(
        conn.call_tool("self.led.set_many", {"colors": "[[255, 0, 0]]"})
    )

    await asyncio.sleep(0)

    assert len(ws.sent) == 2
    sent_messages = [json.loads(message) for message in ws.sent]
    request_ids = [message["payload"]["id"] for message in sent_messages]
    assert [message["payload"]["method"] for message in sent_messages] == [
        "tools/call",
        "tools/call",
    ]
    assert [message["payload"]["params"]["name"] for message in sent_messages] == [
        "self.robot.set_head_angles",
        "self.led.set_many",
    ]

    conn.handle_response(
        {
            "jsonrpc": "2.0",
            "id": request_ids[1],
            "result": {"content": [{"type": "text", "text": "led"}]},
        }
    )
    conn.handle_response(
        {
            "jsonrpc": "2.0",
            "id": request_ids[0],
            "result": {"content": [{"type": "text", "text": "servo"}]},
        }
    )

    servo_result, led_result = await asyncio.gather(servo_task, led_task)
    assert servo_result[0]["content"][0]["text"] == "servo"
    assert servo_result[1] is None
    assert led_result[0]["content"][0]["text"] == "led"
    assert led_result[1] is None


@pytest.mark.asyncio
async def test_connection_removes_pending_request_when_call_is_cancelled():
    """Cancelling a tool call does not leave a stale pending response slot."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-cancel")  # type: ignore[arg-type]

    task = asyncio.create_task(
        conn.call_tool("self.robot.set_head_angles", {"yaw": 10, "pitch": 30})
    )

    await asyncio.sleep(0)
    assert len(ws.sent) == 1
    assert len(conn._pending) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert conn._pending == {}


class _GateableConnection:
    """Fake initialized connection with per-tool release gates."""

    connected = True
    initialized = True

    def __init__(self, releases: dict[str, asyncio.Event]) -> None:
        self.releases = releases
        self.started: list[str] = []
        self.finished: list[str] = []
        self.all_started = asyncio.Event()

    async def call_tool(self, name, arguments):  # noqa: ARG002 - test fake
        self.started.append(name)
        if len(self.started) >= len(self.releases):
            self.all_started.set()
        await self.releases[name].wait()
        self.finished.append(name)
        return {"content": [{"type": "text", "text": name}]}, None


@pytest.mark.asyncio
async def test_manager_call_tools_dispatches_independent_lanes_in_parallel():
    """Servo, LED, and avatar calls start together instead of waiting in line."""
    releases = {
        "self.robot.set_head_angles": asyncio.Event(),
        "self.led.set_many": asyncio.Event(),
        "self.display.set_avatar": asyncio.Event(),
    }
    connection = _GateableConnection(releases)
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]

    task = asyncio.create_task(
        mgr.call_tools(
            [
                ("self.robot.set_head_angles", {"yaw": 0, "pitch": 45}),
                ("self.led.set_many", {"colors": "[]"}),
                ("self.display.set_avatar", {"face": "happy"}),
            ]
        )
    )

    await asyncio.wait_for(connection.all_started.wait(), timeout=1.0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.led.set_many",
        "self.display.set_avatar",
    ]
    assert connection.finished == []

    for release in releases.values():
        release.set()
    results = await asyncio.wait_for(task, timeout=1.0)

    assert [result[0]["content"][0]["text"] for result in results] == [
        "self.robot.set_head_angles",
        "self.led.set_many",
        "self.display.set_avatar",
    ]
    assert [error for _, error in results] == [None, None, None]


@pytest.mark.asyncio
async def test_manager_call_tool_uses_lane_dispatch_for_existing_api():
    """Existing single-tool API can still overlap independent hardware lanes."""
    releases = {
        "self.robot.set_head_angles": asyncio.Event(),
        "self.led.set_many": asyncio.Event(),
    }
    connection = _GateableConnection(releases)
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]

    servo_task = asyncio.create_task(
        mgr.call_tool("self.robot.set_head_angles", {"yaw": 0, "pitch": 45})
    )
    led_task = asyncio.create_task(
        mgr.call_tool("self.led.set_many", {"colors": "[]"})
    )

    await asyncio.wait_for(connection.all_started.wait(), timeout=1.0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.led.set_many",
    ]
    assert connection.finished == []

    for release in releases.values():
        release.set()
    results = await asyncio.wait_for(
        asyncio.gather(servo_task, led_task),
        timeout=1.0,
    )

    assert [result[0]["content"][0]["text"] for result in results] == [
        "self.robot.set_head_angles",
        "self.led.set_many",
    ]
    assert [error for _, error in results] == [None, None]


@pytest.mark.asyncio
async def test_manager_call_tools_serializes_calls_on_same_hardware_lane():
    """Two servo calls keep their relative order on the servo lane."""
    releases = {
        "self.robot.set_head_angles": asyncio.Event(),
        "self.robot.get_head_angles": asyncio.Event(),
    }
    connection = _GateableConnection(releases)
    mgr = ESP32Manager()
    mgr._connection = connection  # type: ignore[assignment]

    task = asyncio.create_task(
        mgr.call_tools(
            [
                ("self.robot.set_head_angles", {"yaw": 0, "pitch": 45}),
                ("self.robot.get_head_angles", {}),
            ]
        )
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert connection.started == ["self.robot.set_head_angles"]

    releases["self.robot.set_head_angles"].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert connection.started == [
        "self.robot.set_head_angles",
        "self.robot.get_head_angles",
    ]

    releases["self.robot.get_head_angles"].set()
    await asyncio.wait_for(task, timeout=1.0)
    assert connection.finished == [
        "self.robot.set_head_angles",
        "self.robot.get_head_angles",
    ]


# ---------------------------------------------------------------------------
# send_audio_frame (TTS pipeline egress, Issue #70 PR2)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal stand-in for websockets.ServerConnection used in unit tests."""

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []

    async def send(self, data):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_connection_send_audio_frame_sends_binary():
    """ESP32Connection.send_audio_frame writes the bytes to the underlying WS."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    await conn.send_audio_frame(b"opus_payload_bytes")

    assert ws.sent == [b"opus_payload_bytes"]


@pytest.mark.asyncio
async def test_connection_send_audio_frame_raises_after_disconnect():
    """A disconnected connection refuses to send rather than silently dropping."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    conn.disconnect()

    with pytest.raises(ConnectionError):
        await conn.send_audio_frame(b"opus_payload_bytes")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_manager_send_audio_frame_no_device():
    """ESP32Manager.send_audio_frame raises when no device is attached.

    The orchestrator turns this into a clean MCP error JSON; without
    this guard the call would AttributeError on a None connection.
    """
    mgr = ESP32Manager()

    with pytest.raises(ConnectionError):
        await mgr.send_audio_frame(b"opus_payload_bytes")


@pytest.mark.asyncio
async def test_connection_send_tts_state_sends_json():
    """ESP32Connection.send_tts_state writes a tts state JSON message."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-tts")  # type: ignore[arg-type]

    await conn.send_tts_state("start")

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload == {
        "session_id": "session-tts",
        "type": "tts",
        "state": "start",
    }


@pytest.mark.asyncio
async def test_connection_send_tts_state_raises_after_disconnect():
    """A disconnected connection refuses to send TTS notifications."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-tts")  # type: ignore[arg-type]

    conn.disconnect()

    with pytest.raises(ConnectionError):
        await conn.send_tts_state("stop")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_manager_send_tts_state_no_device():
    """ESP32Manager.send_tts_state raises when no device is attached."""
    mgr = ESP32Manager()

    with pytest.raises(ConnectionError):
        await mgr.send_tts_state("start")


# ---------------------------------------------------------------------------
# send_listen_state (STT pipeline, Issue #91)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_send_listen_state_start_includes_mode():
    """listen.start carries a mode field on the wire."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-listen")  # type: ignore[arg-type]

    await conn.send_listen_state("start", mode="manual")

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload == {
        "session_id": "session-listen",
        "type": "listen",
        "state": "start",
        "mode": "manual",
    }


@pytest.mark.asyncio
async def test_connection_send_listen_state_stop_omits_mode():
    """listen.stop has no mode field — the wire shape mirrors the firmware.

    The firmware's ``OnIncomingJson`` listen handler only consults
    ``mode`` on ``state="start"``; sending it on stop would be noise.
    """
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-listen")  # type: ignore[arg-type]

    await conn.send_listen_state("stop")

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload == {
        "session_id": "session-listen",
        "type": "listen",
        "state": "stop",
    }


@pytest.mark.asyncio
async def test_connection_send_listen_state_raises_after_disconnect():
    """A disconnected connection refuses to send listen notifications."""
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-listen")  # type: ignore[arg-type]

    conn.disconnect()

    with pytest.raises(ConnectionError):
        await conn.send_listen_state("start", mode="manual")
    assert ws.sent == []


@pytest.mark.asyncio
async def test_manager_send_listen_state_no_device():
    """ESP32Manager.send_listen_state raises when no device is attached."""
    mgr = ESP32Manager()

    with pytest.raises(ConnectionError):
        await mgr.send_listen_state("start")


def test_manager_listen_lock_is_same_as_tts_lock():
    """listen() and say() share a single audio-path lock per device.

    Without sharing, the firmware's ``HandleStartListeningEvent`` could
    abort an in-flight ``say()`` mid-utterance the moment a concurrent
    ``listen()`` arrived (state == kDeviceStateSpeaking →
    AbortSpeaking + SetListeningMode), and conversely TTS frames in
    flight would leak into a concurrent capture's buffer. Treating
    the audio path as a single serialised resource keeps the device's
    state machine observable from the gateway side.
    """
    mgr = ESP32Manager()
    assert mgr.tts_lock is mgr.listen_lock


class _FailingWebSocket:
    """WebSocket that raises a websockets-specific error on send()."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.send_calls = 0

    async def send(self, data):
        self.send_calls += 1
        raise self._exc


@pytest.mark.asyncio
async def test_send_audio_frame_translates_websockets_close_to_connection_error():
    """websockets.ConnectionClosed becomes ConnectionError + marks dead.

    Without translation the websockets-specific exception would
    bypass the orchestrator's ``except ConnectionError`` filter and
    leak as a stack trace through the MCP transport.
    """
    import websockets.exceptions

    closed = websockets.exceptions.ConnectionClosed(rcvd=None, sent=None)
    ws = _FailingWebSocket(closed)
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    with pytest.raises(ConnectionError, match="WebSocket send"):
        await conn.send_audio_frame(b"opus")

    # After the translated failure, the connection is marked dead so
    # subsequent sends fail fast without re-touching the dead socket.
    assert not conn.connected
    with pytest.raises(ConnectionError):
        await conn.send_audio_frame(b"more")
    assert ws.send_calls == 1


@pytest.mark.asyncio
async def test_send_tts_state_translates_oserror_to_connection_error():
    """OSError on send (e.g. broken pipe) is translated to ConnectionError."""
    ws = _FailingWebSocket(OSError("broken pipe"))
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    with pytest.raises(ConnectionError, match="WebSocket send"):
        await conn.send_tts_state("start")
    assert not conn.connected


@pytest.mark.asyncio
async def test_send_mcp_request_translates_send_failure_and_marks_disconnected():
    """tools/call send failures use the same connection-state handling as TTS."""
    ws = _FailingWebSocket(OSError("broken pipe"))
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    loop_errors = []
    previous_handler = loop.get_exception_handler()

    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        result, error = await conn.call_tool("self.robot.set_head_angles", {})
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert result is None
    assert error is not None
    assert "WebSocket send failed" in error["message"]
    assert not conn.connected
    assert conn._pending == {}
    assert ws.send_calls == 1
    assert loop_errors == []


def test_connection_default_protocol_version_is_one():
    """Fresh ESP32Connection defaults to WebSocket protocol v1.

    v1 is what the gateway's audio framing currently targets (raw
    Opus binary frames). v2/v3 wrap payloads in a BinaryProtocol
    header which this gateway does not yet emit; the hello handler
    logs a warning when a non-v1 device negotiates so operators know
    the TTS path may not work for them.
    """
    ws = _FakeWebSocket()
    conn = ESP32Connection(ws, session_id="session-1")  # type: ignore[arg-type]

    assert conn.protocol_version == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _complete_handshake(ws, tools=None):
    """Complete the full ESP32 handshake sequence."""
    if tools is None:
        tools = []

    # Send hello
    hello = {
        "type": "hello",
        "version": 1,
        "features": {"mcp": True},
        "transport": "websocket",
    }
    await ws.send(json.dumps(hello))

    # Receive hello response
    await asyncio.wait_for(ws.recv(), timeout=5.0)

    # Receive and respond to initialize
    init_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    init_msg = json.loads(init_raw)
    init_resp = {
        "session_id": init_msg["session_id"],
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": init_msg["payload"]["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-device", "version": "1.0.0"},
            },
        },
    }
    await ws.send(json.dumps(init_resp))

    # Receive and respond to tools/list
    tools_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    tools_msg = json.loads(tools_raw)
    tools_resp = {
        "session_id": tools_msg["session_id"],
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": tools_msg["payload"]["id"],
            "result": {"tools": tools, "nextCursor": ""},
        },
    }
    await ws.send(json.dumps(tools_resp))


# --- Device-driven listen capture --------------------------------------------


@pytest_asyncio.fixture
async def manager_with_hook(monkeypatch):
    """ESP32Manager started with a configured audio hook URL.

    ``push_audio_capture`` is patched to record invocations into a
    shared list so tests can assert the hook was triggered without
    starting a real HTTP server. The recorded payload is the actual
    ``frames`` list the gateway captured for that listen window.
    """
    calls: list[dict] = []

    async def _fake_push(hook_url, token, frames, *, session_id="", timeout_s=10.0):
        calls.append(
            {
                "hook_url": hook_url,
                "token": token,
                "frames": list(frames),
                "session_id": session_id,
            }
        )
        return True

    monkeypatch.setattr(
        "stackchan_mcp.esp32_client.push_audio_capture", _fake_push
    )

    mgr = ESP32Manager()
    await mgr.start(
        "127.0.0.1",
        0,
        audio_hook_url="http://test/hook",
        audio_hook_token="test-token",
    )
    server = mgr._server
    mgr._test_port = server.sockets[0].getsockname()[1]

    try:
        yield mgr, calls
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_device_driven_listen_pushes_to_hook(manager_with_hook):
    """device → gateway listen.start/stop sequence forwards frames
    captured between the two messages to the audio hook."""
    from stackchan_mcp.audio_stream import is_recording

    mgr, calls = manager_with_hook
    port = mgr._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await _complete_handshake(ws)

        # Device-initiated listen.start
        await ws.send(json.dumps({
            "session_id": "",  # device fills its own; ignored on receive
            "type": "listen",
            "state": "start",
            "mode": "manual",
        }))

        # Wait for gateway to open the recording slot. We can't observe
        # the gateway's internals through the WS, so poll the module
        # state for a short bounded time.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if is_recording():
                break
        assert is_recording(), "gateway did not open the recording slot"

        # Stream a couple of binary "audio" frames
        await ws.send(b"\xaa\xbb\xcc")
        await ws.send(b"\xdd\xee\xff")

        # Give the gateway a moment to buffer the frames
        await asyncio.sleep(0.1)

        # Device-initiated listen.stop
        await ws.send(json.dumps({
            "session_id": "",
            "type": "listen",
            "state": "stop",
        }))

        # Wait for the push task to fire (asyncio.create_task in the
        # handler dispatches it eagerly; one event-loop tick is enough,
        # but we give it a few to absorb scheduling jitter).
        for _ in range(20):
            await asyncio.sleep(0.05)
            if calls:
                break

    assert len(calls) == 1
    assert calls[0]["hook_url"] == "http://test/hook"
    assert calls[0]["token"] == "test-token"
    assert calls[0]["frames"] == [b"\xaa\xbb\xcc", b"\xdd\xee\xff"]


@pytest.mark.asyncio
async def test_device_driven_listen_disabled_when_no_hook(manager):
    """Without STACKCHAN_AUDIO_HOOK_URL the gateway ignores inbound
    listen.start (no recording slot opens, no push fires)."""
    from stackchan_mcp.audio_stream import is_recording

    port = manager._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await _complete_handshake(ws)

        await ws.send(json.dumps({
            "type": "listen",
            "state": "start",
            "mode": "manual",
        }))
        # Give the gateway time to NOT do anything.
        await asyncio.sleep(0.2)
        assert not is_recording()


@pytest.mark.asyncio
async def test_device_driven_listen_cleanup_on_disconnect(manager_with_hook):
    """Disconnecting mid-capture drops the partial buffer rather than
    leaking it into the next connection's recording slot."""
    from stackchan_mcp.audio_stream import is_recording

    mgr, calls = manager_with_hook
    port = mgr._test_port

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await _complete_handshake(ws)
        await ws.send(json.dumps({
            "type": "listen",
            "state": "start",
            "mode": "manual",
        }))
        for _ in range(20):
            await asyncio.sleep(0.05)
            if is_recording():
                break
        assert is_recording()
        await ws.send(b"\x11\x22\x33")
        await asyncio.sleep(0.05)
        # Drop the connection without sending listen.stop.

    # Give the server-side handler's finally clause time to run.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if not is_recording():
            break
    assert not is_recording(), "recording slot was leaked across connections"
    # No push should have fired for the aborted capture.
    assert calls == []

