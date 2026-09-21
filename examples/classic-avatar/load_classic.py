#!/usr/bin/env python3
"""Build the classic matrix if needed and load it onto a connected device."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE / "classic-matrix.rgb565"
MCP = "http://127.0.0.1:8767/mcp"
SESSION = "mcp-session-id"


def req(payload: dict, session: str | None = None, timeout: float = 200) -> tuple[dict, bytes]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if session:
        headers[SESSION] = session
    request = urllib.request.Request(
        MCP, data=json.dumps(payload).encode(), method="POST"
    )
    for key, value in headers.items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return dict(resp.headers), resp.read()


def result_text(raw: bytes) -> str:
    data = json.loads(raw.decode())
    if "error" in data:
        return json.dumps(data["error"])
    content = data.get("result", {}).get("content", [])
    return content[0].get("text", raw.decode()[:400]) if content else raw.decode()[:400]


def ensure_binary() -> None:
    if BIN.exists() and BIN.stat().st_size == 90 * 160 * 120 * 2:
        return
    subprocess.check_call([sys.executable, str(HERE / "make_classic.py")], cwd=HERE)


def main() -> int:
    ensure_binary()
    headers, _ = req(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "classic-avatar-load", "version": "1"},
            },
        }
    )
    session = headers.get(SESSION) or headers.get("Mcp-Session-Id")
    if not session:
        print("gateway initialize failed: no session", file=sys.stderr)
        return 1
    req({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    _, raw = req(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "load_avatar_set",
                "arguments": {
                    "archive_path": str(BIN),
                    "mode": "matrix",
                    "timeout": 180,
                },
            },
        },
        session,
    )
    print(result_text(raw))
    _, raw = req(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "set_avatar", "arguments": {"face": "idle"}},
        },
        session,
    )
    print(result_text(raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
