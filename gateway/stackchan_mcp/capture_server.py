"""HTTP capture server for receiving photos from ESP32.

ESP32's camera.Explain() POSTs multipart/form-data with:
- field 'question' (text)
- field 'file' (camera.jpg, JPEG image)

This server saves the JPEG and returns the file path so MCP client
can view the image via the Read tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass

from aiohttp import web

logger = logging.getLogger(__name__)

CAPTURE_DIR = os.path.expanduser("~/.stackchan/captures")
CAPTURE_TOKEN_KEY = web.AppKey("capture_token", str)

# Phase 4.5 avatar (saiverse-stackchan-addon): in-memory staging for
# one-time avatar set downloads. See docs/intent/stackchan_avatar_pipeline.md
# §C-2 in the SAIVerse repository.
AVATAR_SETS_KEY = web.AppKey("avatar_sets", dict)
AVATAR_SETS_LOCK_KEY = web.AppKey("avatar_sets_lock", asyncio.Lock)

# A staging entry is GC'd if it hasn't been fetched within this window.
AVATAR_SET_STAGING_TTL_SEC = 120.0


@dataclass(frozen=True)
class _AvatarStaging:
    token: str
    mode: str
    payload: bytes
    sha256: str
    created_at: float


def _is_authorized(auth_header: str, expected_token: str) -> bool:
    """Return whether the bearer auth header matches the expected token."""
    return auth_header == f"Bearer {expected_token}"


async def handle_capture(request: web.Request) -> web.Response:
    """Handle photo upload from ESP32."""
    expected_token = request.app[CAPTURE_TOKEN_KEY]
    if expected_token and not _is_authorized(
        request.headers.get("Authorization", ""), expected_token
    ):
        logger.warning("Capture upload auth rejected")
        return web.Response(
            text='{"error": "Unauthorized"}',
            status=401,
            content_type="application/json",
        )

    os.makedirs(CAPTURE_DIR, exist_ok=True)

    reader = await request.multipart()
    question = ""
    image_path = ""

    async for part in reader:
        if part.name == "question":
            question = (await part.read()).decode("utf-8")
        elif part.name == "file":
            timestamp = int(time.time() * 1000)
            filename = f"capture_{timestamp}.jpg"
            image_path = os.path.join(CAPTURE_DIR, filename)
            with open(image_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(8192)
                    if not chunk:
                        break
                    f.write(chunk)

    if image_path and os.path.exists(image_path):
        file_size = os.path.getsize(image_path)
        logger.info(
            "Captured photo: %s (%d bytes), question: %s",
            image_path,
            file_size,
            question,
        )
        result = json.dumps({
            "image_path": image_path,
            "size_bytes": file_size,
            "question": question,
        })
        return web.Response(text=result, content_type="application/json")

    return web.Response(
        text='{"error": "No image received"}',
        status=400,
        content_type="application/json",
    )


async def stage_avatar_set(
    app: web.Application,
    mode: str,
    payload: bytes,
) -> tuple[str, str, str]:
    """Stage an avatar set for one-time HTTP download.

    Returns (short_id, token, sha256). The caller hands these to the
    device via WS avatar_set_fetch; the device performs a GET against
    /avatar_set/{short_id} with Authorization: Bearer <token>.

    The staging entry is consumed on the first successful fetch and
    GC'd after AVATAR_SET_STAGING_TTL_SEC if never fetched.
    """
    if mode not in ("layered", "matrix"):
        raise ValueError(f"unknown avatar mode: {mode}")

    short_id = secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()

    staging = _AvatarStaging(
        token=token,
        mode=mode,
        payload=payload,
        sha256=sha256,
        created_at=time.time(),
    )

    sets = app[AVATAR_SETS_KEY]
    async with app[AVATAR_SETS_LOCK_KEY]:
        # Best-effort GC of stale entries before inserting.
        now = time.time()
        expired = [
            k for k, v in sets.items()
            if now - v.created_at > AVATAR_SET_STAGING_TTL_SEC
        ]
        for k in expired:
            sets.pop(k, None)
        sets[short_id] = staging

    logger.info(
        "Staged avatar set: short_id=%s mode=%s bytes=%d sha256=%s",
        short_id, mode, len(payload), sha256,
    )
    return short_id, token, sha256


async def handle_avatar_set_fetch(request: web.Request) -> web.Response:
    """Serve a staged avatar set (one-time)."""
    short_id = request.match_info.get("short_id", "")
    if not short_id:
        return web.Response(status=400, text="missing short_id")

    sets = request.app[AVATAR_SETS_KEY]
    # Validate the request fully (existence, TTL, auth) before consuming
    # the staged entry. An unauthenticated probe must not be able to
    # invalidate a legitimate transfer just by guessing the short_id,
    # and a real fetch that fails auth due to a transient header issue
    # must still find the entry on retry.
    async with request.app[AVATAR_SETS_LOCK_KEY]:
        staging = sets.get(short_id)
        if staging is None:
            return web.Response(status=404, text="not_found_or_consumed")

        if time.time() - staging.created_at > AVATAR_SET_STAGING_TTL_SEC:
            # Expired — drop the slot so it doesn't linger.
            sets.pop(short_id, None)
            return web.Response(status=410, text="staging_expired")

        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {staging.token}":
            logger.warning(
                "Avatar set fetch auth rejected for short_id=%s", short_id
            )
            return web.Response(status=401, text="unauthorized")

        # Auth confirmed: consume the one-time entry now.
        sets.pop(short_id, None)

    logger.info(
        "Serving avatar set: short_id=%s mode=%s bytes=%d",
        short_id, staging.mode, len(staging.payload),
    )
    return web.Response(
        body=staging.payload,
        content_type="application/octet-stream",
        headers={
            "X-Avatar-Mode": staging.mode,
            "X-Avatar-Sha256": staging.sha256,
            "Content-Length": str(len(staging.payload)),
        },
    )


def create_capture_app(capture_token: str = "") -> web.Application:
    """Create the HTTP capture application."""
    app = web.Application()
    app[CAPTURE_TOKEN_KEY] = capture_token
    app[AVATAR_SETS_KEY] = {}
    app[AVATAR_SETS_LOCK_KEY] = asyncio.Lock()
    app.router.add_post("/capture", handle_capture)
    app.router.add_get("/avatar_set/{short_id}", handle_avatar_set_fetch)
    return app
