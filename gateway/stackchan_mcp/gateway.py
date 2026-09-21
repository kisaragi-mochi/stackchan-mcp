"""Two-faced gateway: bridges MCP client (stdio MCP) and ESP32 (WebSocket MCP).

MCP client sees a standard MCP server via stdio.
ESP32 sees a WebSocket server that sends MCP client requests.
This module orchestrates both sides.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from aiohttp import web

from .capture_server import create_capture_app, stage_avatar_set
from .esp32_client import ESP32Connection, ESP32Manager
from .mdns_advertiser import MdnsAdvertiser

logger = logging.getLogger(__name__)

BEAT_MODE_LISTEN_STOP_TIMEOUT_S = 3.0
AVATAR_SET_PATH_ENV = "STACKCHAN_AVATAR_SET_PATH"
AVATAR_SET_MODE_ENV = "STACKCHAN_AVATAR_SET_MODE"
AVATAR_SET_TIMEOUT_ENV = "STACKCHAN_AVATAR_SET_TIMEOUT"
_AVATAR_FRAME_BYTES = 160 * 120 * 2
_AVATAR_LAYERED_BYTES = 14 * _AVATAR_FRAME_BYTES
_AVATAR_MATRIX_BYTES = 90 * _AVATAR_FRAME_BYTES


def resolve_configured_avatar_set() -> dict[str, Any] | None:
    """Read the optional connect-time avatar-set autoload from the environment.

    ``STACKCHAN_AVATAR_SET_PATH`` is required. Mode comes from
    ``STACKCHAN_AVATAR_SET_MODE`` when set, otherwise from an exact file
    size match (layered = 537,600 bytes, matrix = 3,456,000). Unknown
    sizes fail closed. Timeout defaults to 180 s for matrix and 60 s for
    layered; ``STACKCHAN_AVATAR_SET_TIMEOUT`` overrides that.
    """
    raw_path = os.getenv(AVATAR_SET_PATH_ENV, "").strip()
    if not raw_path:
        return None

    path = os.path.expanduser(raw_path)
    mode_env = os.getenv(AVATAR_SET_MODE_ENV, "").strip().lower()
    if mode_env in {"layered", "matrix"}:
        mode = mode_env
    elif not os.path.isfile(path):
        logger.warning(
            "%s=%r is set but the file is missing and %s is unset — "
            "skipping avatar-set autoload",
            AVATAR_SET_PATH_ENV,
            path,
            AVATAR_SET_MODE_ENV,
        )
        return None
    else:
        size = os.path.getsize(path)
        if size == _AVATAR_LAYERED_BYTES:
            mode = "layered"
        elif size == _AVATAR_MATRIX_BYTES:
            mode = "matrix"
        else:
            logger.warning(
                "%s=%r size=%d is neither layered (%d) nor matrix (%d) "
                "and %s is unset — skipping avatar-set autoload",
                AVATAR_SET_PATH_ENV,
                path,
                size,
                _AVATAR_LAYERED_BYTES,
                _AVATAR_MATRIX_BYTES,
                AVATAR_SET_MODE_ENV,
            )
            return None

    timeout_raw = os.getenv(AVATAR_SET_TIMEOUT_ENV, "").strip()
    if timeout_raw:
        try:
            timeout = float(timeout_raw)
        except ValueError:
            logger.warning(
                "invalid %s=%r — using mode default",
                AVATAR_SET_TIMEOUT_ENV,
                timeout_raw,
            )
            timeout = 180.0 if mode == "matrix" else 60.0
    else:
        timeout = 180.0 if mode == "matrix" else 60.0

    return {"path": path, "mode": mode, "timeout": timeout}


class Gateway:
    """Main gateway orchestrator.

    Holds the ESP32 manager and provides the bridge between
    the stdio MCP server (MCP client side) and the ESP32 device.

    Also runs an HTTP capture server for receiving photos from ESP32.
    """

    def __init__(self):
        self.esp32 = ESP32Manager()
        self._running = False
        self._http_runner: web.AppRunner | None = None
        # Phase 4.5 avatar: kept so load_avatar_set can stage payloads
        # against the same web.Application that serves /avatar_set/{id}.
        self._capture_app: web.Application | None = None
        self._mdns_advertiser: MdnsAdvertiser | None = None

    @property
    def vision_url(self) -> str:
        """URL for ESP32 to POST captured photos to.

        VISION_URL can be set to a complete public capture URL for remote
        access setups such as Tailscale Funnel. Otherwise VISION_HOST should
        be the LAN IP of the host running this gateway, as seen from the ESP32
        (e.g. something like 192.168.x.y on a typical home network). Falls
        back to "127.0.0.1" with a warning if unset; in that case the ESP32
        will not be able to reach the capture endpoint over the network.
        """
        explicit_url = os.getenv("VISION_URL")
        if explicit_url:
            return explicit_url

        host = os.getenv("VISION_HOST")
        if not host:
            logger.warning(
                "VISION_URL/VISION_HOST not set; defaulting to 127.0.0.1. "
                "ESP32 will not reach the capture endpoint unless "
                "VISION_HOST is set to this host's LAN IP or VISION_URL is "
                "set to a full capture URL."
            )
            host = "127.0.0.1"
        port = int(os.getenv("CAPTURE_PORT", "8766"))
        return f"http://{host}:{port}/capture"

    @property
    def vision_token(self) -> str:
        """Bearer token expected by the capture endpoint.

        VISION_TOKEN can be set separately. By default, reuse the ESP32
        WebSocket token so remote capture uploads are protected whenever the
        gateway itself is protected.
        """
        return (
            os.getenv("VISION_TOKEN")
            or os.getenv("STACKCHAN_TOKEN")
            or os.getenv("BEARER_TOKEN")
            or ""
        )

    @property
    def audio_hook_url(self) -> str:
        """URL receiving device-driven listen captures as Ogg/Opus.

        STACKCHAN_AUDIO_HOOK_URL enables the device-driven listen
        capture path (wake word / button / LCD touch). ``local``
        transcribes and speaks in this process on ``listen.stop``.
        Any other value is an HTTP URL: the gateway packs inbound
        Opus into Ogg and POSTs it there. The path is **disabled**
        when unset — stackchan-mcp's primary listen model remains
        MCP-client-driven (the ``listen()`` tool).
        """
        return os.getenv("STACKCHAN_AUDIO_HOOK_URL", "")

    @property
    def audio_hook_token(self) -> str:
        """Bearer token expected by the audio hook endpoint.

        STACKCHAN_AUDIO_HOOK_TOKEN can be set separately. Falls back to
        STACKCHAN_TOKEN so a single-token setup works out of the box.
        """
        return (
            os.getenv("STACKCHAN_AUDIO_HOOK_TOKEN")
            or os.getenv("STACKCHAN_TOKEN")
            or os.getenv("BEARER_TOKEN")
            or ""
        )

    @property
    def pcm_token(self) -> str:
        """Bearer token expected by the /pcm HTTP endpoint.

        Separate token from the ESP32 WebSocket / capture upload because
        the /pcm endpoint authorises external PCM producers (e.g. the
        SAIVerse voice-tts addon) — a different trust boundary from the
        device-to-gateway authentication. Falls back to STACKCHAN_TOKEN
        / BEARER_TOKEN when STACKCHAN_PCM_TOKEN is not configured so
        single-token local development keeps working.
        """
        return (
            os.getenv("STACKCHAN_PCM_TOKEN")
            or os.getenv("STACKCHAN_TOKEN")
            or os.getenv("BEARER_TOKEN")
            or ""
        )

    async def start(self, *, advertise_mdns: bool = True) -> None:
        """Start the ESP32 WebSocket server and HTTP capture server."""
        self.esp32.set_on_device_ready(self._autoload_configured_avatar_set)
        host = os.getenv("HOST", "0.0.0.0")
        ws_port = int(os.getenv("WS_PORT", os.getenv("PORT", "8765")))
        capture_port = int(os.getenv("CAPTURE_PORT", "8766"))

        # Start WebSocket server for ESP32
        await self.esp32.start(
            host,
            ws_port,
            vision_url=self.vision_url,
            vision_token=self.vision_token,
            audio_hook_url=self.audio_hook_url,
            audio_hook_token=self.audio_hook_token,
        )

        # Start HTTP capture server. Hosts /capture, /pcm, and the
        # Phase 4.5 avatar /avatar_set/{short_id} endpoint on the same
        # web.Application. The PCM endpoint forwards into
        # send_pcm_stream, so we hand it the active Gateway instance so
        # it can reach esp32 + tts_lock.
        app = create_capture_app(
            capture_token=self.vision_token,
            pcm_token=self.pcm_token,
            gateway=self,
        )
        self._capture_app = app
        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        site = web.TCPSite(self._http_runner, host, capture_port)
        await site.start()

        if advertise_mdns:
            self._mdns_advertiser = MdnsAdvertiser()
            try:
                await self._mdns_advertiser.start(host=host, port=ws_port, path="/")
            except Exception as exc:  # pragma: no cover - exact zeroconf errors vary by host
                logger.warning("mDNS advertisement failed: %s", exc)
                self._mdns_advertiser = None
        else:
            self._mdns_advertiser = None

        self._running = True
        logger.info(
            "Gateway started: WS on %s:%d, capture on %s:%d, vision_url=%s",
            host, ws_port, host, capture_port, self.vision_url,
        )

    async def stop(self) -> None:
        """Stop the gateway."""
        # Cancel any active pose-stream follower before the rest of the
        # shutdown sequence closes gateway-side services.
        try:
            from .follow_pose_stream import stop_follow

            await stop_follow()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("follow_pose_stream shutdown failed: %s", exc)

        # Likewise cancel any active LED-stream follower so its WiFi
        # power-save lease is released before services close.
        try:
            from .follow_led_stream import stop_follow as stop_led_follow

            await stop_led_follow()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("follow_led_stream shutdown failed: %s", exc)

        try:
            from .beat import stop_beat_mode

            await stop_beat_mode(
                listen_stop_timeout_s=BEAT_MODE_LISTEN_STOP_TIMEOUT_S,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("beat mode shutdown failed: %s", exc)

        self._running = False
        if self._mdns_advertiser:
            try:
                await self._mdns_advertiser.stop()
            except Exception as exc:  # pragma: no cover - exact zeroconf errors vary by host
                logger.warning("mDNS advertisement shutdown failed: %s", exc)
            finally:
                self._mdns_advertiser = None
        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None
        self._capture_app = None
        await self.esp32.stop()
        logger.info("Gateway stopped")

    # ---- Phase 4.5 avatar (saiverse-stackchan-addon) ------------------

    @property
    def avatar_set_base_url(self) -> str:
        """Base URL the device should hit for /avatar_set/{short_id}.

        Reuses vision_url so the device reaches this gateway over the
        same network path it already uses for camera POSTs (VISION_HOST
        / VISION_URL). The trailing /capture component is stripped.
        """
        url = self.vision_url
        if url.endswith("/capture"):
            return url[: -len("/capture")]
        return url

    async def load_avatar_set(
        self,
        archive_path: str,
        mode: str,
        timeout: float = 60.0,
    ) -> dict:
        """Stage an avatar set + notify the device + await its reply.

        See docs/intent/stackchan_avatar_pipeline.md §C in the SAIVerse
        repository for the protocol. ``archive_path`` is the path to a
        local file containing the raw RGB565 payload (gateway expects
        the addon to have already converted PNG/PIL output to RGB565).
        """
        if self._capture_app is None:
            return {"ok": False, "error": "gateway_not_started"}
        if not os.path.exists(archive_path):
            return {"ok": False, "error": f"archive_not_found: {archive_path}"}

        with open(archive_path, "rb") as f:
            payload = f.read()

        kimg_bytes = 160 * 120 * 2  # 38_400 — matches AvatarSet::kImageBytes
        expected = {
            "layered": 14 * kimg_bytes,   # 537_600
            "matrix":  90 * kimg_bytes,   # 3_456_000
        }.get(mode)
        if expected is None:
            return {"ok": False, "error": f"unknown_mode: {mode}"}
        if len(payload) != expected:
            return {
                "ok": False,
                "error": f"size_mismatch: got={len(payload)} expected={expected} (mode={mode})",
            }

        try:
            short_id, token, sha256 = await stage_avatar_set(
                self._capture_app, mode, payload
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        url = f"{self.avatar_set_base_url}/avatar_set/{short_id}"
        result = await self.esp32.send_avatar_set_fetch(
            url=url,
            token=token,
            mode=mode,
            checksum=sha256,
            expected_size=len(payload),
            timeout=timeout,
        )
        # Surface the staging metadata for caller-side observability.
        result.setdefault("checksum", sha256)
        result["bytes_transferred"] = len(payload) if result.get("ok") else 0
        return result

    async def _autoload_configured_avatar_set(
        self,
        _connection: ESP32Connection,
        device_id: str,
    ) -> None:
        """Reload STACKCHAN_AVATAR_SET_PATH after every device init.

        Custom sets live in PSRAM and vanish on reboot. This hook runs
        on connect — including reconnect after the gateway was already
        up — so the configured face comes back without a manual
        ``load_avatar_set``. Failures are logged and do not block idle
        render or the rest of device init. When an avatar set is
        configured, blink is also re-enabled after the load attempt
        (firmware starts with blink off).
        """
        config = resolve_configured_avatar_set()
        if config is None:
            return
        logger.info(
            "auto-loading avatar set: device=%s path=%s mode=%s timeout=%.0fs",
            device_id,
            config["path"],
            config["mode"],
            config["timeout"],
        )
        result = await self.load_avatar_set(
            config["path"],
            config["mode"],
            config["timeout"],
        )
        if not result.get("ok"):
            logger.warning(
                "auto-loading avatar set failed: device=%s error=%s",
                device_id,
                result.get("error", result),
            )
        await self._enable_connect_blink(device_id)

    async def _enable_connect_blink(self, device_id: str) -> None:
        """Turn autonomous blink on after a configured avatar-set autoload."""
        try:
            _result, error = await self.esp32.call_tool(
                "self.display.set_blink",
                {"enabled": True},
            )
        except Exception as exc:
            logger.warning(
                "auto-enabling blink failed: device=%s error=%s",
                device_id,
                exc,
            )
            return
        if error:
            logger.warning(
                "auto-enabling blink failed: device=%s error=%s",
                device_id,
                error,
            )
            return
        logger.info("auto-enabled blink: device=%s", device_id)


# Singleton gateway instance, shared between stdio server and ESP32 manager
_gateway: Gateway | None = None


def get_gateway() -> Gateway:
    """Get or create the singleton gateway."""
    global _gateway
    if _gateway is None:
        _gateway = Gateway()
    return _gateway
