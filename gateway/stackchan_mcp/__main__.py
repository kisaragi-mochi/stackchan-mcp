"""Entry point: python -m stackchan_mcp

Starts the two-faced gateway:
- stdio MCP server (MCP client side)
- WebSocket server (ESP32 side)
"""

import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


async def _run() -> None:
    """Start both the ESP32 WebSocket server and the stdio MCP server."""
    from .gateway import get_gateway
    from .stdio_server import run_stdio_server

    gateway = get_gateway()

    # Start ESP32 WebSocket server
    await gateway.start()
    logger.info("Gateway started, waiting for ESP32 connections...")

    try:
        # Run stdio MCP server (blocks until MCP client disconnects)
        await run_stdio_server()
    finally:
        await gateway.stop()


def main() -> None:
    asyncio.run(_run())


main()
