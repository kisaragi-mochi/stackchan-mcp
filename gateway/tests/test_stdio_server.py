"""Tests for stdio MCP server tool definitions."""

import json

import pytest
from mcp.types import CallToolRequest, ListToolsRequest

from stackchan_mcp.stdio_server import create_server


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
