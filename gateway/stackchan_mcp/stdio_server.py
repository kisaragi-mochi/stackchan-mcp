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
from .stt import listen_and_transcribe
from .tts import synthesize_and_send

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
                    "Move the robot's head to safe, recommended angles. "
                    "yaw: horizontal (-90 to 90), pitch: vertical (5 to 85, "
                    "the M5Stack-recommended operating range). Out-of-range "
                    "requests are rejected at this MCP layer; for advanced "
                    "callers that need the firmware hard clamp (pitch 0..88), "
                    "use the firmware-side `set_head_angles` device tool, "
                    "which exposes a permissive schema and the authoritative "
                    "two-tier guard described in the README."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "yaw": {
                            "type": "integer",
                            "description": "Horizontal angle in degrees (-90 to 90)",
                            "minimum": -90,
                            "maximum": 90,
                        },
                        "pitch": {
                            "type": "integer",
                            "description": (
                                "Vertical angle in degrees (5 to 85, "
                                "M5Stack-recommended operating range). For the "
                                "wider firmware hard clamp (0..88), use the "
                                "`set_head_angles` device tool instead."
                            ),
                            "minimum": 5,
                            "maximum": 85,
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
                name="set_servo_torque",
                description=(
                    "Enable or disable SCS0009 servo torque on the yaw / "
                    "pitch axes independently. Disabling torque stops motor "
                    "current on that axis; the head holds via static "
                    "friction (no motion is commanded). On disable, the "
                    "firmware also cancels any in-flight MotionDriver "
                    "interpolation and marks the axis position unknown so "
                    "a subsequent same-target set_head_angles is re-"
                    "dispatched rather than no-op-optimized. Re-enabling "
                    "torque does NOT trigger a move; the next "
                    "set_head_angles or wobble call will. Diagnostic / "
                    "power-management primitive used to observe physical "
                    "head behavior under torque-off (Issue #163; auto "
                    "release on idle is Issue #152 Phase 4)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "yaw_enabled": {
                            "type": "boolean",
                            "description": (
                                "True to enable yaw axis torque, false to "
                                "disable."
                            ),
                        },
                        "pitch_enabled": {
                            "type": "boolean",
                            "description": (
                                "True to enable pitch axis torque, false "
                                "to disable."
                            ),
                        },
                    },
                    "required": ["yaw_enabled", "pitch_enabled"],
                },
            ),
            Tool(
                name="set_auto_torque_release",
                description=(
                    "Enable or disable firmware-side automatic SCS0009 "
                    "torque release after motion idle timeout. timeout_ms "
                    "is clamped by the firmware to 500..600000 ms. "
                    "Disabling this setting does not re-enable torque if "
                    "it is already released; the next set_head_angles, "
                    "wobble, or explicit set_servo_torque(true, true) call "
                    "re-engages torque."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "enabled": {
                            "type": "boolean",
                            "description": (
                                "True to enable idle auto-release, false "
                                "to disable it."
                            ),
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": (
                                "Idle timeout in milliseconds. Values "
                                "outside 500..600000 are clamped by the "
                                "firmware handler."
                            ),
                        },
                    },
                    "required": ["enabled", "timeout_ms"],
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
            Tool(
                name="set_led",
                description=(
                    "Set a single RGB LED on the StackChan base. There are 12 LEDs "
                    "arranged in two rows of 6 (index 0..11). Updates immediately."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "LED index (0..11)",
                            "minimum": 0,
                            "maximum": 11,
                        },
                        "r": {"type": "integer", "description": "Red 0..255", "minimum": 0, "maximum": 255},
                        "g": {"type": "integer", "description": "Green 0..255", "minimum": 0, "maximum": 255},
                        "b": {"type": "integer", "description": "Blue 0..255", "minimum": 0, "maximum": 255},
                    },
                    "required": ["index", "r", "g", "b"],
                },
            ),
            Tool(
                name="set_all_leds",
                description="Set all 12 RGB LEDs on the StackChan base to the same color. Updates immediately.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "r": {"type": "integer", "description": "Red 0..255", "minimum": 0, "maximum": 255},
                        "g": {"type": "integer", "description": "Green 0..255", "minimum": 0, "maximum": 255},
                        "b": {"type": "integer", "description": "Blue 0..255", "minimum": 0, "maximum": 255},
                    },
                    "required": ["r", "g", "b"],
                },
            ),
            Tool(
                name="set_leds",
                description=(
                    "Set multiple RGB LEDs in one shot. 'colors' is an array of "
                    "[r,g,b] triples starting at index 0 (e.g. [[255,0,0],[0,255,0]]). "
                    "Up to 12 entries; extras are ignored, missing entries keep their "
                    "previous color. Use this for animations / patterns to avoid 12x "
                    "I2C round-trips."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "colors": {
                            "type": "array",
                            "description": "Array of [r,g,b] triples, each 0..255",
                            "items": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 0, "maximum": 255},
                                "minItems": 3,
                                "maxItems": 3,
                            },
                            "minItems": 1,
                            "maxItems": 12,
                        },
                    },
                    "required": ["colors"],
                },
            ),
            Tool(
                name="clear_leds",
                description="Turn off all 12 RGB LEDs on the StackChan base.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="say",
                description=(
                    "Speak the given text on the device speaker via gateway-side "
                    "TTS (Phase 4, Issue #70). The gateway synthesises audio, "
                    "encodes it to Opus, and pushes frames over the existing "
                    "WebSocket — the device firmware does not change. Engine is "
                    "selectable via 'voice' (default 'voicevox'). "
                    "NOTE: this build ships the framework only; concrete engines "
                    "(VOICEVOX, Irodori) land in follow-up PRs and require the "
                    "matching optional extra (e.g. "
                    "'pip install stackchan-mcp[tts-voicevox]'). Calling this tool "
                    "before an engine is registered returns a clear error."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Text to speak. Must be non-empty.",
                        },
                        "voice": {
                            "type": "string",
                            "description": (
                                "Engine identifier (e.g. 'voicevox', 'irodori'). "
                                "Default 'voicevox'."
                            ),
                            "default": "voicevox",
                        },
                        "speaker_id": {
                            "type": "integer",
                            "description": (
                                "Engine-specific speaker identifier "
                                "(e.g. a VOICEVOX speaker ID)."
                            ),
                        },
                        "reference_audio": {
                            "type": "string",
                            "description": (
                                "Path to a reference audio file used by "
                                "voice-cloning engines (e.g. Irodori). "
                                "Ignored by engines that do not support it."
                            ),
                        },
                    },
                    "required": ["text"],
                },
            ),
            Tool(
                name="listen",
                description=(
                    "Capture a short utterance from the device microphone and "
                    "transcribe it via a gateway-side STT engine (Phase 4, "
                    "Issue #91). The gateway sends a 'listen' notification "
                    "over the existing WebSocket to put the device firmware "
                    "into listening mode, buffers the Opus frames the device "
                    "streams up during the capture window, then decodes and "
                    "transcribes them once the window closes. Requires a "
                    "minimal firmware change to handle the inbound 'listen' "
                    "wire type (paired with this gateway release). Engine is "
                    "selectable via 'engine' (default 'faster-whisper', local). "
                    "Optional 'motion' feedback can switch the avatar to "
                    "'thinking' during capture ('face-only') or tilt the head "
                    "up while preserving yaw ('look-up'). "
                    "Install the relevant extra "
                    "('pip install stackchan-mcp[stt-faster-whisper]' or "
                    "'stt-openai'); calling this tool before an engine is "
                    "registered returns a clear error."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "duration_ms": {
                            "type": "integer",
                            "description": (
                                "Capture window in milliseconds. Clamped to "
                                "[100, 30000]."
                            ),
                            "default": 5000,
                            "minimum": 100,
                            "maximum": 30000,
                        },
                        "engine": {
                            "type": "string",
                            "description": (
                                "Engine identifier (e.g. 'faster-whisper', "
                                "'openai-whisper'). Default 'faster-whisper'."
                            ),
                            "default": "faster-whisper",
                        },
                        "language": {
                            "type": "string",
                            "description": (
                                "ISO 639-1 language code (e.g. 'ja'). Pass "
                                "an empty string or omit for autodetect."
                            ),
                            "default": "ja",
                        },
                        "model": {
                            "type": "string",
                            "description": (
                                "Engine-specific model identifier (e.g. "
                                "'base' / 'small' / 'medium' for faster-"
                                "whisper, 'whisper-1' for OpenAI). Engines "
                                "fall back to their default when omitted."
                            ),
                        },
                        "motion": {
                            "type": "string",
                            "enum": ["none", "face-only", "look-up"],
                            "description": (
                                "Optional visible feedback during capture. "
                                "'none' preserves the previous behaviour. "
                                "'face-only' shows the thinking avatar during "
                                "capture and restores idle at the end. "
                                "'look-up' preserves yaw, tilts pitch to "
                                "look_up_pitch, and holds the pose on success."
                            ),
                            "default": "none",
                        },
                        "look_up_pitch": {
                            "type": "number",
                            "description": (
                                "Pitch angle for motion='look-up'. Must be "
                                "between 5 and 85 degrees."
                            ),
                            "default": 50.0,
                            "minimum": 5,
                            "maximum": 85,
                        },
                    },
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

        if name == "say":
            # TTS runs on the gateway side. The orchestrator validates
            # arguments, looks up an engine, synthesises PCM, encodes
            # Opus, and pushes frames through the WebSocket binary
            # channel that the device's audio decoder consumes. Errors
            # are surfaced as clean MCP error JSON rather than letting
            # tracebacks leak into the agent's transcript.
            try:
                result = await synthesize_and_send(arguments, gateway=gw)
            except (ValueError, NotImplementedError, RuntimeError) as exc:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": str(exc)}),
                    )
                ]
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "listen":
            # STT runs on the gateway side. The orchestrator drives the
            # device's listening state via ``listen.start``/``stop``
            # notifications, buffers the inbound Opus frames, decodes
            # them, and hands the PCM blob to the registered engine.
            # Same error-class discipline as say(): ValueError /
            # NotImplementedError / RuntimeError all turn into clean
            # MCP error JSON.
            try:
                result = await listen_and_transcribe(arguments, gateway=gw)
            except (ValueError, NotImplementedError, RuntimeError) as exc:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": str(exc)}),
                    )
                ]
            return [TextContent(type="text", text=json.dumps(result))]

        if not gw.esp32.device_connected:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "No ESP32 device connected. Please check the device."}),
                )
            ]

        if name == "move_head":
            # Belt-and-suspenders validation for the recommended pitch range.
            # The Tool inputSchema already declares minimum/maximum for both
            # yaw and pitch, but mcp Python SDK server-side enforcement of
            # JSON Schema bounds is not guaranteed across versions and
            # clients. Reject out-of-recommended values here as a clean
            # MCP error JSON before any motion command reaches the device.
            # Callers that genuinely need the firmware hard clamp 0..88
            # should use the firmware-side `set_head_angles` device tool,
            # which exposes the authoritative two-tier guard described in
            # the README "Y-axis (pitch) safe range" section.
            yaw_val = arguments.get("yaw")
            pitch_val = arguments.get("pitch")
            if (
                not isinstance(yaw_val, int)
                or isinstance(yaw_val, bool)
                or not (-90 <= yaw_val <= 90)
            ):
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": (
                                    "yaw must be an integer in -90..90 "
                                    f"(got {yaw_val!r})"
                                )
                            }
                        ),
                    )
                ]
            if (
                not isinstance(pitch_val, int)
                or isinstance(pitch_val, bool)
                or not (5 <= pitch_val <= 85)
            ):
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": (
                                    "pitch must be an integer in 5..85 "
                                    "(M5Stack-recommended operating range; "
                                    "for the wider firmware hard clamp "
                                    "0..88 use `set_head_angles`). got "
                                    f"{pitch_val!r}"
                                )
                            }
                        ),
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
            "set_servo_torque": (
                "self.robot.set_servo_torque",
                arguments,
            ),
            "set_auto_torque_release": (
                "self.robot.set_auto_torque_release",
                arguments,
            ),
            "get_touch_state": (
                "self.touch.get_touch_state",
                {},
            ),
            "set_led": (
                "self.led.set_color",
                arguments,
            ),
            "set_all_leds": (
                "self.led.set_all",
                arguments,
            ),
            # Firmware accepts colors as a JSON-encoded string (the on-device
            # MCP layer has no array property type), so re-pack the Python
            # list here. The schema we exposed above still lets the LLM
            # think in real arrays.
            "set_leds": (
                "self.led.set_many",
                {"colors": json.dumps(arguments.get("colors", []))},
            ),
            "clear_leds": (
                "self.led.clear",
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
