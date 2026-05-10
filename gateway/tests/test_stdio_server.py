"""Tests for stdio MCP server tool definitions."""

import json

import pytest
from mcp.types import CallToolRequest, ListToolsRequest

from stackchan_mcp.stdio_server import create_server
from stackchan_mcp.tts import get_registry


def test_create_server():
    """Server creation succeeds with correct name."""
    server = create_server()
    assert server is not None
    assert server.name == "stackchan-mcp"


@pytest.mark.asyncio
async def test_list_tools_includes_get_head_angles():
    """get_head_angles is exposed to MCP clients."""
    server = create_server()

    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )

    tool_names = [tool.name for tool in result.root.tools]
    assert "get_head_angles" in tool_names


@pytest.mark.asyncio
async def test_get_head_angles_relays_to_esp32(monkeypatch):
    """get_head_angles maps to the ESP32 self.robot.get_head_angles tool."""
    calls = []

    class FakeESP32:
        device_connected = True

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"yaw": 12, "pitch": -3}),
                    }
                ],
            }, None

    class FakeGateway:
        esp32 = FakeESP32()

    import stackchan_mcp.stdio_server as stdio_server

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "get_head_angles", "arguments": {}},
        )
    )

    assert calls == [("self.robot.get_head_angles", {})]
    assert json.loads(result.root.content[0].text) == {"yaw": 12, "pitch": -3}


@pytest.mark.asyncio
async def test_list_tools_includes_set_mouth_sequence():
    """set_mouth_sequence is exposed to MCP clients with an array schema."""
    server = create_server()

    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )

    tool = next((t for t in result.root.tools if t.name == "set_mouth_sequence"), None)
    assert tool is not None, "set_mouth_sequence tool should be registered"

    schema = tool.inputSchema
    assert schema["properties"]["steps"]["type"] == "array"
    assert schema["properties"]["steps"]["minItems"] == 1
    assert schema["properties"]["steps"]["maxItems"] == 256

    item_schema = schema["properties"]["steps"]["items"]
    assert item_schema["properties"]["shape"]["enum"] == [
        "closed",
        "half",
        "open",
        "e",
        "u",
    ]
    assert item_schema["properties"]["duration_ms"]["minimum"] == 10
    assert item_schema["properties"]["duration_ms"]["maximum"] == 10000
    assert set(item_schema["required"]) == {"shape", "duration_ms"}


@pytest.mark.asyncio
async def test_list_tools_includes_say():
    """say is exposed to MCP clients with text required."""
    server = create_server()

    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )

    tool = next((t for t in result.root.tools if t.name == "say"), None)
    assert tool is not None, "say tool should be registered"

    schema = tool.inputSchema
    assert schema["properties"]["text"]["type"] == "string"
    assert "voice" in schema["properties"]
    assert "speaker_id" in schema["properties"]
    assert "reference_audio" in schema["properties"]
    assert schema["required"] == ["text"]


@pytest.mark.asyncio
async def test_say_unknown_voice_returns_clean_error():
    """say with an unknown voice returns a clean error JSON, not a traceback.

    This invariant holds regardless of which engines happen to be
    registered at the default level — the orchestrator must surface a
    NotImplementedError to the caller as MCP error JSON.
    """
    server = create_server()
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={
                "name": "say",
                "arguments": {"text": "hello", "voice": "nonexistent_engine"},
            },
        )
    )

    payload = json.loads(result.root.content[0].text)
    assert "error" in payload
    assert "nonexistent_engine" in payload["error"]


@pytest.mark.asyncio
async def test_say_rejects_empty_text_with_clean_error():
    """say with empty text returns a clean ValueError-shaped error."""
    server = create_server()
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "say", "arguments": {"text": ""}},
        )
    )

    payload = json.loads(result.root.content[0].text)
    assert "error" in payload
    assert "text" in payload["error"]


@pytest.mark.asyncio
async def test_say_returns_clean_error_when_device_disconnected(monkeypatch):
    """say without a connected ESP32 surfaces a clean MCP error JSON.

    Validation passes (text + registered engine), the orchestrator
    needs to push frames somewhere, and the device gate fires. The
    handler must turn that into ``{"error": "..."}`` rather than
    leaking a stack trace through the MCP transport.
    """

    class FakeESP32:
        device_connected = False

        def get_status(self):
            return {"connected": False}

    class FakeGateway:
        esp32 = FakeESP32()

    import stackchan_mcp.stdio_server as stdio_server

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "say", "arguments": {"text": "hello"}},
        )
    )

    payload = json.loads(result.root.content[0].text)
    assert "error" in payload
    msg = payload["error"].lower()
    assert "esp32" in msg or "device" in msg


@pytest.mark.asyncio
async def test_default_registry_includes_voicevox():
    """The default registry registers VOICEVOX at import time.

    PR2 of Issue #70 wires VOICEVOX in via ``tts/__init__.py`` so users
    who install the ``[tts]`` extra can call ``say`` without needing
    to register an engine themselves. This test pins that contract.
    """
    assert "voicevox" in get_registry().names()


# ---------------------------------------------------------------------------
# say handler regression tests — degraded VOICEVOX / mid-stream disconnect
# must produce error JSON, not stack traces. Codex adversarial review
# flagged that this contract was previously only verified at the
# orchestrator level; these tests close the loop through create_server().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_say_returns_error_json_when_voicevox_returns_5xx(monkeypatch):
    """A 503 from VOICEVOX surfaces as ``{"error": ...}``, not a traceback."""
    httpx = pytest.importorskip("httpx")

    from stackchan_mcp.tts import EngineRegistry, TTSEngine
    import stackchan_mcp.tts.orchestrator as orchestrator
    import stackchan_mcp.stdio_server as stdio_server

    class _HttpFailEngine(TTSEngine):
        name = "voicevox"

        async def synthesize(self, text, **opts):
            request = httpx.Request("POST", "http://test/audio_query")
            response = httpx.Response(503, request=request, text="overloaded")
            raise httpx.HTTPStatusError(
                "503", request=request, response=response
            )

    reg = EngineRegistry()
    reg.register(_HttpFailEngine())

    class FakeESP32:
        device_connected = True

        def get_status(self):
            return {"connected": True}

        async def send_audio_frame(self, frame):
            raise AssertionError("synthesise should have failed before push")

        async def send_tts_state(self, state):  # noqa: ARG002 - test stub
            return None

    class FakeGateway:
        esp32 = FakeESP32()

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(orchestrator, "get_registry", lambda: reg)

    server = create_server()
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "say", "arguments": {"text": "hello"}},
        )
    )
    payload = json.loads(result.root.content[0].text)
    assert "error" in payload
    assert "voicevox" in payload["error"].lower()


@pytest.mark.asyncio
async def test_say_returns_error_json_when_device_disconnects_mid_stream(
    monkeypatch,
):
    """A mid-stream disconnect surfaces as ``{"error": ...}``."""
    from stackchan_mcp.tts import EngineRegistry, TTSEngine
    import stackchan_mcp.tts.orchestrator as orchestrator
    import stackchan_mcp.stdio_server as stdio_server

    pcm = b"\x01\x00" * 1440  # ~ 1.5 frames

    class _PCMEngine(TTSEngine):
        name = "voicevox"

        async def synthesize(self, text, **opts):
            return pcm

    reg = EngineRegistry()
    reg.register(_PCMEngine())

    def fake_encode(_pcm, **kwargs):
        return iter([b"opus_a", b"opus_b"])

    class FailingESP32:
        device_connected = True

        def __init__(self):
            self.frames: list[bytes] = []

        def get_status(self):
            return {"connected": True}

        async def send_audio_frame(self, frame):
            if self.frames:
                raise ConnectionError("simulated disconnect")
            self.frames.append(frame)

        async def send_tts_state(self, state):  # noqa: ARG002 - test stub
            return None

    class FakeGateway:
        esp32 = FailingESP32()

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    monkeypatch.setattr(orchestrator, "get_registry", lambda: reg)
    monkeypatch.setattr(orchestrator, "encode_opus_frames", fake_encode)

    server = create_server()
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "say", "arguments": {"text": "hello"}},
        )
    )
    payload = json.loads(result.root.content[0].text)
    assert "error" in payload
    msg = payload["error"].lower()
    assert "disconnect" in msg or "frame" in msg


@pytest.mark.asyncio
async def test_set_mouth_sequence_relays_steps_as_json_string(monkeypatch):
    """set_mouth_sequence serialises steps to JSON for the firmware.

    The ESP32 MCP Property type system only supports string/integer/boolean,
    so the gateway flattens the steps array to a JSON string under
    `steps_json` before forwarding to self.display.set_mouth_sequence.
    """
    calls = []

    class FakeESP32:
        device_connected = True

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"ok": True, "queued_steps": 2, "estimated_duration_ms": 160}
                        ),
                    }
                ],
            }, None

    class FakeGateway:
        esp32 = FakeESP32()

    import stackchan_mcp.stdio_server as stdio_server

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    server = create_server()

    steps = [
        {"shape": "open", "duration_ms": 80},
        {"shape": "closed", "duration_ms": 80},
    ]
    await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "set_mouth_sequence", "arguments": {"steps": steps}},
        )
    )

    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "self.display.set_mouth_sequence"
    assert set(arguments.keys()) == {"steps_json"}
    assert json.loads(arguments["steps_json"]) == steps
