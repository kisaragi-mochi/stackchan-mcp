"""Console entry point for stackchan-mcp.

This module exists so that `import stackchan_mcp` (or any of its
submodules) does not trigger import-time side effects like
`load_dotenv()` or logging configuration. All such side effects live
inside :func:`main`, which is registered as the `stackchan-mcp`
console script in ``pyproject.toml`` and is also re-exported through
``stackchan_mcp.__main__`` so that ``python -m stackchan_mcp`` keeps
working.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def _run() -> None:
    """Start both the ESP32 WebSocket server and the stdio MCP server."""
    from .gateway import get_gateway
    from .stdio_server import run_stdio_server

    gateway = get_gateway()

    await gateway.start()
    logger.info("Gateway started, waiting for ESP32 connections...")

    try:
        # Run stdio MCP server (blocks until MCP client disconnects)
        await run_stdio_server()
    finally:
        await gateway.stop()


def main() -> None:
    """Console-script entry point.

    Loads ``.env``, configures logging, and starts the gateway. Side
    effects are intentionally scoped to this function so that
    ``import stackchan_mcp`` stays clean.
    """
    from dotenv import load_dotenv

    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
