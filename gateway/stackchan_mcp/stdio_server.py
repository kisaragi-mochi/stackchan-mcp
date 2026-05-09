"""stdio MCP server for MCP client.

Exposes stackchan tools via the MCP Python SDK's stdio transport.
Each tool call is relayed to the connected ESP32 device.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .gateway import get_gateway

logger = logging.getLogger(__name__)


def create_server() -> Server:
    """Create and configure the MCP server with tool handlers."""
    server = Server("stackchan-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available stackchan tools.

        Tools prefixed with ESP32 names (self.*) are relayed to the device.
        get_status is handled locally by the gateway.
        """
        return [
            Tool(
                name="get_status",
                description=(
                    "Get the gateway's connection status: whether ESP32 is connected, "
                    "device info, and list of available device tools."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="get_device_info",
                description=(
                    "Get real-time device information from ESP32: "
                    "battery level, speaker volume, screen brightness, network status, etc."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="take_photo",
                description=(
                    "Take a photo with the robot's camera and ask a question about it. "
                    "The device captures an image and returns an AI-generated description."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Question to ask about the photo (e.g. 'What do you see?')",
                        },
                    },
                    "required": ["question"],
                },
            ),
            Tool(
                name="set_volume",
                description="Set the speaker volume (0-100).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "volume": {
                            "type": "integer",
                            "description": "Volume level (0-100)",
                        },
                    },
                    "required": ["volume"],
                },
            ),
            Tool(
                name="set_brightness",
                description="Set the screen brightness (0-100).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "brightness": {
                            "type": "integer",
                            "description": "Brightness level (0-100)",
                        },
                    },
                    "required": ["brightness"],
                },
            ),
            Tool(
                name="move_head",
                description=(
                    "Move the robot's head to the specified angles. "
                    "yaw: horizontal (-90 to 90), pitch: vertical (-30 to 30)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "yaw": {
                            "type": "integer",
                            "description": "Horizontal angle in degrees (-90 to 90)",
                        },
                        "pitch": {
                            "type": "integer",
                            "description": "Vertical angle in degrees (-30 to 30)",
                        },
                    },
                    "required": ["yaw", "pitch"],
                },
            ),
            Tool(
                name="get_head_angles",
                description="Get the robot's current head angles: yaw and pitch in degrees.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="gpio_test",
                description="Test GPIO6 pin by toggling HIGH/LOW 5 times. Check if servo reacts.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="uart_diag",
                description="Send raw servo bytes via UART and report write result.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="check_vm_en",
                description=(
                    "Diagnostic: read PY32 REG_GPIO_O_L and report whether VM EN "
                    "(pin 0 = servo power) is currently HIGH. Returns "
                    "{io_expander_present, i2c_read_ok, raw, vm_en_high}."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="set_avatar",
                description=(
                    "Switch the avatar face shown on the LCD. "
                    "Choose one of the supported faces; this is the robot's "
                    "actual visible expression, not just a label. "
                    "Pass 'off' to hide the avatar and disable blink, exposing the "
                    "underlying xiaozhi-esp32 screens (WiFi config UI, OTA, settings); "
                    "any other face brings the avatar back and restores blink."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "face": {
                            "type": "string",
                            "enum": [
                                "idle",
                                "happy",
                                "thinking",
                                "sad",
                                "surprised",
                                "embarrassed",
                                "off",
                            ],
                            "description": (
                                "One of: idle, happy, thinking, sad, surprised, "
                                "embarrassed, off."
                            ),
                        },
                    },
                    "required": ["face"],
                },
            ),
            Tool(
                name="set_mouth",
                description=(
                    "Set the avatar mouth shape for lip-sync. "
                    "The shape is held until the next set_avatar / set_mouth call, "
                    "or until an autonomous blink restores the resting face. "
                    "Calling this while a set_mouth_sequence is in flight "
                    "interrupts the sequence."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mouth": {
                            "type": "string",
                            "enum": ["closed", "half", "open", "e", "u"],
                            "description": "One of: closed, half, open, e, u.",
                        },
                    },
                    "required": ["mouth"],
                },
            ),
            Tool(
                name="set_mouth_sequence",
                description=(
                    "Queue a lip-sync sequence and play it on the device. "
                    "Each step holds 'shape' for 'duration_ms' before "
                    "advancing. The firmware walks the queue locally so "
                    "there is no per-step network RTT (use this instead of "
                    "issuing many set_mouth calls back-to-back from a TTS "
                    "loop). Returns immediately with the queued step count "
                    "and estimated total duration. Calling set_mouth, "
                    "set_avatar, or this tool again interrupts the in-flight "
                    "sequence and replaces it. Autonomous blink is paused "
                    "while a sequence is playing and resumed when it ends. "
                    "The final shape is held until the next "
                    "set_mouth / set_avatar call, or until an autonomous "
                    "blink restores the resting face — this is the same "
                    "Phase 2 trade-off that applies to set_mouth, since the "
                    "blink animation ends by repainting the full face. If "
                    "the final shape must persist visually, disable blink "
                    "with set_blink(false) before the sequence (or append a "
                    "closed step if you just want the mouth to close at "
                    "the end)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 256,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "shape": {
                                        "type": "string",
                                        "enum": ["closed", "half", "open", "e", "u"],
                                        "description": (
                                            "Mouth shape for this step. "
                                            "One of: closed, half, open, e, u."
                                        ),
                                    },
                                    "duration_ms": {
                                        "type": "integer",
                                        "minimum": 10,
                                        "maximum": 10000,
                                        "description": (
                                            "How long to hold this shape "
                                            "before advancing, in ms (10..10000)."
                                        ),
                                    },
                                },
                                "required": ["shape", "duration_ms"],
                            },
                            "description": (
                                "Ordered list of mouth shapes with hold "
                                "durations (1..256 steps)."
                            ),
                        },
                    },
                    "required": ["steps"],
                },
            ),
            Tool(
                name="set_blink",
                description=(
                    "Enable or disable autonomous eye blinking. "
                    "When enabled, the avatar blinks every 3-6 seconds at random."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "enabled": {
                            "type": "boolean",
                            "description": "True to start blinking, false to stop.",
                        },
                    },
                    "required": ["enabled"],
                },
            ),
            Tool(
                name="get_touch_state",
                description=(
                    "Read the head-touch (Si12T) sensor state and the most recent "
                    "gesture event (tap/stroke/idle). Returns per-zone booleans, "
                    "the raw output byte, and how long ago the last event fired."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        """Handle a tool call by relaying to ESP32."""
        arguments = arguments or {}
        gw = get_gateway()

        if name == "get_status":
            # get_status is handled locally — no ESP32 needed
            status = gw.esp32.get_status()
            return [TextContent(type="text", text=json.dumps(status, indent=2))]

        if not gw.esp32.device_connected:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "No ESP32 device connected. Please check the device."}),
                )
            ]

        # Map MCP client tool names to ESP32 MCP tool names (self.* prefix)
        tool_map: dict[str, tuple[str, dict[str, Any]]] = {
            "get_device_info": (
                "self.get_device_status",
                {},
            ),
            "take_photo": (
                "self.camera.take_photo",
                arguments,
            ),
            "set_volume": (
                "self.audio_speaker.set_volume",
                arguments,
            ),
            "set_brightness": (
                "self.screen.set_brightness",
                arguments,
            ),
            "move_head": (
                "self.robot.set_head_angles",
                arguments,
            ),
            "get_head_angles": (
                "self.robot.get_head_angles",
                {},
            ),
            "gpio_test": (
                "self.robot.gpio_test",
                {},
            ),
            "uart_diag": (
                "self.robot.uart_diag",
                {},
            ),
            "check_vm_en": (
                "self.robot.check_vm_en",
                {},
            ),
            "set_avatar": (
                "self.display.set_avatar",
                arguments,
            ),
            "set_mouth": (
                "self.display.set_mouth",
                arguments,
            ),
            # The MCP Property type system on ESP32 only supports
            # string/integer/boolean, so we serialise the steps array to
            # a JSON string here. The firmware decodes it via cJSON.
            "set_mouth_sequence": (
                "self.display.set_mouth_sequence",
                {"steps_json": json.dumps(arguments.get("steps", []))},
            ),
            "set_blink": (
                "self.display.set_blink",
                arguments,
            ),
            "get_touch_state": (
                "self.touch.get_touch_state",
                {},
            ),
        }

        if name not in tool_map:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}),
                )
            ]

        esp32_name, esp32_args = tool_map[name]
        result, error = await gw.esp32.call_tool(esp32_name, esp32_args)

        if error:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": error.get("message", str(error))}),
                )
            ]

        # result from ESP32 is MCP format: {"content": [...], "isError": bool}
        if isinstance(result, dict):
            content = result.get("content", [])
            if content and isinstance(content, list):
                # Pass through content items as text
                texts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        texts.append(item.get("text", ""))
                if texts:
                    return [TextContent(type="text", text="\n".join(texts))]

            # Fallback: dump entire result
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        return [TextContent(type="text", text=str(result))]

    return server


async def run_stdio_server() -> None:
    """Run the MCP server on stdio."""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        logger.info("stdio MCP server starting")
        await server.run(read_stream, write_stream, server.create_initialization_options())
