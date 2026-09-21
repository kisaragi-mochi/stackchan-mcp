#!/usr/bin/env python3
"""Local receiver for device-driven listen captures (screen tap).

The gateway POSTs Ogg/Opus to this process when the robot starts and
stops listening on its own. We transcribe with faster-whisper and
speak a reply through the gateway ``say`` tool.

    export STACKCHAN_AUDIO_HOOK_URL=http://127.0.0.1:8780/audio
    # restart the gateway, then:
    uv run --extra stt-faster-whisper python examples/audio-hook-receiver/receive.py
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

MCP = os.getenv("STACKCHAN_MCP_URL", "http://127.0.0.1:8767/mcp")
HOST = os.getenv("STACKCHAN_AUDIO_HOOK_BIND", "127.0.0.1")
PORT = int(os.getenv("STACKCHAN_AUDIO_HOOK_PORT", "8780"))
LANGUAGE = os.getenv("STACKCHAN_LISTEN_LANGUAGE", "ja").strip() or "ja"
MODEL = os.getenv("STACKCHAN_FASTER_WHISPER_MODEL", "base")
DEVICE = os.getenv("STACKCHAN_FASTER_WHISPER_DEVICE", "cpu")
COMPUTE = os.getenv("STACKCHAN_FASTER_WHISPER_COMPUTE_TYPE", "int8")
HOOK_TOKEN = (
    os.getenv("STACKCHAN_AUDIO_HOOK_TOKEN")
    or os.getenv("STACKCHAN_TOKEN")
    or ""
).strip()
SESSION_HEADER = "mcp-session-id"

logger = logging.getLogger("audio-hook-receiver")
_model = None
_model_lock = threading.Lock()
_work_lock = threading.Lock()


def reply_text(transcript: str) -> str:
    text = transcript.strip()
    if not text:
        return "I did not catch that."
    return f"You said: {text}"


def _mcp(payload: dict[str, Any], session: str | None = None, timeout: float = 60) -> tuple[dict, dict]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if session:
        headers[SESSION_HEADER] = session
    request = Request(MCP, data=json.dumps(payload).encode(), method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    with urlopen(request, timeout=timeout) as resp:
        raw = resp.read()
        body = json.loads(raw.decode()) if raw else {}
        return dict(resp.headers), body


def say(text: str) -> None:
    headers, _ = _mcp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "audio-hook-receiver", "version": "1"},
            },
        }
    )
    session = headers.get(SESSION_HEADER) or headers.get("Mcp-Session-Id")
    if not session:
        raise RuntimeError("gateway initialize failed: no session")
    _mcp({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    _mcp(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "set_avatar", "arguments": {"face": "happy"}},
        },
        session,
    )
    _mcp(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "say", "arguments": {"text": text}},
        },
        session,
        timeout=120,
    )


def load_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            logger.info("loading faster-whisper model=%s device=%s", MODEL, DEVICE)
            _model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE)
        return _model


def transcribe(ogg: bytes) -> str:
    model = load_model()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(ogg)
        path = tmp.name
    try:
        segments, _info = model.transcribe(path, language=LANGUAGE, beam_size=1)
        return "".join(seg.text for seg in segments).strip()
    finally:
        Path(path).unlink(missing_ok=True)


def authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not HOOK_TOKEN:
        return True
    expected = f"Bearer {HOOK_TOKEN}"
    return handler.headers.get("Authorization", "") == expected


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if self.path.rstrip("/") != "/healthz":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/audio":
            self.send_error(404)
            return
        if not authorized(self):
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        ogg = self.rfile.read(length) if length else b""
        session = self.headers.get("X-StackChan-Session", "")
        if not ogg:
            self.send_error(400, "empty body")
            return

        if not _work_lock.acquire(blocking=False):
            self.send_error(429, "busy")
            return
        try:
            transcript = transcribe(ogg)
            spoken = reply_text(transcript)
            logger.info(
                "transcript session=%s text=%r",
                session,
                transcript,
            )
            try:
                say(spoken)
            except Exception:
                logger.exception("say() failed")
                self.send_error(502, "say failed")
                return
        except Exception:
            logger.exception("transcribe failed")
            self.send_error(500, "transcribe failed")
            return
        finally:
            _work_lock.release()

        body = json.dumps({"ok": True, "text": transcript, "said": spoken}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info("audio hook listening on http://%s:%d/audio", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
