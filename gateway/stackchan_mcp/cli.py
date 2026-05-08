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

import argparse
import asyncio
import logging

from . import __version__

logger = logging.getLogger(__name__)


_DESCRIPTION = (
    "stdio MCP gateway for the StackChan / xiaozhi-esp32 firmware. "
    "Bridges stdio MCP clients (for example Claude Code) to a StackChan "
    "ESP32 device over WebSocket, and exposes an HTTP capture endpoint "
    "for photo uploads from the device."
)

_EPILOG = """\
Environment variables:
  STACKCHAN_TOKEN   Bearer token shared with the ESP32 firmware.
  VISION_URL        Full public capture URL (e.g. Tailscale Funnel).
  VISION_HOST       LAN IP of this machine, as seen from the ESP32.
  VISION_TOKEN      Optional separate token for VISION_URL uploads.
  HOST              Bind address for the ESP32 WebSocket server (default 0.0.0.0).
  WS_PORT           Port for the ESP32 WebSocket server (default 8765).
  CAPTURE_PORT      Port for the HTTP capture server (default 8766).

See gateway/README.md and the top-level README.md for full setup,
including pairing the ESP32 firmware and configuring the WiFi gateway URL.
"""


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stackchan-mcp",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


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


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point.

    Parses ``--help`` / ``--version`` early (without side effects), then
    loads ``.env``, configures logging, and starts the gateway. Side
    effects are intentionally scoped to this function so that
    ``import stackchan_mcp`` stays clean.
    """
    parser = _build_arg_parser()
    # argparse exits with status 0 on --help / --version before reaching
    # any of the gateway start-up below, which is the intended behaviour.
    parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
