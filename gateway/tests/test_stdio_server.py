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
