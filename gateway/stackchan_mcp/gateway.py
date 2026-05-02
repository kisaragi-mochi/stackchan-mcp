"""Two-faced gateway: bridges MCP client (stdio MCP) and ESP32 (WebSocket MCP).

MCP client sees a standard MCP server via stdio.
ESP32 sees a WebSocket server that sends MCP client requests.
This module orchestrates both sides.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web

from .capture_server import create_capture_app
from .esp32_client import ESP32Manager

logger = logging.getLogger(__name__)


class Gateway:
    """Main gateway orchestrator.

    Holds the ESP32 manager and provides the bridge between
    the stdio MCP server (MCP client side) and the ESP32 device.

    Also runs an HTTP capture server for receiving photos from ESP32.
    """

    def __init__(self):
        self.esp32 = ESP32Manager()
        self._running = False
        self._http_runner: web.AppRunner | None = None

    @property
    def vision_url(self) -> str:
        """URL for ESP32 to POST captured photos to.

        VISION_HOST should be the LAN IP of the host running this gateway,
        as seen from the ESP32 (e.g. something like 192.168.x.y on a typical
        home network). Falls back to "127.0.0.1" with a warning if unset; in
        that case the ESP32 will not be able to reach the capture endpoint
        over the network.
        """
        host = os.getenv("VISION_HOST")
        if not host:
            logger.warning(
                "VISION_HOST not set; defaulting to 127.0.0.1. "
                "ESP32 will not reach the capture endpoint unless "
                "VISION_HOST is set to this host's LAN IP."
            )
            host = "127.0.0.1"
        port = int(os.getenv("CAPTURE_PORT", "8766"))
        return f"http://{host}:{port}/capture"

    async def start(self) -> None:
        """Start the ESP32 WebSocket server and HTTP capture server."""
        host = os.getenv("HOST", "0.0.0.0")
        ws_port = int(os.getenv("WS_PORT", os.getenv("PORT", "8765")))
        capture_port = int(os.getenv("CAPTURE_PORT", "8766"))

        # Start WebSocket server for ESP32
        await self.esp32.start(host, ws_port, vision_url=self.vision_url)

        # Start HTTP capture server
        app = create_capture_app()
        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        site = web.TCPSite(self._http_runner, host, capture_port)
        await site.start()

        self._running = True
        logger.info(
            "Gateway started: WS on %s:%d, capture on %s:%d, vision_url=%s",
            host, ws_port, host, capture_port, self.vision_url,
        )

    async def stop(self) -> None:
        """Stop the gateway."""
        self._running = False
        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None
        await self.esp32.stop()
        logger.info("Gateway stopped")


# Singleton gateway instance, shared between stdio server and ESP32 manager
_gateway: Gateway | None = None


def get_gateway() -> Gateway:
    """Get or create the singleton gateway."""
    global _gateway
    if _gateway is None:
        _gateway = Gateway()
    return _gateway
