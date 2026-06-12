"""stdio MCP server for MCP client.

Exposes stackchan tools via the MCP Python SDK's stdio transport.
Each tool call is relayed to the connected ESP32 device.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
import inspect
import json
import logging
from typing import Any, Literal, cast

import anyio
from mcp.server import InitializationOptions, NotificationOptions, Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.types import Notification, TextContent, Tool

from . import __version__
from .gateway import get_gateway
from .notify_config import NotifyConfig, load_notify_config
from .stt import listen_and_transcribe
from .tts import synthesize_and_send

logger = logging.getLogger(__name__)

STACKCHAN_EVENT_METHOD = "stackchan/event"
CHANNEL_NOTIFICATION_METHOD = "notifications/claude/channel"
CHANNEL_CAPABILITY = "claude/channel"
_SUPPORTED_EVENT_METHODS = {STACKCHAN_EVENT_METHOD, CHANNEL_NOTIFICATION_METHOD}
STACKCHAN_EVENT_INSTRUCTIONS = (
    "Stack-chan physical events arrive as server-initiated "
    "notifications with method='stackchan/event'. Params include "
    "event_type ('touch'), subtype ('tap' or 'stroke'), "
    "duration_ms, ts, session_id. When such a notification "
    "arrives, react naturally using existing tools "
    "(set_avatar, say, set_mouth, set_leds, move_head). There is "
    "no dedicated reply tool — the existing tool palette is the "
    "reaction surface."
)
STACKCHAN_CHANNEL_INSTRUCTIONS = (
    'Stack-chan physical events arrive as Channels notifications under '
    '<channel source="plugin:stackchanmcp:stackchanmcp" action="..." '
    'subtype="..." duration_ms="...">. React naturally using existing '
    'tools (set_avatar, say, set_mouth, set_leds, move_head).'
)
STACKCHAN_JSONL_INSTRUCTIONS = (
    "Stack-chan physical events are persisted to the JSONL log; host "
    "integration consumes them externally."
)

PRESET_DPS = {
    "low": 30,
    "mid": 120,
    "high": 240,
}
SPEED_DPS_MAX = 10000
SPEED_DESCRIPTION = """speed (optional): How fast to move the head.
  - "low"  — slow, deliberate, ~30°/s. Good for curious tilts or gentle look-toward.
  - "mid"  — default natural turn, ~120°/s. Use for conversational eye contact.
  - "high" — quick reaction, ~240°/s. Use for surprise / double-take.
  - Or a raw degrees-per-second integer if you need a specific value."""

_active_session: Any | None = None
_active_sessions: dict[int, Any] = {}


class StackChanEventNotification(
    Notification[dict[str, Any], Literal["stackchan/event"]]
):
    method: Literal["stackchan/event"] = "stackchan/event"
    params: dict[str, Any]


class StackChanChannelNotification(
    Notification[dict[str, Any], Literal["notifications/claude/channel"]]
):
    method: Literal["notifications/claude/channel"] = "notifications/claude/channel"
    params: dict[str, Any]


class StackChanServer(Server):
    def __init__(self, name: str, *, notify_config: NotifyConfig) -> None:
        super().__init__(name)
        self._notify_config = notify_config

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        if notification_options is None and experimental_capabilities is None:
            return _create_initialization_options(self, self._notify_config)
        return super().create_initialization_options(
            notification_options=notification_options,
            experimental_capabilities=experimental_capabilities,
        )

    async def run(
        self,
        read_stream: Any,
        write_stream: Any,
        initialization_options: InitializationOptions,
        raise_exceptions: bool = False,
        stateless: bool = False,
    ) -> None:
        global _active_session
        session: Any | None = None
        try:
            async with AsyncExitStack() as stack:
                lifespan_context = await stack.enter_async_context(self.lifespan(self))
                session = await stack.enter_async_context(
                    ServerSession(
                        read_stream,
                        write_stream,
                        initialization_options,
                        stateless=stateless,
                    )
                )
                _active_session = session
                _active_sessions[id(session)] = session

                task_support = (
                    self._experimental_handlers.task_support
                    if self._experimental_handlers
                    else None
                )
                if task_support is not None:
                    task_support.configure_session(session)
                    await stack.enter_async_context(task_support.run())

                async with anyio.create_task_group() as tg:
                    try:
                        async for message in session.incoming_messages:
                            logger.debug("Received message: %s", message)
                            tg.start_soon(
                                self._handle_message,
                                message,
                                session,
                                lifespan_context,
                                raise_exceptions,
                            )
                    finally:
                        tg.cancel_scope.cancel()
        finally:
            if session is not None:
                _active_sessions.pop(id(session), None)
            _active_session = _latest_active_session()

    async def _handle_message(
        self,
        message: Any,
        session: Any,
        lifespan_context: Any,
        raise_exceptions: bool = False,
    ) -> None:
        global _active_session
        _active_session = session
        _active_sessions[id(session)] = session
        await super()._handle_message(
            message,
            session,
            lifespan_context,
            raise_exceptions,
    )


def _latest_active_session() -> Any | None:
    if not _active_sessions:
        return None
    return next(reversed(_active_sessions.values()))


async def notify_stackchan_event(method: str, params: dict[str, Any]) -> None:
    """Forward a stackchan event to the connected MCP client."""
    if method not in _SUPPORTED_EVENT_METHODS:
        logger.warning("Unsupported stackchan event notification method: %s", method)
        return

    sessions = list(_active_sessions.values())
    if not sessions and _active_session is not None:
        sessions = [_active_session]
    if not sessions:
        logger.warning("Cannot emit %s notification: no active MCP session", method)
        return

    notification = _build_stackchan_notification(method, params)
    for session in sessions:
        try:
            await session.send_notification(cast(Any, notification))
        except Exception as exc:  # pragma: no cover - depends on client transport failure
            logger.warning("Failed to emit %s notification: %s", method, exc)


def _build_stackchan_notification(
    method: str,
    params: dict[str, Any],
) -> StackChanEventNotification | StackChanChannelNotification:
    if method == STACKCHAN_EVENT_METHOD:
        return StackChanEventNotification(params=params)
    return StackChanChannelNotification(params=params)


def _build_experimental_capabilities(
    notify_config: NotifyConfig,
) -> dict[str, dict[str, Any]]:
    capabilities: dict[str, dict[str, Any]] = {}
    if notify_config.legacy_event_enabled:
        capabilities[STACKCHAN_EVENT_METHOD] = {}
    if notify_config.channels_enabled:
        capabilities[CHANNEL_CAPABILITY] = {}
    return capabilities


def _build_stackchan_event_instructions(notify_config: NotifyConfig) -> str | None:
    fragments = []
    if notify_config.channels_enabled:
        fragments.append(STACKCHAN_CHANNEL_INSTRUCTIONS)
    if notify_config.legacy_event_enabled:
        fragments.append(STACKCHAN_EVENT_INSTRUCTIONS)
    if (
        notify_config.jsonl_enabled
        and not notify_config.channels_enabled
        and not notify_config.legacy_event_enabled
    ):
        fragments.append(STACKCHAN_JSONL_INSTRUCTIONS)
    if not fragments:
        return None
    return "\n\n".join(fragments)


def _create_initialization_options(
    server: Server,
    notify_config: NotifyConfig,
) -> InitializationOptions:
    return InitializationOptions(
        server_name="stackchanmcp",
        server_version=__version__,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities=_build_experimental_capabilities(notify_config),
        ),
        instructions=_build_stackchan_event_instructions(notify_config),
    )


def _verify_mcp_sdk_compatibility() -> None:
    """Fail fast if the installed MCP SDK no longer exposes the private
    attributes that ``StackChanServer`` depends on.

    ``StackChanServer`` mirrors a slimmed-down copy of ``Server.run()`` so it
    can capture the active ``ServerSession`` for server-initiated
    ``stackchan/event`` notifications. The public MCP SDK currently does not
    offer a stable hook for this, so the subclass touches
    ``Server._experimental_handlers`` and ``Server._handle_message`` directly.

    These private members are pinned by the ``mcp>=1.27,<2.0`` range declared
    in ``pyproject.toml``. This guard adds an extra safety net so the gateway
    fails with a clear ``RuntimeError`` at startup rather than silently
    dropping notifications or crashing mid-message if a future installation
    somehow resolves a wholly incompatible SDK shape.
    """

    probe = Server("compat-check")

    if not hasattr(probe, "_experimental_handlers"):
        raise RuntimeError(
            "stackchan-mcp gateway requires `mcp.server.Server._experimental_handlers` "
            "to exist on instances. The installed MCP SDK appears to have removed or "
            "renamed this attribute; pin `mcp` to a verified 1.x release."
        )

    handle = getattr(probe, "_handle_message", None)
    if not callable(handle) or not inspect.iscoroutinefunction(handle):
        raise RuntimeError(
            "stackchan-mcp gateway requires `mcp.server.Server._handle_message` to be "
            "an async callable. The installed MCP SDK does not expose it in the "
            "expected shape; pin `mcp` to a verified 1.x release."
        )

    try:
        sig = inspect.signature(handle)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "stackchan-mcp gateway could not introspect "
            "`mcp.server.Server._handle_message` signature on the installed MCP SDK; "
            "pin `mcp` to a verified 1.x release."
        ) from exc

    positional = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
        )
    ]
    if len(positional) < 4:
        raise RuntimeError(
            "stackchan-mcp gateway requires `mcp.server.Server._handle_message` to "
            "accept at least 4 positional arguments "
            "(message, session, lifespan_context, raise_exceptions); the installed "
            f"MCP SDK exposes {sig}. Pin `mcp` to a verified 1.x release."
        )


def _resolve_speed_dps(speed: Any) -> int | None:
    """Return an int speed_dps to forward, or None to omit the field."""
    if speed is None:
        return None
    if isinstance(speed, bool):
        raise TypeError("speed must be a preset string or an integer, not bool")
    if isinstance(speed, str):
        if speed not in PRESET_DPS:
            raise ValueError(
                f"speed preset must be one of {list(PRESET_DPS)}, got {speed!r}"
            )
        return PRESET_DPS[speed]
    if isinstance(speed, int):
        if speed < 1:
            raise ValueError(f"speed integer must be >= 1, got {speed}")
        if speed > SPEED_DPS_MAX:
            raise ValueError(f"speed integer must be <= {SPEED_DPS_MAX}, got {speed}")
        return speed
    raise TypeError(
        f"speed must be 'low' / 'mid' / 'high' / int / None, got {type(speed).__name__}"
    )


async def _dispatch_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    gateway: Any,
) -> list[TextContent]:
    """Run one StackChan MCP tool against the provided gateway instance."""
    if name == "get_status":
        status = gateway.esp32.get_status()
        return [TextContent(type="text", text=json.dumps(status, indent=2))]

    if name == "say":
        try:
            result = await synthesize_and_send(arguments, gateway=gateway)
        except (ValueError, NotImplementedError, RuntimeError) as exc:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": str(exc)}),
                )
            ]
        return [TextContent(type="text", text=json.dumps(result))]

    if name == "listen":
        try:
            result = await listen_and_transcribe(arguments, gateway=gateway)
        except (ValueError, NotImplementedError, RuntimeError) as exc:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": str(exc)}),
                )
            ]
        return [TextContent(type="text", text=json.dumps(result))]

    if name == "load_avatar_set":
        archive_path = arguments.get("archive_path", "")
        mode = arguments.get("mode", "")
        try:
            timeout = float(arguments.get("timeout", 60.0))
        except (TypeError, ValueError):
            timeout = 60.0
        if not archive_path or not isinstance(archive_path, str):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"ok": False, "error": "archive_path is required"}
                    ),
                )
            ]
        if mode not in ("layered", "matrix"):
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"ok": False, "error": f"unknown mode: {mode}"}),
                )
            ]
        result = await gateway.load_avatar_set(archive_path, mode, timeout)
        return [TextContent(type="text", text=json.dumps(result))]

    if not gateway.esp32.device_connected:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": "No ESP32 device connected. Please check the device."}
                ),
            )
        ]

    if name == "move_head":
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
        try:
            speed_dps = _resolve_speed_dps(arguments.get("speed"))
        except (TypeError, ValueError) as exc:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": str(exc)}),
                )
            ]
        arguments = {"yaw": yaw_val, "pitch": pitch_val}
        if speed_dps is not None:
            arguments["speed_dps"] = speed_dps

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
        "gateway_config_get": (
            "self.gateway_config.get",
            {},
        ),
        "gateway_config_set": (
            "self.gateway_config.set",
            arguments,
        ),
        "set_avatar": (
            "self.display.set_avatar",
            arguments,
        ),
        "set_mouth": (
            "self.display.set_mouth",
            arguments,
        ),
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
        "set_leds": (
            "self.led.set_many",
            {"colors": json.dumps(arguments.get("colors", []))},
        ),
        "clear_leds": (
            "self.led.clear",
            {},
        ),
        "port_b_ws2812_init": (
            "self.port_b.ws2812.init",
            arguments,
        ),
        "port_b_ws2812_set_pixel": (
            "self.port_b.ws2812.set_pixel",
            arguments,
        ),
        "port_b_ws2812_set_strip": (
            "self.port_b.ws2812.set_strip",
            {"colors": json.dumps(arguments.get("colors", []))},
        ),
        "port_b_ws2812_refresh": (
            "self.port_b.ws2812.refresh",
            {},
        ),
        "port_b_ws2812_clear": (
            "self.port_b.ws2812.clear",
            {},
        ),
        "i2c_scan": (
            "self.i2c.scan",
            {},
        ),
        "i2c_read": (
            "self.i2c.read",
            arguments,
        ),
        "i2c_write": (
            "self.i2c.write",
            arguments,
        ),
        "i2c_write_read": (
            "self.i2c.write_read",
            arguments,
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
    result, error = await gateway.esp32.call_tool(esp32_name, esp32_args)

    if error:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": error.get("message", str(error))}),
            )
        ]

    if isinstance(result, dict):
        content = result.get("content", [])
        if content and isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
            if texts:
                return [TextContent(type="text", text="\n".join(texts))]

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return [TextContent(type="text", text=str(result))]


def create_server(notify_config: NotifyConfig | None = None) -> StackChanServer:
    """Create and configure the MCP server with tool handlers."""
    _verify_mcp_sdk_compatibility()
    if notify_config is None:
        notify_config = load_notify_config()
    server = StackChanServer("stackchanmcp", notify_config=notify_config)

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
                        "speed": {
                            "oneOf": [
                                {"enum": ["low", "mid", "high"]},
                                {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": SPEED_DPS_MAX,
                                },
                            ],
                            "description": SPEED_DESCRIPTION,
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
                name="gateway_config_get",
                description=(
                    "Read the device's NVS-backed WebSocket gateway "
                    "connection settings. Returns url, fallback_url, "
                    "token_set (never the token value), forced_keys, "
                    "force_mode, discovery_enabled, and connected_url when "
                    "the current WebSocket candidate is connected. Empty url "
                    "enables mDNS discovery when firmware discovery support "
                    "is compiled in and the primary URL is not forced; "
                    "fallback_url is tried after discovery and is suitable "
                    "for an out-of-LAN relay. forced_keys lists any of url, "
                    "fallback_url, and token that a non-empty Kconfig default "
                    "overrides at connect time; force_mode=true means at "
                    "least one key is forced."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="gateway_config_set",
                description=(
                    "Update the device's NVS-backed WebSocket gateway "
                    "connection settings. Optional string fields: url, "
                    "fallback_url, token; at least one must be provided. "
                    "Passing an empty string clears that key. Leave url "
                    "empty to enable mDNS discovery on the next reconnect "
                    "when firmware discovery support is compiled in and the "
                    "primary URL is not forced; fallback_url is tried after "
                    "discovery and is suitable for an out-of-LAN relay. The "
                    "change is persisted but does not disconnect, reconnect, "
                    "or reboot the device; it takes effect on the next "
                    "reconnect. forced_keys lists any of url, fallback_url, "
                    "and token that a non-empty Kconfig default overrides at "
                    "connect time; force_mode=true means at least one key is "
                    "forced, so updates to those keys are ignored until a "
                    "non-force build is flashed."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": (
                                "Primary NVS WebSocket URL. Empty string "
                                "clears websocket.url, which enables mDNS "
                                "discovery on the next reconnect when "
                                "discovery is compiled in and the primary URL "
                                "is not forced."
                            ),
                        },
                        "fallback_url": {
                            "type": "string",
                            "description": (
                                "Fallback NVS WebSocket URL. Tried after "
                                "discovery and suitable for an out-of-LAN "
                                "relay. Empty string clears "
                                "websocket.fallback_url."
                            ),
                        },
                        "token": {
                            "type": "string",
                            "description": (
                                "Bearer token stored in NVS and sent to the "
                                "gateway. Empty string clears the token. The "
                                "token value is never returned by "
                                "gateway_config_get."
                            ),
                        },
                    },
                },
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
                name="port_b_ws2812_init",
                description=(
                    "Initialize a WS2812-compatible LED strip connected to "
                    "Port B (CoreS3 HY2.0-4P digital OUTPUT, GPIO 9). "
                    "led_count is the number of LEDs in the strip (1..256). "
                    "Repeated calls with the same led_count are no-ops; a "
                    "different led_count rebuilds the strip handle. Port B "
                    "outputs 3.3 V CMOS data on GPIO 9; older strict 5 V "
                    "WS2812 variants may require a level shifter."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "led_count": {
                            "type": "integer",
                            "description": "Number of LEDs in the strip (1..256).",
                            "minimum": 1,
                            "maximum": 256,
                        },
                    },
                    "required": ["led_count"],
                },
            ),
            Tool(
                name="port_b_ws2812_set_pixel",
                description=(
                    "Set one LED in the Port B WS2812 strip buffer. Call "
                    "port_b_ws2812_init first. index is 0..255, with the "
                    "effective range bounded by led_count. r, g, and b are "
                    "0..255. By default the color is buffered only; pass "
                    "refresh=true to latch it immediately, or call "
                    "port_b_ws2812_refresh after several buffered updates. "
                    "Port B outputs 3.3 V CMOS data on GPIO 9; older strict "
                    "5 V WS2812 variants may require a level shifter."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "LED index (0..255).",
                            "minimum": 0,
                            "maximum": 255,
                        },
                        "r": {
                            "type": "integer",
                            "description": "Red 0..255.",
                            "minimum": 0,
                            "maximum": 255,
                        },
                        "g": {
                            "type": "integer",
                            "description": "Green 0..255.",
                            "minimum": 0,
                            "maximum": 255,
                        },
                        "b": {
                            "type": "integer",
                            "description": "Blue 0..255.",
                            "minimum": 0,
                            "maximum": 255,
                        },
                        "refresh": {
                            "type": "boolean",
                            "description": "True to latch the update immediately.",
                            "default": False,
                        },
                    },
                    "required": ["index", "r", "g", "b"],
                },
            ),
            Tool(
                name="port_b_ws2812_set_strip",
                description=(
                    "Set multiple LEDs in the Port B WS2812 strip and refresh "
                    "immediately. Call port_b_ws2812_init first. colors is an "
                    "array of [r,g,b] integer triples applied from LED index 0; "
                    "up to led_count entries are written, extras are ignored, "
                    "and missing trailing entries preserve the previous buffer. "
                    "The firmware validates the full payload before writing. "
                    "Port B outputs 3.3 V CMOS data on GPIO 9; older strict "
                    "5 V WS2812 variants may require a level shifter."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "colors": {
                            "type": "array",
                            "description": (
                                "Array of [r,g,b] triples, each integer 0..255."
                            ),
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 255,
                                },
                                "minItems": 3,
                                "maxItems": 3,
                            },
                            "minItems": 1,
                            "maxItems": 256,
                        },
                    },
                    "required": ["colors"],
                },
            ),
            Tool(
                name="port_b_ws2812_refresh",
                description=(
                    "Refresh the Port B WS2812 strip, latching the current "
                    "buffered colors out on CoreS3 HY2.0-4P digital OUTPUT "
                    "GPIO 9. Call port_b_ws2812_init first. Use this after "
                    "one or more port_b_ws2812_set_pixel calls made with "
                    "refresh=false. Port B outputs 3.3 V CMOS data; older "
                    "strict 5 V WS2812 variants may require a level shifter."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="port_b_ws2812_clear",
                description=(
                    "Turn off every LED in the Port B WS2812 strip and "
                    "refresh immediately on CoreS3 HY2.0-4P digital OUTPUT "
                    "GPIO 9. Call port_b_ws2812_init first. This clears the "
                    "driver's per-pixel buffer. Port B outputs 3.3 V CMOS "
                    "data; older strict 5 V WS2812 variants may require a "
                    "level shifter."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="say",
                description=(
                    "Speak the given text on the device speaker via gateway-side "
                    "TTS. The gateway synthesises audio, encodes it to Opus, "
                    "and pushes frames over the existing WebSocket; the device "
                    "firmware does not change. Engine is selectable via 'voice' "
                    "(default 'voicevox'). If the text contains a supported "
                    "expression emoji, say first switches the avatar face in the "
                    "same call: happy (😊 😄 😀 😁 🙂 😆 🥰 😍 😋 🤗), "
                    "sad (😢 😭 😞 😔 ☹️ 🙁 😿), surprised (😲 😮 😯 😱 🤯), "
                    "embarrassed (😳 😅 🫣), thinking (🤔 🧐 💭). The first "
                    "mapped emoji wins; unmapped emoji do not change the face, "
                    "and emoji never select 'off'. Irodori keeps emoji in the "
                    "TTS input so they can act as voice-style cues. Engines "
                    "without emoji-style support, including VOICEVOX, strip all "
                    "emoji before synthesis; if stripping leaves empty text, the "
                    "face change is still attempted and speech is skipped."
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
            Tool(
                name="i2c_scan",
                description=(
                    "Scan the external I2C bus on Grove Port A and return "
                    "all 7-bit addresses (probe range 0x08..0x77, "
                    "excluding I2C reserved ranges) that ACK a probe. Use "
                    "this to discover attached M5Stack Unit modules "
                    "(ENV III, ToF, gas sensor, PaHub, etc.). On-board ICs "
                    "on the internal bus are NOT included (this tool "
                    "operates on a physically separate bus). Returns "
                    "{\"ok\": true, \"addresses\": [...]}."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="i2c_read",
                description=(
                    "Read n_bytes from an I2C device at 7-bit address "
                    "`addr` on Grove Port A. Use this for protocols that "
                    "read the device's current register / output without "
                    "a preceding write. For typical 'write register "
                    "address, then read' patterns, use `i2c_write_read` "
                    "instead. Returns "
                    "{\"ok\": true, \"bytes\": [...]} or "
                    "{\"ok\": false, \"error\": \"ESP_ERR_TIMEOUT\"} on NACK."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "addr": {
                            "type": "integer",
                            "description": (
                                "7-bit I2C address; range 0x08..0x77 "
                                "(I2C reserved ranges excluded — matches "
                                "the i2c_scan probe range)."
                            ),
                            "minimum": 8,
                            "maximum": 119,
                        },
                        "n_bytes": {
                            "type": "integer",
                            "description": "Bytes to read (1..256).",
                            "minimum": 1,
                            "maximum": 256,
                        },
                    },
                    "required": ["addr", "n_bytes"],
                },
            ),
            Tool(
                name="i2c_write",
                description=(
                    "Write bytes to an I2C device at 7-bit address `addr` "
                    "on Grove Port A. `bytes` is an array of integers "
                    "(0..255). This tool operates on the external Port A "
                    "bus only; on-board ICs (PMIC, AW9523, touch, etc.) "
                    "on the internal bus are not reachable."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "addr": {
                            "type": "integer",
                            "description": (
                                "7-bit I2C address; range 0x08..0x77 "
                                "(I2C reserved ranges excluded — matches "
                                "the i2c_scan probe range)."
                            ),
                            "minimum": 8,
                            "maximum": 119,
                        },
                        "bytes": {
                            "type": "array",
                            "description": "Bytes to write (each 0..255).",
                            "items": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 255,
                            },
                        },
                    },
                    "required": ["addr", "bytes"],
                },
            ),
            Tool(
                name="i2c_write_read",
                description=(
                    "Write `write_bytes` to an I2C device at 7-bit address "
                    "`addr` on Grove Port A, then read `n_bytes` back in a "
                    "single Repeated Start transaction. Common 'set "
                    "register pointer, then read' idiom: pass "
                    "write_bytes=[reg_addr] to read from a specific "
                    "register."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "addr": {
                            "type": "integer",
                            "description": (
                                "7-bit I2C address; range 0x08..0x77 "
                                "(I2C reserved ranges excluded — matches "
                                "the i2c_scan probe range)."
                            ),
                            "minimum": 8,
                            "maximum": 119,
                        },
                        "write_bytes": {
                            "type": "array",
                            "description": (
                                "Bytes to write before reading "
                                "(each 0..255)."
                            ),
                            "items": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 255,
                            },
                        },
                        "n_bytes": {
                            "type": "integer",
                            "description": "Bytes to read (1..256).",
                            "minimum": 1,
                            "maximum": 256,
                        },
                    },
                    "required": ["addr", "write_bytes", "n_bytes"],
                },
            ),
            Tool(
                name="load_avatar_set",
                description=(
                    "Load a dynamic avatar set onto the connected ESP32 "
                    "(Phase 4.5 avatar pipeline). The gateway stages the "
                    "payload on its HTTP server, notifies the device via "
                    "WebSocket, and the device fetches + SHA256-verifies + "
                    "loads it into PSRAM. ``archive_path`` must point to a "
                    "raw RGB565 file on the gateway host: layered mode = "
                    "14 frames (face 6 + eyes 3 + mouth 5) totalling "
                    "537,600 bytes; matrix mode = 90 frames (6 × 3 × 5) "
                    "totalling 3,456,000 bytes. Returns ok / checksum / "
                    "bytes_transferred / error."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "archive_path": {
                            "type": "string",
                            "description": (
                                "Filesystem path on the gateway host to "
                                "the raw RGB565 payload."
                            ),
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["layered", "matrix"],
                            "description": (
                                "'layered' (14 frames, ~525 KB) or "
                                "'matrix' (90 frames, ~3.3 MB)."
                            ),
                        },
                        "timeout": {
                            "type": "number",
                            "description": (
                                "Max seconds to wait for the device's "
                                "avatar_set_loaded reply."
                            ),
                            "default": 60.0,
                            "minimum": 5.0,
                            "maximum": 300.0,
                        },
                    },
                    "required": ["archive_path", "mode"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        """Handle a tool call by relaying to ESP32."""
        arguments = arguments or {}
        return await _dispatch_mcp_tool(name, arguments, get_gateway())

    return server


async def run_stdio_server(notify_config: NotifyConfig | None = None) -> None:
    """Run the MCP server on stdio."""
    if notify_config is None:
        notify_config = load_notify_config()
    server = create_server(notify_config=notify_config)
    async with stdio_server() as (read_stream, write_stream):
        logger.info("stdio MCP server starting")
        await server.run(
            read_stream,
            write_stream,
            _create_initialization_options(server, notify_config),
        )
