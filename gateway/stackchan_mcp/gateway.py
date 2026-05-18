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

        VISION_URL can be set to a complete public capture URL for remote
        access setups such as Tailscale Funnel. Otherwise VISION_HOST should
        be the LAN IP of the host running this gateway, as seen from the ESP32
        (e.g. something like 192.168.x.y on a typical home network). Falls
        back to "127.0.0.1" with a warning if unset; in that case the ESP32
        will not be able to reach the capture endpoint over the network.
        """
        explicit_url = os.getenv("VISION_URL")
        if explicit_url:
            return explicit_url

        host = os.getenv("VISION_HOST")
        if not host:
            logger.warning(
                "VISION_URL/VISION_HOST not set; defaulting to 127.0.0.1. "
                "ESP32 will not reach the capture endpoint unless "
                "VISION_HOST is set to this host's LAN IP or VISION_URL is "
                "set to a full capture URL."
            )
            host = "127.0.0.1"
        port = int(os.getenv("CAPTURE_PORT", "8766"))
        return f"http://{host}:{port}/capture"

    @property
    def vision_token(self) -> str:
        """Bearer token expected by the capture endpoint.

        VISION_TOKEN can be set separately. By default, reuse the ESP32
        WebSocket token so remote capture uploads are protected whenever the
        gateway itself is protected.
        """
        return (
            os.getenv("VISION_TOKEN")
            or os.getenv("STACKCHAN_TOKEN")
            or os.getenv("BEARER_TOKEN")
            or ""
        )

    @property
    def audio_hook_url(self) -> str:
        """URL receiving device-driven listen captures as Ogg/Opus.

        STACKCHAN_AUDIO_HOOK_URL enables the device-driven listen
        capture path (wake word / button / LCD touch): the gateway
        packs inbound Opus frames into an Ogg container and POSTs to
        this URL on ``listen.stop``. The capture path is **disabled**
        when this is unset — stackchan-mcp's primary listen model
        remains MCP-client-driven (the ``listen()`` tool), and
        device-driven capture only makes sense when an external
        service is set up to receive the audio.
        """
        return os.getenv("STACKCHAN_AUDIO_HOOK_URL", "")

    @property
    def audio_hook_token(self) -> str:
        """Bearer token expected by the audio hook endpoint.

        STACKCHAN_AUDIO_HOOK_TOKEN can be set separately. Falls back to
        STACKCHAN_TOKEN so a single-token setup works out of the box.
        """
        return (
            os.getenv("STACKCHAN_AUDIO_HOOK_TOKEN")
            or os.getenv("STACKCHAN_TOKEN")
            or os.getenv("BEARER_TOKEN")
            or ""
        )

    async def start(self) -> None:
        """Start the ESP32 WebSocket server and HTTP capture server."""
        host = os.getenv("HOST", "0.0.0.0")
        ws_port = int(os.getenv("WS_PORT", os.getenv("PORT", "8765")))
        capture_port = int(os.getenv("CAPTURE_PORT", "8766"))

        # Start WebSocket server for ESP32
        await self.esp32.start(
            host,
            ws_port,
            vision_url=self.vision_url,
            vision_token=self.vision_token,
            audio_hook_url=self.audio_hook_url,
            audio_hook_token=self.audio_hook_token,
        )

        # Start HTTP capture server
        app = create_capture_app(capture_token=self.vision_token)
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
