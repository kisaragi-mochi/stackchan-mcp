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
import errno
import logging
import os
import shutil
import socket
import subprocess
import sys

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
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Run a non-destructive preflight (configuration, port "
            "availability, derived URLs) and exit. Exit 0 if ready to run, "
            "non-zero if at least one blocking issue is found."
        ),
    )
    return parser


# --- Preflight diagnostics (--check) ----------------------------------------
#
# The preflight is intentionally side-effect-free: it loads ``.env``, reads
# environment variables, attempts non-blocking ``bind()`` calls to the two
# server ports, and prints a concise human-readable report. It does NOT
# reach out to any ESP32, does not start either server, and does not modify
# any files. Live device connectivity belongs in a future ``status``
# subcommand (Issue #54 "Out of scope" note).


_BIND_ERROR_PREFIX = "bind error: "


def _check_port(host: str, port: int) -> tuple[bool, str | None]:
    """Probe ``(host, port)`` by trying to ``bind()`` to it.

    Returns ``(available, info)``:

    - ``(True, None)``: bind succeeded, port is free.
    - ``(False, "pid <N>, <cmd>")``: bind failed with ``EADDRINUSE`` and
      ``lsof`` identified the holder.
    - ``(False, None)``: bind failed with ``EADDRINUSE`` but ``lsof`` is
      unavailable / the lookup failed.
    - ``(False, "bind error: <reason>")``: bind failed for a non-
      ``EADDRINUSE`` reason (for example, ``EADDRNOTAVAIL`` when ``HOST``
      is not actually assigned to this machine, or ``EACCES`` on a
      privileged port without permission). Distinguishing this from
      "in use" prevents users from looking for a phantom process when
      the real problem is the bind address.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Deliberately skip SO_REUSEADDR: we want bind to fail when the
    # port is genuinely held by another process, not silently succeed.
    try:
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return (False, _try_get_port_holder(port))
            reason = exc.strerror or (
                os.strerror(exc.errno) if exc.errno is not None else str(exc)
            )
            return (False, f"{_BIND_ERROR_PREFIX}{reason}")
    finally:
        sock.close()
    return (True, None)


def _try_get_port_holder(port: int) -> str | None:
    """Best-effort lookup of the process holding ``port`` via ``lsof``.

    Returns ``"pid <N>, <cmd>"`` on success, or ``None`` if ``lsof`` is
    not installed, the call fails, or the port is not in fact held (for
    example, the bind failure was due to a permission error rather than
    EADDRINUSE).
    """
    if shutil.which("lsof") is None:
        return None
    try:
        result = subprocess.run(
            ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpcn"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    pid: str | None = None
    cmd: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c"):
            cmd = line[1:]
    if pid and cmd:
        return f"pid {pid}, {cmd}"
    if pid:
        return f"pid {pid}"
    return None


def _format_port_status(available: bool, holder: str | None) -> str:
    if available:
        return "AVAILABLE"
    if holder is None:
        return "IN USE"
    if holder.startswith(_BIND_ERROR_PREFIX):
        # Don't say "IN USE" for non-EADDRINUSE bind failures
        # (EADDRNOTAVAIL, EACCES, etc.). Surface the actual reason
        # instead so the user does not chase a phantom process.
        reason = holder.removeprefix(_BIND_ERROR_PREFIX)
        return f"BIND ERROR ({reason})"
    return f"IN USE ({holder})"


_TCP_PORT_RANGE = range(0, 65536)


def _validate_port_value(raw: str, var: str) -> tuple[int | None, str]:
    """Parse ``raw`` as a TCP port, returning ``(port, source_or_error)``.

    Returns ``(int_value, var)`` for a valid in-range integer (0..65535
    inclusive — ``0`` lets the OS pick, which the gateway may not
    actually want but is at least bind-able). Returns ``(None, "<var>=
    <raw> (...)")`` otherwise; the caller treats that as a blocking
    issue rather than silently falling through to a default.

    Both branches matter for the preflight: ``socket.bind()`` raises
    ``OverflowError`` for values outside the TCP port range, so without
    this validation ``--check`` would crash with a stack trace instead
    of producing the diagnostic report it exists to produce.
    """
    try:
        value = int(raw)
    except ValueError:
        return (None, f"{var}={raw!r} (not an integer)")
    if value not in _TCP_PORT_RANGE:
        return (None, f"{var}={raw!r} (out of TCP port range 0-65535)")
    return (value, var)


def _resolve_ws_port() -> tuple[int | None, str]:
    """Resolve the WebSocket port using the same precedence as ``gateway.py``.

    Mirrors ``int(os.getenv("WS_PORT", os.getenv("PORT", "8765")))`` from
    ``gateway.py`` so the preflight checks the port the gateway will
    actually bind, not a hard-coded default. See ``_validate_port_value``
    for the validation rules; on success returns ``(port, "WS_PORT")``
    or ``(port, "PORT")``, otherwise ``(None, "<var>=<raw> (...)")``.
    """
    for var in ("WS_PORT", "PORT"):
        raw = os.getenv(var)
        if raw is None:
            continue
        return _validate_port_value(raw, var)
    return (8765, "default")


def _resolve_capture_port() -> tuple[int | None, str]:
    """Resolve the HTTP capture port using ``gateway.py``'s precedence.

    Mirrors ``int(os.getenv("CAPTURE_PORT", "8766"))``. See
    ``_validate_port_value`` for the validation rules.
    """
    raw = os.getenv("CAPTURE_PORT")
    if raw is None:
        return (8766, "default")
    return _validate_port_value(raw, "CAPTURE_PORT")


def _load_dotenv() -> None:
    """Lazy ``.env`` loader exposed as a single attachable seam.

    Wrapping ``python-dotenv`` here keeps two properties:

    1. ``import stackchan_mcp.cli`` stays side-effect free (the
       ``dotenv`` import only happens when the gateway / preflight is
       actually invoked).
    2. Tests can ``monkeypatch.setattr(cli, "_load_dotenv", ...)`` to
       prevent the real ``find_dotenv()`` walking up to the developer's
       ``gateway/.env`` and contaminating environment-isolation tests.
    """
    from dotenv import load_dotenv

    load_dotenv()


def _run_preflight() -> int:
    """Run preflight diagnostics. Returns the desired process exit code.

    Output is intentionally fixed-width and grep-friendly. Exit 0 means
    "ready to run"; non-zero means at least one blocking issue (currently
    only port unavailability). Missing optional configuration is reported
    but does not fail the check, mirroring how the gateway itself only
    warns about a missing ``STACKCHAN_TOKEN``.
    """
    _load_dotenv()

    issues = 0
    print(f"stackchan-mcp {__version__} preflight")
    print()

    # --- Configuration ------------------------------------------------------
    print("Configuration:")
    token = os.getenv("STACKCHAN_TOKEN") or os.getenv("BEARER_TOKEN")
    if token:
        print("  STACKCHAN_TOKEN     set (***redacted***)")
    else:
        print("  STACKCHAN_TOKEN     not set (gateway will accept any client)")

    vision_host = os.getenv("VISION_HOST", "")
    capture_port_raw = os.getenv("CAPTURE_PORT", "8766")
    if vision_host:
        print(f"  VISION_HOST         {vision_host}")
    else:
        print("  VISION_HOST         not set")

    vision_url_explicit = os.getenv("VISION_URL", "")
    if vision_url_explicit:
        print(f"  VISION_URL          {vision_url_explicit}")
    elif vision_host:
        derived = f"http://{vision_host}:{capture_port_raw}/capture"
        print(f"  VISION_URL          (derived) {derived}")
    else:
        print(
            "  VISION_URL          not set "
            "(set VISION_HOST or VISION_URL for take_photo)"
        )

    if os.getenv("VISION_TOKEN"):
        print("  VISION_TOKEN        set (***redacted***)")
    else:
        print("  VISION_TOKEN        not set (will reuse STACKCHAN_TOKEN)")

    # --- Ports --------------------------------------------------------------
    print()
    print("Ports:")
    host = os.getenv("HOST", "0.0.0.0")
    ws_port, ws_source = _resolve_ws_port()
    cap_port, cap_source = _resolve_capture_port()

    if ws_port is None:
        print(f"  ws://{host}:???     INVALID ({ws_source})")
        issues += 1
    if cap_port is None:
        print(f"  http://{host}:???   INVALID ({cap_source})")
        issues += 1

    if ws_port is not None and cap_port is not None and ws_port == cap_port:
        # The gateway runs WebSocket and HTTP capture as separate
        # listeners; binding the WebSocket server first will then make
        # the HTTP bind fail. Independent _check_port probes can't see
        # this on their own (each one binds-and-releases), so we surface
        # the conflict explicitly.
        print(
            f"  WS_PORT ({ws_source}) and CAPTURE_PORT ({cap_source}) "
            f"both resolve to {ws_port}; the gateway needs distinct ports."
        )
        issues += 1

    if ws_port is not None:
        ws_available, ws_holder = _check_port(host, ws_port)
        print(
            f"  ws://{host}:{ws_port}   "
            f"{_format_port_status(ws_available, ws_holder)}"
        )
        if not ws_available:
            issues += 1

    if cap_port is not None:
        cap_available, cap_holder = _check_port(host, cap_port)
        print(
            f"  http://{host}:{cap_port} "
            f"{_format_port_status(cap_available, cap_holder)}"
        )
        if not cap_available:
            issues += 1

    # --- Result -------------------------------------------------------------
    print()
    if issues == 0:
        print("Result: ready. Exit 0.")
        return 0
    plural = "s" if issues > 1 else ""
    print(f"Result: {issues} issue{plural}. Exit 1.")
    return 1


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

    Parses ``--help`` / ``--version`` / ``--check`` early (without
    starting the server), then loads ``.env``, configures logging, and
    starts the gateway. Side effects are intentionally scoped to this
    function so that ``import stackchan_mcp`` stays clean.
    """
    parser = _build_arg_parser()
    # argparse exits with status 0 on --help / --version before reaching
    # any of the gateway start-up below, which is the intended behaviour.
    args = parser.parse_args(argv)

    if args.check:
        # ``_run_preflight`` loads ``.env`` itself; do not double-load
        # via the path below.
        sys.exit(_run_preflight())

    _load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
