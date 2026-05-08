"""stackchan-mcp: Two-faced gateway for StackChan (xiaozhi-esp32).

MCP client side: stdio MCP server (mcp Python SDK)
ESP32 side: WebSocket server (MCP client over JSON-RPC 2.0)
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("stackchan-mcp")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0+unknown"
