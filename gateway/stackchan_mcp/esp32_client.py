"""ESP32 connection manager.

Acts as a WebSocket server that ESP32 connects TO,
and as an MCP client that sends commands TO the ESP32.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

import websockets
import websockets.exceptions
from websockets.asyncio.server import ServerConnection

from .protocol import HelloResponse, make_mcp_message, parse_jsonrpc_response

logger = logging.getLogger(__name__)

# Timeout for waiting for ESP32 responses
RESPONSE_TIMEOUT = 10.0


class ESP32Connection:
    """Manages a single ESP32 device connection."""

    def __init__(self, ws: ServerConnection, session_id: str):
        self._ws = ws
        self.session_id = session_id
        self.device_id: str = "unknown"
        self.tools: list[dict[str, Any]] = []
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._connected = True
        self._initialized = False
        # Device-declared WebSocket protocol version (from the hello
        # message). Defaults to 1, which matches the firmware's default
        # (firmware/main/protocols/websocket_protocol.h: ``version_ = 1``)
        # and the audio framing this gateway emits today (raw Opus
        # payload). v2/v3 add a BinaryProtocol header that this gateway
        # does not yet wrap — see Issue follow-up to #70.
        self.protocol_version: int = 1

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def initialized(self) -> bool:
        return self._initialized

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def send_mcp_request(
        self, method: str, params: dict[str, Any]
    ) -> tuple[Any, dict[str, Any] | None]:
        """Send an MCP request to ESP32 and wait for response.

        Returns (result, error).
        """
        if not self._connected:
            return None, {"code": -32000, "message": "ESP32 not connected"}

        req_id = self._next_id()
        message = make_mcp_message(self.session_id, method, params, req_id)

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            await self._ws.send(json.dumps(message))
            response = await asyncio.wait_for(future, timeout=RESPONSE_TIMEOUT)
            return parse_jsonrpc_response(response)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return None, {"code": -32000, "message": f"Timeout waiting for ESP32 response (method={method})"}
        except Exception as exc:
            self._pending.pop(req_id, None)
            return None, {"code": -32000, "message": f"ESP32 communication error: {exc}"}

    async def initialize(self, vision_url: str = "", vision_token: str = "") -> bool:
        """Send MCP initialize to ESP32."""
        capabilities: dict[str, Any] = {}
        if vision_url:
            vision: dict[str, Any] = {"url": vision_url}
            if vision_token:
                vision["token"] = vision_token
            capabilities["vision"] = vision
        result, error = await self.send_mcp_request("initialize", {"capabilities": capabilities})
        if error:
            logger.error("ESP32 initialize failed: %s", error)
            return False

        logger.info(
            "ESP32 initialized: protocol=%s server=%s",
            result.get("protocolVersion", "?"),
            result.get("serverInfo", {}),
        )
        self._initialized = True
        return True

    async def discover_tools(self) -> list[dict[str, Any]]:
        """Discover tools available on ESP32."""
        all_tools: list[dict[str, Any]] = []
        cursor = ""

        while True:
            params: dict[str, Any] = {"cursor": cursor}
            result, error = await self.send_mcp_request("tools/list", params)

            if error:
                logger.error("tools/list failed: %s", error)
                break

            tools = result.get("tools", [])
            all_tools.extend(tools)

            next_cursor = result.get("nextCursor", "")
            if not next_cursor:
                break
            cursor = next_cursor

        self.tools = all_tools
        logger.info("Discovered %d tools on ESP32", len(all_tools))
        return all_tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[Any, dict[str, Any] | None]:
        """Call a tool on ESP32."""
        return await self.send_mcp_request(
            "tools/call", {"name": name, "arguments": arguments}
        )

    def handle_response(self, payload: dict[str, Any]) -> None:
        """Handle an incoming MCP response from ESP32."""
        req_id = payload.get("id")
        if req_id is not None and req_id in self._pending:
            future = self._pending.pop(req_id)
            if not future.done():
                future.set_result(payload)
        else:
            # Notification (no id) — log and discard for now
            method = payload.get("method", "")
            logger.info("ESP32 notification: %s", method)

    async def _ws_send(self, payload: bytes | str) -> None:
        """Send a payload, translating websockets errors to ConnectionError.

        The ``websockets`` library raises its own exception hierarchy
        (``ConnectionClosed`` and friends), which is *not* a subclass
        of the built-in :class:`ConnectionError`. Without translation
        the orchestrator's ``except ConnectionError`` filter — and the
        MCP handler's ``except RuntimeError`` filter — would let those
        errors leak as raw tracebacks into the MCP transport, breaking
        the say() tool's clean error JSON contract on mid-stream
        disconnect.
        """
        try:
            await self._ws.send(payload)
        except (
            websockets.exceptions.ConnectionClosed,
            OSError,
        ) as exc:
            # Mark the connection dead so subsequent calls fail fast
            # rather than each one re-discovering the broken socket.
            self.disconnect()
            raise ConnectionError(f"WebSocket send failed: {exc}") from exc

    async def send_audio_frame(self, opus_frame: bytes) -> None:
        """Send a single Opus frame to the ESP32 as a WebSocket binary frame.

        The device's ``OnData`` handler (firmware/main/protocols/
        websocket_protocol.cc) treats every binary frame as an Opus
        audio payload to feed into its decoder, so this method is the
        TTS pipeline's egress point.
        """
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        await self._ws_send(opus_frame)

    async def send_tts_state(self, state: str) -> None:
        """Send a TTS state notification (``start`` / ``stop`` / ...).

        The device's :func:`Application::OnIncomingJson` translates
        ``{"type":"tts","state":"start"}`` into
        :data:`kDeviceStateSpeaking`, which is the gate for
        :func:`OnIncomingAudio` pushing packets into the decode queue
        (see ``firmware/main/application.cc``). Without bracketing the
        audio frames in start/stop, the device drops them on the floor
        and the speaker stays silent — the TTS tool returns success
        without anything actually playing.
        """
        if not self._connected:
            raise ConnectionError("ESP32 not connected")
        message = {
            "session_id": self.session_id,
            "type": "tts",
            "state": state,
        }
        await self._ws_send(json.dumps(message))

    def disconnect(self) -> None:
        """Mark connection as disconnected."""
        self._connected = False
        self._initialized = False
        # Cancel all pending futures
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("ESP32 disconnected"))
        self._pending.clear()


class ESP32Manager:
    """Manages ESP32 device connections.

    Runs a WebSocket server that ESP32 devices connect to.
    Currently supports a single device connection.
    """

    def __init__(self):
        self._connection: ESP32Connection | None = None
        self._server: Any = None
        self._lock = asyncio.Lock()
        self._init_tasks: list[asyncio.Task] = []
        self._vision_url: str = ""
        self._vision_token: str = ""
        # Per-device serialisation for TTS send sequences. Acquired by
        # the orchestrator around the entire start → frames → stop
        # block so concurrent ``say()`` invocations cannot interleave
        # their Opus frames on the same WebSocket or overlap their
        # ``tts.start``/``tts.stop`` notifications (which would yank
        # the firmware out of ``kDeviceStateSpeaking`` mid-utterance
        # and silently drop the remaining audio). The lock is scoped
        # to the manager because the manager owns the device today —
        # if multi-device support lands later, the lock should move
        # onto :class:`ESP32Connection` instead.
        self._tts_lock = asyncio.Lock()

    @property
    def device_connected(self) -> bool:
        return self._connection is not None and self._connection.connected

    @property
    def connection(self) -> ESP32Connection | None:
        return self._connection

    @property
    def tts_lock(self) -> asyncio.Lock:
        """Per-device lock guarding the TTS send sequence.

        See :attr:`_tts_lock` for the rationale; the orchestrator wraps
        the start → frames → stop block in ``async with`` on this lock.
        """
        return self._tts_lock

    async def start(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        vision_url: str = "",
        vision_token: str = "",
    ) -> None:
        """Start the WebSocket server for ESP32 connections."""
        self._vision_url = vision_url
        self._vision_token = vision_token
        logger.info("ESP32 WebSocket server starting on ws://%s:%d", host, port)
        self._server = await websockets.serve(
            self._handler,
            host,
            port,
            process_request=self._check_auth,
        )

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        # Cancel any pending initialization tasks
        for task in self._init_tasks:
            task.cancel()
        self._init_tasks.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _check_auth(
        self, connection: ServerConnection, request: websockets.http11.Request
    ) -> None | websockets.http11.Response:
        """Validate Bearer token.

        websockets 16+ passes (connection, request) to process_request.
        """
        expected = os.getenv("STACKCHAN_TOKEN") or os.getenv("BEARER_TOKEN")
        if not expected:
            logger.warning("STACKCHAN_TOKEN not set — accepting all connections")
            return None

        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {expected}":
            return None

        logger.warning("ESP32 auth rejected")
        return websockets.http11.Response(
            401, "Unauthorized", websockets.datastructures.Headers()
        )

    async def _handler(self, ws: ServerConnection) -> None:
        """Handle an incoming ESP32 WebSocket connection.

        Architecture: the message read loop runs continuously, dispatching
        MCP responses to pending futures. Initialization (initialize + tools/list)
        runs as a separate task so it doesn't block the read loop.
        """
        session_id = str(uuid.uuid4())
        device_id = (
            ws.request.headers.get("Device-Id", "unknown") if ws.request else "unknown"
        )
        logger.info("ESP32 connecting: device=%s", device_id)

        connection = ESP32Connection(ws, session_id)
        connection.device_id = device_id

        try:
            async for message in ws:
                if isinstance(message, bytes):
                    # Binary = audio frame, ignore for now
                    continue

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from ESP32: %s", str(message)[:100])
                    continue

                msg_type = data.get("type", "")

                if msg_type == "hello":
                    # ESP32 hello handshake
                    features = data.get("features", {})
                    if not features.get("mcp"):
                        logger.warning("ESP32 does not support MCP, rejecting")
                        await ws.close()
                        return

                    # Capture the device's WebSocket protocol version
                    # so callers (e.g. the TTS pipeline) can decide
                    # whether their wire format is compatible. The
                    # firmware accepts raw Opus only on v1; v2/v3 wrap
                    # the payload in a BinaryProtocol header.
                    raw_version = data.get("version", 1)
                    try:
                        connection.protocol_version = int(raw_version)
                    except (TypeError, ValueError):
                        connection.protocol_version = 1
                    if connection.protocol_version != 1:
                        logger.warning(
                            "ESP32 negotiated WebSocket protocol "
                            "version=%s; the gateway emits raw Opus "
                            "binary frames matching v1 only. TTS "
                            "calls (say) will be blocked at the "
                            "orchestrator until v2/v3 BinaryProtocol "
                            "header wrapping is implemented",
                            connection.protocol_version,
                        )

                    # Send hello response
                    resp = HelloResponse(session_id=session_id)
                    await ws.send(resp.model_dump_json())

                    # Register connection
                    async with self._lock:
                        if self._connection and self._connection.connected:
                            logger.warning("Replacing existing ESP32 connection")
                            self._connection.disconnect()
                        self._connection = connection

                    # Start initialization as a separate task so the read loop
                    # continues to pump messages (responses to initialize/tools_list)
                    task = asyncio.create_task(self._init_device(connection, device_id))
                    self._init_tasks.append(task)
                    task.add_done_callback(lambda t: self._init_tasks.remove(t) if t in self._init_tasks else None)

                elif msg_type == "mcp":
                    # MCP response from ESP32
                    payload = data.get("payload", {})
                    connection.handle_response(payload)

                else:
                    logger.debug("ESP32 message type=%s (ignored)", msg_type)

        except websockets.exceptions.ConnectionClosed:
            logger.info("ESP32 disconnected: device=%s", device_id)
        finally:
            connection.disconnect()
            async with self._lock:
                if self._connection is connection:
                    self._connection = None

    async def _init_device(self, connection: ESP32Connection, device_id: str) -> None:
        """Initialize MCP session with a newly connected device."""
        if await connection.initialize(
            vision_url=self._vision_url,
            vision_token=self._vision_token,
        ):
            await connection.discover_tools()
            logger.info(
                "ESP32 ready: device=%s tools=%d",
                device_id,
                len(connection.tools),
            )
        else:
            logger.error("ESP32 MCP initialization failed")

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[Any, dict[str, Any] | None]:
        """Call a tool on the connected ESP32 device."""
        if not self._connection or not self._connection.connected:
            return None, {"code": -32000, "message": "No ESP32 device connected"}
        if not self._connection.initialized:
            return None, {"code": -32000, "message": "ESP32 not initialized"}
        return await self._connection.call_tool(name, arguments)

    async def send_audio_frame(self, opus_frame: bytes) -> None:
        """Push a single Opus frame to the connected device.

        Used by the TTS pipeline to deliver synthesised audio. Raises
        :class:`ConnectionError` if no device is currently attached so
        the orchestrator can surface a clean error to the MCP client
        instead of silently dropping audio.
        """
        if not self._connection or not self._connection.connected:
            raise ConnectionError("No ESP32 device connected")
        await self._connection.send_audio_frame(opus_frame)

    async def send_tts_state(self, state: str) -> None:
        """Send a TTS state notification (``start`` / ``stop`` / ...).

        Required around audio frame egress so the device transitions
        into ``kDeviceStateSpeaking`` and back; see
        :meth:`ESP32Connection.send_tts_state` for the full rationale.
        """
        if not self._connection or not self._connection.connected:
            raise ConnectionError("No ESP32 device connected")
        await self._connection.send_tts_state(state)

    def get_status(self) -> dict[str, Any]:
        """Get current connection status."""
        if not self._connection or not self._connection.connected:
            return {
                "connected": False,
                "device_id": None,
                "tools_count": 0,
            }
        return {
            "connected": True,
            "device_id": self._connection.device_id,
            "initialized": self._connection.initialized,
            "tools_count": len(self._connection.tools),
            "tools": [t.get("name", "") for t in self._connection.tools],
        }
