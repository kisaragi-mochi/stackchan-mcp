"""Gateway ownership lock - refuse-mode MVP (#177 Phase A)."""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

LOCK_DIR = Path.home() / ".stackchan-mcp"
LOCK_PATH = LOCK_DIR / "owner.lock"


LockMode = Literal["stdio", "streamable-http"]


class _BaseLockInfo(TypedDict):
    owner_id: str
    pid: int
    start_ts: str
    host: str


class LockInfo(_BaseLockInfo, total=False):
    mode: LockMode
    http_endpoint: str | None
    started_by: str | None


class OwnershipError(RuntimeError):
    """Raised when ownership cannot be acquired."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_owner_id() -> str:
    env = os.environ.get("STACKCHAN_OWNER_ID")
    if env:
        return env
    return f"stackchan-mcp-{uuid.uuid4().hex[:8]}"


def is_pid_alive(pid: int) -> bool:
    """Return whether pid is alive without disturbing the target process.

    On Windows, os.kill(pid, 0) calls TerminateProcess(..., 0), which would
    kill the target process; use a non-destructive Win32 API check instead.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_pid_alive_windows(pid: int) -> bool:
    import ctypes
    import ctypes.wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False

    try:
        exit_code = ctypes.wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_lock(path: Path = LOCK_PATH) -> LockInfo | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(raw, dict):
        return None

    owner_id = raw.get("owner_id")
    pid = raw.get("pid")
    start_ts = raw.get("start_ts")
    host = raw.get("host")
    if (
        not isinstance(owner_id, str)
        or not isinstance(pid, int)
        or not isinstance(start_ts, str)
        or not isinstance(host, str)
    ):
        return None

    info: LockInfo = {
        "owner_id": owner_id,
        "pid": pid,
        "start_ts": start_ts,
        "host": host,
    }

    # Optional metadata: silently ignore unknown or malformed values so a
    # schema-drift writer (for example a future-mode lock file or a
    # partially compatible external writer) cannot cause read_lock to
    # return None and trick acquire_lock into unlinking a live owner's
    # lock file. The four required #177 base fields above are
    # authoritative for the claim/refuse decision; validating optional
    # metadata is a separate diagnostic concern.

    mode = raw.get("mode")
    if mode in ("stdio", "streamable-http"):
        info["mode"] = mode

    if "http_endpoint" in raw:
        http_endpoint = raw["http_endpoint"]
        if http_endpoint is None or isinstance(http_endpoint, str):
            info["http_endpoint"] = http_endpoint

    if "started_by" in raw:
        started_by = raw["started_by"]
        if started_by is None or isinstance(started_by, str):
            info["started_by"] = started_by

    return info


def _write_lock_atomic(info: LockInfo, path: Path = LOCK_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
    try:
        # Link the complete temp file into place only if no owner file exists.
        # This keeps readers from seeing partial JSON and lets exactly one
        # simultaneous startup win the initial claim.
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def acquire_lock(
    owner_id: str,
    path: Path = LOCK_PATH,
    *,
    mode: LockMode = "stdio",
    http_endpoint: str | None = None,
    started_by: str | None = None,
) -> LockInfo:
    """Acquire the ownership lock. Raise OwnershipError on refuse.

    The default stdio-mode call writes the original #177 lock shape so
    older lock readers and ``stackchan-mcp --check`` output remain
    compatible. Daemon transports can attach optional metadata for
    diagnostics without changing the atomic hardlink claim.
    """
    if mode not in ("stdio", "streamable-http"):
        raise ValueError(f"unsupported lock mode: {mode!r}")
    if mode == "stdio" and (http_endpoint is not None or started_by is not None):
        raise ValueError(
            "stdio-mode ownership locks must not carry http_endpoint or "
            "started_by; these fields are reserved for non-stdio transports"
        )

    while True:
        existing = read_lock(path)
        if existing is not None:
            if is_pid_alive(existing["pid"]):
                raise OwnershipError(
                    "stackchan-mcp: device already owned by "
                    f"{existing['owner_id']} "
                    f"(pid {existing['pid']}, since {existing['start_ts']})"
                )
            print(
                f"stackchan-mcp: removed stale lock from dead pid {existing['pid']}",
                file=sys.stderr,
            )
            path.unlink(missing_ok=True)
        elif path.exists():
            path.unlink()

        info: LockInfo = {
            "owner_id": owner_id,
            "pid": os.getpid(),
            "start_ts": _now_iso(),
            "host": socket.gethostname(),
        }
        if mode != "stdio":
            info["mode"] = mode
        if http_endpoint is not None:
            info["http_endpoint"] = http_endpoint
        if started_by is not None:
            info["started_by"] = started_by

        try:
            _write_lock_atomic(info, path)
        except FileExistsError:
            continue
        return info


def release_lock(path: Path = LOCK_PATH) -> None:
    """Remove the lock file. Idempotent.

    This is the legacy, owner-unaware release primitive kept for backward
    compatibility. New callers should prefer :func:`release_lock_if_owner`
    so that a stale cleanup callback cannot unlink a successor process's
    live lock.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def release_lock_if_owner(info: LockInfo, path: Path = LOCK_PATH) -> bool:
    """Remove the lock file only if it still belongs to ``info``.

    Returns ``True`` if the lock was removed, ``False`` if the on-disk
    lock has a different ``owner_id`` / ``pid`` / ``start_ts`` or no
    longer exists. This is the owner-scoped counterpart to
    :func:`release_lock` and is intended for cleanup paths (``finally``
    blocks, ``atexit.register``) where the caller may have lost ownership
    between claim and cleanup — for example after the gateway exited and
    a second process acquired the lock before the first process's
    interpreter exit callbacks ran.
    """
    existing = read_lock(path)
    if existing is None:
        return False
    if (
        existing.get("owner_id") != info.get("owner_id")
        or existing.get("pid") != info.get("pid")
        or existing.get("start_ts") != info.get("start_ts")
    ):
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
