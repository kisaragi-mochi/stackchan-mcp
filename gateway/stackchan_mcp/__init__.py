"""stackchan-mcp: Two-faced gateway for StackChan (xiaozhi-esp32).

MCP client side: stdio MCP server (mcp Python SDK)
ESP32 side: WebSocket server (MCP client over JSON-RPC 2.0)
"""

import os as _os
import sys as _sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path as _Path

try:
    __version__ = version("stackchan-mcp")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0+unknown"

# Windows: register the bundled native libs directory with the DLL
# search path before any submodule pulls in `opuslib` (or any other
# wrapper that calls `ctypes.util.find_library`). On Linux/macOS the
# system package manager typically already provides libopus, so we do
# nothing on those platforms.
#
# Why this is here and not in tts/__init__.py or stt/__init__.py:
# opuslib's libopus lookup happens at import time (the wrapper's
# top-level module unconditionally calls `find_library('opus')` and
# raises if it returns None). That means we need `add_dll_directory`
# to have run before *any* code imports opuslib, no matter which
# subpackage of stackchan_mcp loads first. The package `__init__.py`
# is the only place guaranteed to run before all sibling submodules.
#
# See `stackchan_mcp/_libs/SOURCES.md` for the bundled DLL provenance.
if _sys.platform == "win32":
    _libs_dir = _Path(__file__).resolve().parent / "_libs"
    if _libs_dir.is_dir():
        _os.add_dll_directory(str(_libs_dir))
