"""Tests for stdio MCP server tool definitions."""

import json
from pathlib import Path

import pytest
from mcp.types import CallToolRequest, ListToolsRequest

from stackchan_mcp.notify_config import DEFAULT_MESSAGE_TEMPLATES, NotifyConfig
import stackchan_mcp.stdio_server as stdio_server
from stackchan_mcp.stdio_server import (
    CHANNEL_CAPABILITY,
    CHANNEL_NOTIFICATION_METHOD,
    STACKCHAN_CHANNEL_INSTRUCTIONS,
    STACKCHAN_EVENT_INSTRUCTIONS,
    STACKCHAN_EVENT_METHOD,
    STACKCHAN_JSONL_INSTRUCTIONS,
    SPEED_DESCRIPTION,
    _build_experimental_capabilities,
    _build_stackchan_event_instructions,
    _create_initialization_options,
    _resolve_speed_dps,
    create_server,
    notify_stackchan_event,
)
from stackchan_mcp.tts import get_registry


def test_create_server():
    """Server creation succeeds with correct name."""
    server = create_server()
    assert server is not None
    assert server.name == "stackchanmcp"


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
async def test_list_tools_includes_gateway_config_tools():
    """gateway_config_get/set are exposed with the expected schemas."""
    server = create_server()

    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )

    tools = {tool.name: tool for tool in result.root.tools}
    assert "gateway_config_get" in tools
    assert "gateway_config_set" in tools

    get_schema = tools["gateway_config_get"].inputSchema
    assert get_schema == {"type": "object", "properties": {}}
    assert "mDNS" in tools["gateway_config_get"].description
    assert "force_mode" in tools["gateway_config_get"].description

    set_tool = tools["gateway_config_set"]
    set_schema = set_tool.inputSchema
    assert set(set_schema["properties"]) == {"url", "fallback_url", "token"}
    assert "required" not in set_schema
    assert set_schema["properties"]["url"]["type"] == "string"
    assert set_schema["properties"]["fallback_url"]["type"] == "string"
    assert set_schema["properties"]["token"]["type"] == "string"
    assert "empty string clears" in set_tool.description
    assert "next reconnect" in set_tool.description


@pytest.mark.asyncio
async def test_gateway_config_get_relays_to_esp32(monkeypatch):
    """gateway_config_get maps to self.gateway_config.get."""
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
                            {
                                "url": "",
                                "fallback_url": "wss://relay.example/",
                                "token_set": True,
                                "force_mode": False,
                                "discovery_enabled": True,
                            }
                        ),
                    }
                ],
            }, None

    class FakeGateway:
        esp32 = FakeESP32()

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "gateway_config_get", "arguments": {}},
        )
    )

    assert calls == [("self.gateway_config.get", {})]
    assert json.loads(result.root.content[0].text)["discovery_enabled"] is True


@pytest.mark.asyncio
async def test_gateway_config_set_relays_optional_strings(monkeypatch):
    """gateway_config_set forwards provided fields, including empty strings."""
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
                            {
                                "ok": True,
                                "updated_keys": ["url", "fallback_url"],
                                "url": "",
                                "fallback_url": "wss://relay.example/",
                                "token_set": False,
                                "discovery_enabled": True,
                            }
                        ),
                    }
                ],
            }, None

    class FakeGateway:
        esp32 = FakeESP32()

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    server = create_server()

    arguments = {"url": "", "fallback_url": "wss://relay.example/"}
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "gateway_config_set", "arguments": arguments},
        )
    )

    assert calls == [("self.gateway_config.set", arguments)]
    payload = json.loads(result.root.content[0].text)
    assert payload["ok"] is True
    assert payload["url"] == ""


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


_PORT_B_WS2812_TOOL_NAMES = (
    "port_b_ws2812_init",
    "port_b_ws2812_set_pixel",
    "port_b_ws2812_set_strip",
    "port_b_ws2812_refresh",
    "port_b_ws2812_clear",
)


@pytest.mark.asyncio
async def test_list_tools_includes_port_b_ws2812_tools_with_schemas():
    """Port B WS2812 wrappers are exposed with LLM-facing schemas."""
    server = create_server()

    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )

    tools_by_name = {tool.name: tool for tool in result.root.tools}
    for tool_name in _PORT_B_WS2812_TOOL_NAMES:
        assert tool_name in tools_by_name, f"{tool_name} tool should be registered"
        description = tools_by_name[tool_name].description
        assert "Port B" in description
        assert "GPIO 9" in description
        assert "3.3 V CMOS data" in description
        assert "level shifter" in description

    init_schema = tools_by_name["port_b_ws2812_init"].inputSchema
    assert init_schema["properties"]["led_count"] == {
        "type": "integer",
        "description": "Number of LEDs in the strip (1..256).",
        "minimum": 1,
        "maximum": 256,
    }
    assert init_schema["required"] == ["led_count"]

    pixel_schema = tools_by_name["port_b_ws2812_set_pixel"].inputSchema
    assert pixel_schema["properties"]["index"]["minimum"] == 0
    assert pixel_schema["properties"]["index"]["maximum"] == 255
    for channel in ("r", "g", "b"):
        assert pixel_schema["properties"][channel]["minimum"] == 0
        assert pixel_schema["properties"][channel]["maximum"] == 255
    assert pixel_schema["properties"]["refresh"] == {
        "type": "boolean",
        "description": "True to latch the update immediately.",
        "default": False,
    }
    assert pixel_schema["required"] == ["index", "r", "g", "b"]

    strip_schema = tools_by_name["port_b_ws2812_set_strip"].inputSchema
    colors_schema = strip_schema["properties"]["colors"]
    assert colors_schema["type"] == "array"
    assert colors_schema["minItems"] == 1
    assert colors_schema["maxItems"] == 256
    assert colors_schema["items"]["type"] == "array"
    assert colors_schema["items"]["minItems"] == 3
    assert colors_schema["items"]["maxItems"] == 3
    assert colors_schema["items"]["items"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 255,
    }
    assert strip_schema["required"] == ["colors"]

    for tool_name in ("port_b_ws2812_refresh", "port_b_ws2812_clear"):
        assert tools_by_name[tool_name].inputSchema == {
            "type": "object",
            "properties": {},
        }


def _make_port_b_ws2812_fake_gateway(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class FakeESP32:
        device_connected = True

        async def call_tool(self, tool_name, arguments):
            calls.append((tool_name, arguments))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"ok": True}),
                    }
                ],
            }, None

    class FakeGateway:
        esp32 = FakeESP32()

    import stackchan_mcp.stdio_server as stdio_server

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gateway_name", "request_args", "firmware_name", "firmware_args"),
    [
        (
            "port_b_ws2812_init",
            {"led_count": 18},
            "self.port_b.ws2812.init",
            {"led_count": 18},
        ),
        (
            "port_b_ws2812_set_pixel",
            {"index": 2, "r": 10, "g": 20, "b": 30, "refresh": True},
            "self.port_b.ws2812.set_pixel",
            {"index": 2, "r": 10, "g": 20, "b": 30, "refresh": True},
        ),
        (
            "port_b_ws2812_refresh",
            {},
            "self.port_b.ws2812.refresh",
            {},
        ),
        (
            "port_b_ws2812_clear",
            {},
            "self.port_b.ws2812.clear",
            {},
        ),
    ],
)
async def test_port_b_ws2812_tools_relay_to_firmware(
    monkeypatch,
    gateway_name,
    request_args,
    firmware_name,
    firmware_args,
):
    calls = _make_port_b_ws2812_fake_gateway(monkeypatch)
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": gateway_name, "arguments": request_args},
        )
    )

    assert calls == [(firmware_name, firmware_args)]
    assert json.loads(result.root.content[0].text) == {"ok": True}


@pytest.mark.asyncio
async def test_port_b_ws2812_set_strip_relays_colors_as_json_string(monkeypatch):
    calls = _make_port_b_ws2812_fake_gateway(monkeypatch)
    server = create_server()
    colors = [[32, 0, 0], [0, 32, 0], [0, 0, 32]]

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={
                "name": "port_b_ws2812_set_strip",
                "arguments": {"colors": colors},
            },
        )
    )

    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "self.port_b.ws2812.set_strip"
    assert set(arguments.keys()) == {"colors"}
    assert json.loads(arguments["colors"]) == colors
    assert json.loads(result.root.content[0].text) == {"ok": True}


# ---------------------------------------------------------------------------
# move_head — Issue #109: schema + handler enforce the M5Stack-recommended
# pitch operating range (5..85). pitch=0 motion-starts have been observed on
# device to trigger the SCS0009 bus hang tracked in Issue #100, and the
# firmware-side `set_head_angles` tool remains the documented escape hatch
# for callers that need the wider firmware hard clamp (0..88).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_move_head_declares_recommended_pitch_range():
    """move_head schema mirrors M5Stack-recommended 5..85 / yaw -90..90."""
    server = create_server()

    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )

    tool = next((t for t in result.root.tools if t.name == "move_head"), None)
    assert tool is not None, "move_head tool should be registered"

    pitch_schema = tool.inputSchema["properties"]["pitch"]
    assert pitch_schema["minimum"] == 5
    assert pitch_schema["maximum"] == 85

    yaw_schema = tool.inputSchema["properties"]["yaw"]
    assert yaw_schema["minimum"] == -90
    assert yaw_schema["maximum"] == 90

    # The description should mention the escape-hatch tool name so an LLM
    # reading it can pick the right alternative for permissive use cases.
    assert "set_head_angles" in tool.description

    speed_schema = tool.inputSchema["properties"]["speed"]
    assert speed_schema["oneOf"] == [
        {"enum": ["low", "mid", "high"]},
        {"type": "integer", "minimum": 1, "maximum": 10000},
    ]
    assert speed_schema["description"] == SPEED_DESCRIPTION


@pytest.mark.parametrize(
    ("speed", "expected_dps"),
    [
        ("low", 30),
        ("mid", 120),
        ("high", 240),
        (None, None),
        (200, 200),
        (1, 1),
        (10000, 10000),
    ],
)
def test_resolve_speed_dps_valid(speed, expected_dps):
    assert _resolve_speed_dps(speed) == expected_dps


@pytest.mark.parametrize(
    "bad_speed",
    ["fast", "slow", "", 0, -1, 10001, True, False, 1.5, [120], {}],
)
def test_resolve_speed_dps_invalid(bad_speed):
    with pytest.raises((ValueError, TypeError)):
        _resolve_speed_dps(bad_speed)


def _make_fake_gateway(monkeypatch):
    """Helper: wire a FakeESP32/FakeGateway into the stdio_server module.

    Returns the ``calls`` list that records each ESP32 call. The handler
    treats this device as connected. Used by the move_head handler tests.
    """
    calls: list[tuple[str, dict]] = []

    class FakeESP32:
        device_connected = True

        async def call_tool(self, tool_name, arguments):
            calls.append((tool_name, arguments))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"yaw": arguments.get("yaw", 0), "pitch": arguments.get("pitch", 0)}
                        ),
                    }
                ],
            }, None

    class FakeGateway:
        esp32 = FakeESP32()

    import stackchan_mcp.stdio_server as stdio_server

    monkeypatch.setattr(stdio_server, "get_gateway", lambda: FakeGateway())
    return calls


_MISSING = object()


def _move_head_request(yaw, pitch, speed=_MISSING):
    arguments = {"yaw": yaw, "pitch": pitch}
    if speed is not _MISSING:
        arguments["speed"] = speed
    return CallToolRequest(
        method="tools/call",
        params={"name": "move_head", "arguments": arguments},
    )


def _assert_rejected_without_dispatch(result, calls):
    """Common shape: out-of-range request is refused and no ESP32 call fires.

    Two refusal paths coexist:
    - mcp SDK server-side JSON Schema validation rejects the request before
      the handler runs (current behaviour observed with mcp>=1.0). The
      response text is a human-readable validation message rather than the
      handler's structured JSON error.
    - The handler's belt-and-suspenders validation in stdio_server.py
      returns a clean ``{"error": "..."}`` JSON for SDK versions or future
      configurations that may not enforce the schema bounds.

    Either path is acceptable. What matters for hardware safety is that
    ``self.robot.set_head_angles`` is never called.
    """
    assert calls == [], (
        "Out-of-range move_head must not dispatch a motion call. "
        f"Got calls={calls}, response text={result.root.content[0].text!r}"
    )
    response_text = result.root.content[0].text
    # The response should signal an error in some shape. Either the handler
    # JSON ({"error": "..."}) or the SDK validation prose mentions one of
    # these keywords.
    lower = response_text.lower()
    assert any(
        keyword in lower
        for keyword in ("error", "invalid", "minimum", "maximum", "type")
    ), f"Expected an error signal in {response_text!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("pitch", [0, 4, -1, -30])
async def test_move_head_rejects_pitch_below_recommended(monkeypatch, pitch):
    """pitch values below the M5Stack-recommended 5° floor are refused."""
    calls = _make_fake_gateway(monkeypatch)
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        _move_head_request(yaw=0, pitch=pitch)
    )

    _assert_rejected_without_dispatch(result, calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("pitch", [86, 90, 88, 200])
async def test_move_head_rejects_pitch_above_recommended(monkeypatch, pitch):
    """pitch values above the M5Stack-recommended 85° ceiling are refused."""
    calls = _make_fake_gateway(monkeypatch)
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        _move_head_request(yaw=0, pitch=pitch)
    )

    _assert_rejected_without_dispatch(result, calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("yaw", [-91, 91, 200, -1000])
async def test_move_head_rejects_yaw_out_of_range(monkeypatch, yaw):
    """yaw values outside -90..+90 are refused."""
    calls = _make_fake_gateway(monkeypatch)
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        _move_head_request(yaw=yaw, pitch=45)
    )

    _assert_rejected_without_dispatch(result, calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("pitch", [5, 45, 85])
async def test_move_head_accepts_pitch_inside_recommended(monkeypatch, pitch):
    """Boundary and mid-range pitch values are accepted and relayed."""
    calls = _make_fake_gateway(monkeypatch)
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        _move_head_request(yaw=0, pitch=pitch)
    )

    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "self.robot.set_head_angles"
    assert arguments == {"yaw": 0, "pitch": pitch}

    payload = json.loads(result.root.content[0].text)
    assert "error" not in payload


@pytest.mark.asyncio
async def test_move_head_speed_mid_forwards_speed_dps(monkeypatch):
    calls = _make_fake_gateway(monkeypatch)
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        _move_head_request(yaw=10, pitch=45, speed="mid")
    )

    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "self.robot.set_head_angles"
    assert arguments == {"yaw": 10, "pitch": 45, "speed_dps": 120}

    payload = json.loads(result.root.content[0].text)
    assert "error" not in payload


@pytest.mark.asyncio
async def test_move_head_without_speed_omits_speed_dps(monkeypatch):
    calls = _make_fake_gateway(monkeypatch)
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        _move_head_request(yaw=10, pitch=45)
    )

    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "self.robot.set_head_angles"
    assert arguments == {"yaw": 10, "pitch": 45}

    payload = json.loads(result.root.content[0].text)
    assert "error" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("pitch", [None, "45", 5.5])
async def test_move_head_rejects_non_integer_pitch(monkeypatch, pitch):
    """Non-int pitch values are refused before reaching the device."""
    calls = _make_fake_gateway(monkeypatch)
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        _move_head_request(yaw=0, pitch=pitch)
    )

    _assert_rejected_without_dispatch(result, calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("pitch", [True, False])
async def test_move_head_rejects_boolean_pitch(monkeypatch, pitch):
    """bool is an int subclass in Python; must still be refused for pitch."""
    calls = _make_fake_gateway(monkeypatch)
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        _move_head_request(yaw=0, pitch=pitch)
    )

    _assert_rejected_without_dispatch(result, calls)


# ---------------------------------------------------------------------------
# Stack-chan event notification config: capabilities, instructions, allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "legacy",
        "channels",
        "jsonl",
        "expected_capabilities",
        "expected_instructions",
    ),
    [
        (
            False,
            True,
            False,
            {CHANNEL_CAPABILITY: {}},
            STACKCHAN_CHANNEL_INSTRUCTIONS,
        ),
        (
            True,
            False,
            False,
            {STACKCHAN_EVENT_METHOD: {}},
            STACKCHAN_EVENT_INSTRUCTIONS,
        ),
        (
            False,
            False,
            True,
            {},
            STACKCHAN_JSONL_INSTRUCTIONS,
        ),
        (
            True,
            True,
            False,
            {STACKCHAN_EVENT_METHOD: {}, CHANNEL_CAPABILITY: {}},
            STACKCHAN_CHANNEL_INSTRUCTIONS + "\n\n" + STACKCHAN_EVENT_INSTRUCTIONS,
        ),
        (
            False,
            False,
            False,
            {},
            None,
        ),
    ],
)
def test_stackchan_event_capabilities_and_instructions_follow_notify_config(
    legacy,
    channels,
    jsonl,
    expected_capabilities,
    expected_instructions,
):
    config = _notify_config(legacy=legacy, channels=channels, jsonl=jsonl)
    server = create_server()
    options = _create_initialization_options(server, notify_config=config)

    assert _build_experimental_capabilities(config) == expected_capabilities
    assert options.capabilities.experimental == expected_capabilities
    assert _build_stackchan_event_instructions(config) == expected_instructions
    assert options.instructions == expected_instructions


def test_create_initialization_options_requires_notify_config():
    server = create_server(notify_config=_notify_config())

    with pytest.raises(TypeError):
        _create_initialization_options(server)


def test_create_initialization_options_uses_explicit_notify_config(monkeypatch):
    all_off_config = _notify_config(legacy=False, channels=False, jsonl=False)
    channels_config = _notify_config(legacy=False, channels=True, jsonl=False)

    load_calls = []

    def load_all_off_config():
        load_calls.append("load")
        return all_off_config

    monkeypatch.setattr(stdio_server, "load_notify_config", load_all_off_config)
    server = create_server(notify_config=all_off_config)

    all_off_options = _create_initialization_options(server, all_off_config)
    channels_options = _create_initialization_options(server, channels_config)

    assert load_calls == []
    assert all_off_options.capabilities.experimental == {}
    assert all_off_options.instructions is None
    assert channels_options.capabilities.experimental == {CHANNEL_CAPABILITY: {}}
    assert channels_options.instructions == STACKCHAN_CHANNEL_INSTRUCTIONS


@pytest.mark.asyncio
async def test_notify_stackchan_event_accepts_channel_method(monkeypatch):
    session = _FakeNotificationSession()
    monkeypatch.setattr("stackchan_mcp.stdio_server._active_session", session)
    monkeypatch.setattr("stackchan_mcp.stdio_server._active_sessions", {})

    params = {"content": "(head pat)", "meta": {"action": "head_pat"}}
    await notify_stackchan_event(CHANNEL_NOTIFICATION_METHOD, params)

    assert session.notifications == [
        {"method": CHANNEL_NOTIFICATION_METHOD, "params": params}
    ]


@pytest.mark.asyncio
async def test_notify_stackchan_event_rejects_unsupported_method(
    monkeypatch,
    caplog,
):
    session = _FakeNotificationSession()
    monkeypatch.setattr("stackchan_mcp.stdio_server._active_session", session)
    monkeypatch.setattr("stackchan_mcp.stdio_server._active_sessions", {})

    with caplog.at_level("WARNING"):
        await notify_stackchan_event("notifications/other", {"ok": True})

    assert session.notifications == []
    assert "Unsupported stackchan event notification method" in caplog.text


def _notify_config(
    *,
    legacy: bool = False,
    channels: bool = False,
    jsonl: bool = False,
) -> NotifyConfig:
    return NotifyConfig(
        legacy_event_enabled=legacy,
        channels_enabled=channels,
        jsonl_enabled=jsonl,
        jsonl_path=Path("/tmp/stackchan-events-test.jsonl"),
        messages=dict(DEFAULT_MESSAGE_TEMPLATES),
    )


class _FakeNotificationSession:
    def __init__(self):
        self.notifications = []

    async def send_notification(self, notification):
        self.notifications.append(
            notification.model_dump(
                by_alias=True,
                mode="json",
                exclude_none=True,
            )
        )
