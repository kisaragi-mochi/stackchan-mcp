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
# raises if it returns None). That means we need the DLL search path
# update to have run before *any* code imports opuslib, no matter
# which subpackage of stackchan_mcp loads first. The package
# `__init__.py` is the only place guaranteed to run before all
# sibling submodules.
#
# Why we update BOTH `os.add_dll_directory()` AND `os.environ["PATH"]`:
# - `os.add_dll_directory()` is the modern, isolated mechanism used by
#   `LoadLibraryEx(..., LOAD_LIBRARY_SEARCH_USER_DIRS)`. Importantly,
#   `ctypes.util.find_library()` on Windows uses the legacy
#   `LoadLibraryW()` path which does **not** consult the
#   `add_dll_directory()` list (see CPython issue #43603). Since
#   `opuslib/api/__init__.py` calls exactly that — `find_library('opus')`
#   — we also have to prepend the directory to PATH so the legacy
#   resolver picks it up.
# - We add to `add_dll_directory()` too because direct `ctypes.CDLL(...)`
#   / extension-module imports use the modern resolver, and we want
#   bundle discovery to work for both API styles future-proof.
#
# See `stackchan_mcp/_libs/SOURCES.md` for the bundled DLL provenance.
if _sys.platform == "win32":
    _libs_dir = _Path(__file__).resolve().parent / "_libs"
    if _libs_dir.is_dir():
        _os.add_dll_directory(str(_libs_dir))
        _libs_str = str(_libs_dir)
        _existing_path = _os.environ.get("PATH", "")
        if _libs_str not in _existing_path.split(_os.pathsep):
            _os.environ["PATH"] = _libs_str + _os.pathsep + _existing_path
