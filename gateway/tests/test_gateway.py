"""Tests for gateway module."""

import asyncio
from types import SimpleNamespace

import pytest

from stackchan_mcp.audio_stream import is_recording, stop_recording
from stackchan_mcp.beat import mode as beat_mode
from stackchan_mcp.beat.mode import BeatModeConfig
from stackchan_mcp.gateway import Gateway, get_gateway


def _patch_gateway_network(monkeypatch: pytest.MonkeyPatch, gw: Gateway) -> list[tuple]:
    """Replace real listeners with fakes so gateway lifecycle tests avoid bind()."""
    import stackchan_mcp.gateway as gw_mod

    calls: list[tuple] = []

    class FakeEsp32:
        def __init__(self) -> None:
            self._server = None

        async def start(
            self,
            host: str,
            port: int,
            *,
            vision_url: str,
            vision_token: str,
            audio_hook_url: str = "",
            audio_hook_token: str = "",
        ) -> None:
            self._server = object()
            calls.append(("esp32_start", host, port, vision_url, vision_token))

        def set_on_device_ready(self, callback) -> None:
            self._on_device_ready = callback

        async def stop(self) -> None:
            self._server = None
            calls.append(("esp32_stop",))

    class FakeAppRunner:
        def __init__(self, app) -> None:
            self.app = app

        async def setup(self) -> None:
            calls.append(("http_setup",))

        async def cleanup(self) -> None:
            calls.append(("http_cleanup",))

    class FakeTCPSite:
        def __init__(self, runner, host: str, port: int) -> None:
            self.runner = runner
            self.host = host
            self.port = port

        async def start(self) -> None:
            calls.append(("http_start", self.host, self.port))

    gw.esp32 = FakeEsp32()
    monkeypatch.setattr(
        gw_mod,
        "create_capture_app",
        lambda capture_token="", pcm_token="", gateway=None: object(),
    )
    monkeypatch.setattr(gw_mod.web, "AppRunner", FakeAppRunner)
    monkeypatch.setattr(gw_mod.web, "TCPSite", FakeTCPSite)
    return calls


def test_get_gateway_singleton():
    """get_gateway returns the same instance."""
    # Reset singleton for test isolation
    import stackchan_mcp.gateway as gw_mod
    gw_mod._gateway = None

    g1 = get_gateway()
    g2 = get_gateway()
    assert g1 is g2

    # Cleanup
    gw_mod._gateway = None


def test_vision_url_uses_explicit_url(monkeypatch):
    """VISION_URL overrides host/port construction for remote tunnels."""
    monkeypatch.setenv("VISION_URL", "https://stackchan.example.ts.net:8443/capture")
    monkeypatch.setenv("VISION_HOST", "192.0.2.10")
    monkeypatch.setenv("CAPTURE_PORT", "8766")

    gw = Gateway()

    assert gw.vision_url == "https://stackchan.example.ts.net:8443/capture"


def test_vision_url_uses_lan_host(monkeypatch):
    """VISION_HOST and CAPTURE_PORT still build the default LAN capture URL."""
    monkeypatch.delenv("VISION_URL", raising=False)
    monkeypatch.setenv("VISION_HOST", "192.0.2.10")
    monkeypatch.setenv("CAPTURE_PORT", "8766")

    gw = Gateway()

    assert gw.vision_url == "http://192.0.2.10:8766/capture"


def test_vision_token_prefers_explicit_token(monkeypatch):
    """VISION_TOKEN can be separated from the WebSocket token."""
    monkeypatch.setenv("VISION_TOKEN", "capture-token")
    monkeypatch.setenv("STACKCHAN_TOKEN", "ws-token")

    gw = Gateway()

    assert gw.vision_token == "capture-token"


def test_vision_token_falls_back_to_stackchan_token(monkeypatch):
    """Capture uploads use the gateway token by default."""
    monkeypatch.delenv("VISION_TOKEN", raising=False)
    monkeypatch.setenv("STACKCHAN_TOKEN", "ws-token")
    monkeypatch.setenv("BEARER_TOKEN", "legacy-token")

    gw = Gateway()

    assert gw.vision_token == "ws-token"


@pytest.mark.asyncio
async def test_gateway_start_stop(monkeypatch):
    """Gateway can start and stop."""
    monkeypatch.setenv("WS_PORT", "0")  # Random port
    monkeypatch.setenv("CAPTURE_PORT", "0")  # Random port

    gw = Gateway()
    calls = _patch_gateway_network(monkeypatch, gw)

    await gw.start(advertise_mdns=False)
    assert gw._running is True
    assert gw.esp32._server is not None
    assert ("http_start", "0.0.0.0", 0) in calls

    await gw.stop()
    assert gw._running is False
    assert ("http_cleanup",) in calls
    assert ("esp32_stop",) in calls


@pytest.mark.asyncio
async def test_gateway_stop_stops_active_beat_mode_before_esp32_transport(
    monkeypatch,
):
    events: list[tuple[str, str | None] | tuple[str]] = []

    class FakeStreamingOpusDecoder:
        def decode_frame(self, frame: bytes) -> bytes:
            return frame

    class FakeEsp32:
        def __init__(self) -> None:
            self.device_connected = True
            self.connection = SimpleNamespace(
                protocol_version=1,
                session_id="gateway-stop-beat",
            )
            self.listen_lock = asyncio.Lock()

        async def send_listen_state(
            self,
            state: str,
            mode: str = "manual",
            profile: str = "voice",
        ) -> None:
            events.append(("listen", state))

        async def call_tool(
            self,
            name: str,
            arguments: dict,
        ) -> tuple[dict, None]:
            events.append(("tool", name))
            return {"ok": True}, None

        async def stop(self) -> None:
            events.append(("esp32_stop",))

    monkeypatch.setattr(beat_mode, "StreamingOpusDecoder", FakeStreamingOpusDecoder)

    gw = Gateway()
    gw.esp32 = FakeEsp32()

    try:
        await beat_mode.start_beat_mode(gw, BeatModeConfig())
        mode = beat_mode._mode
        assert mode is not None
        assert is_recording()

        await gw.stop()

        assert events == [
            ("listen", "start"),
            ("listen", "stop"),
            ("esp32_stop",),
        ]
        assert not is_recording()
        assert mode._tasks == []
        assert mode.status()["active"] is False
    finally:
        await beat_mode.stop_beat_mode()
        if is_recording():
            stop_recording()
        beat_mode._mode = None


@pytest.mark.asyncio
async def test_gateway_stop_completes_when_beat_listen_stop_hangs(
    monkeypatch,
    caplog,
):
    import stackchan_mcp.gateway as gw_mod

    events: list[tuple[str, str | None] | tuple[str]] = []
    stop_started = asyncio.Event()
    stop_cancelled = asyncio.Event()
    release_stop = asyncio.Event()

    class FakeStreamingOpusDecoder:
        def decode_frame(self, frame: bytes) -> bytes:
            return frame

    class FakeEsp32:
        def __init__(self) -> None:
            self.device_connected = True
            self.connection = SimpleNamespace(
                protocol_version=1,
                session_id="gateway-stop-beat-timeout",
            )
            self.listen_lock = asyncio.Lock()

        async def send_listen_state(
            self,
            state: str,
            mode: str = "manual",
            profile: str = "voice",
        ) -> None:
            events.append(("listen", state))
            if state == "stop":
                stop_started.set()
                try:
                    await release_stop.wait()
                except asyncio.CancelledError:
                    stop_cancelled.set()
                    raise

        async def call_tool(
            self,
            name: str,
            arguments: dict,
        ) -> tuple[dict, None]:
            events.append(("tool", name))
            return {"ok": True}, None

        async def stop(self) -> None:
            events.append(("esp32_stop",))

    monkeypatch.setattr(beat_mode, "StreamingOpusDecoder", FakeStreamingOpusDecoder)
    monkeypatch.setattr(gw_mod, "BEAT_MODE_LISTEN_STOP_TIMEOUT_S", 0.01)

    gw = Gateway()
    gw.esp32 = FakeEsp32()

    try:
        await beat_mode.start_beat_mode(gw, BeatModeConfig())
        mode = beat_mode._mode
        assert mode is not None
        assert is_recording()

        with caplog.at_level("WARNING"):
            await asyncio.wait_for(gw.stop(), timeout=0.25)

        assert stop_started.is_set()
        assert stop_cancelled.is_set()
        assert events == [
            ("listen", "start"),
            ("listen", "stop"),
            ("esp32_stop",),
        ]
        assert not is_recording()
        assert mode._tasks == []
        assert mode.status()["active"] is False
        assert mode.status()["capture_state"] == "stopped"
        assert gw._running is False
        assert "listen.stop timed out" in caplog.text
    finally:
        release_stop.set()
        await beat_mode.stop_beat_mode(listen_stop_timeout_s=0.01)
        if is_recording():
            stop_recording()
        beat_mode._mode = None


@pytest.mark.asyncio
async def test_gateway_start_advertises_mdns_by_default(monkeypatch):
    """Gateway.start() starts mDNS advertising after listeners are ready."""
    import stackchan_mcp.gateway as gw_mod

    calls = []

    class FakeAdvertiser:
        async def start(self, *, host: str, port: int, path: str = "/") -> None:
            calls.append(("start", host, port, path))

        async def stop(self) -> None:
            calls.append(("stop",))

    monkeypatch.setenv("WS_PORT", "0")
    monkeypatch.setenv("CAPTURE_PORT", "0")
    monkeypatch.setattr(gw_mod, "MdnsAdvertiser", FakeAdvertiser)

    gw = Gateway()
    _patch_gateway_network(monkeypatch, gw)
    await gw.start()

    assert calls == [("start", "0.0.0.0", 0, "/")]
    assert gw._running is True

    await gw.stop()
    assert calls == [("start", "0.0.0.0", 0, "/"), ("stop",)]


@pytest.mark.asyncio
async def test_gateway_start_can_disable_mdns(monkeypatch):
    """Gateway.start(advertise_mdns=False) skips mDNS advertising."""
    import stackchan_mcp.gateway as gw_mod

    class FailAdvertiser:
        def __init__(self) -> None:
            raise AssertionError("MdnsAdvertiser should not be constructed")

    monkeypatch.setenv("WS_PORT", "0")
    monkeypatch.setenv("CAPTURE_PORT", "0")
    monkeypatch.setattr(gw_mod, "MdnsAdvertiser", FailAdvertiser)

    gw = Gateway()
    _patch_gateway_network(monkeypatch, gw)
    await gw.start(advertise_mdns=False)

    assert gw._mdns_advertiser is None

    await gw.stop()


@pytest.mark.asyncio
async def test_gateway_mdns_start_failure_does_not_abort(
    monkeypatch, caplog
):
    """mDNS registration failure logs a warning but gateway startup continues."""
    import stackchan_mcp.gateway as gw_mod

    class FailingAdvertiser:
        async def start(self, *, host: str, port: int, path: str = "/") -> None:
            raise RuntimeError("mock mdns failure")

        async def stop(self) -> None:
            raise AssertionError("failed start should not leave advertiser active")

    monkeypatch.setenv("WS_PORT", "0")
    monkeypatch.setenv("CAPTURE_PORT", "0")
    monkeypatch.setattr(gw_mod, "MdnsAdvertiser", FailingAdvertiser)

    gw = Gateway()
    _patch_gateway_network(monkeypatch, gw)
    with caplog.at_level("WARNING"):
        await gw.start()

    assert gw._running is True
    assert gw._mdns_advertiser is None
    assert "mDNS advertisement failed" in caplog.text

    await gw.stop()


@pytest.mark.asyncio
async def test_gateway_mdns_stop_failure_does_not_mask_shutdown(
    monkeypatch, caplog
):
    """mDNS unregister failure logs a warning and shutdown still completes."""
    import stackchan_mcp.gateway as gw_mod

    class FailingStopAdvertiser:
        async def start(self, *, host: str, port: int, path: str = "/") -> None:
            return None

        async def stop(self) -> None:
            raise RuntimeError("mock mdns stop failure")

    monkeypatch.setenv("WS_PORT", "0")
    monkeypatch.setenv("CAPTURE_PORT", "0")
    monkeypatch.setattr(gw_mod, "MdnsAdvertiser", FailingStopAdvertiser)

    gw = Gateway()
    _patch_gateway_network(monkeypatch, gw)
    await gw.start()

    with caplog.at_level("WARNING"):
        await gw.stop()

    assert gw._running is False
    assert gw._mdns_advertiser is None
    assert gw.esp32._server is None
    assert "mDNS advertisement shutdown failed" in caplog.text


def test_resolve_configured_avatar_set_unset(monkeypatch):
    from stackchan_mcp.gateway import resolve_configured_avatar_set

    monkeypatch.delenv("STACKCHAN_AVATAR_SET_PATH", raising=False)
    assert resolve_configured_avatar_set() is None


def test_resolve_configured_avatar_set_infers_mode(monkeypatch, tmp_path):
    from stackchan_mcp import gateway as gw_mod
    from stackchan_mcp.gateway import (
        _AVATAR_LAYERED_BYTES,
        _AVATAR_MATRIX_BYTES,
        resolve_configured_avatar_set,
    )

    monkeypatch.delenv("STACKCHAN_AVATAR_SET_MODE", raising=False)
    monkeypatch.delenv("STACKCHAN_AVATAR_SET_TIMEOUT", raising=False)

    matrix = tmp_path / "classic.rgb565"
    matrix.write_text("")
    layered = tmp_path / "layered.rgb565"
    layered.write_text("")
    sizes = {
        str(matrix): _AVATAR_MATRIX_BYTES,
        str(layered): _AVATAR_LAYERED_BYTES,
    }
    monkeypatch.setattr(gw_mod.os.path, "getsize", lambda path: sizes[path])

    monkeypatch.setenv("STACKCHAN_AVATAR_SET_PATH", str(matrix))
    assert resolve_configured_avatar_set() == {
        "path": str(matrix),
        "mode": "matrix",
        "timeout": 180.0,
    }

    monkeypatch.setenv("STACKCHAN_AVATAR_SET_PATH", str(layered))
    assert resolve_configured_avatar_set() == {
        "path": str(layered),
        "mode": "layered",
        "timeout": 60.0,
    }


def test_resolve_configured_avatar_set_honours_overrides(monkeypatch, tmp_path):
    from stackchan_mcp.gateway import resolve_configured_avatar_set

    missing = tmp_path / "missing.rgb565"
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_PATH", f"~/{missing.name}")
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_MODE", "layered")
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_TIMEOUT", "90")
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = resolve_configured_avatar_set()
    assert resolved == {
        "path": str(tmp_path / missing.name),
        "mode": "layered",
        "timeout": 90.0,
    }


def test_resolve_configured_avatar_set_fails_closed_on_unknown_size(
    monkeypatch, tmp_path, caplog
):
    from stackchan_mcp.gateway import resolve_configured_avatar_set

    weird = tmp_path / "weird.rgb565"
    weird.write_bytes(b"x" * 1234)
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_PATH", str(weird))
    monkeypatch.delenv("STACKCHAN_AVATAR_SET_MODE", raising=False)
    monkeypatch.delenv("STACKCHAN_AVATAR_SET_TIMEOUT", raising=False)

    caplog.set_level("WARNING")
    assert resolve_configured_avatar_set() is None
    assert "neither layered" in caplog.text


def test_resolve_configured_avatar_set_fails_closed_when_missing_without_mode(
    monkeypatch, tmp_path, caplog
):
    from stackchan_mcp.gateway import resolve_configured_avatar_set

    missing = tmp_path / "missing.rgb565"
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_PATH", str(missing))
    monkeypatch.delenv("STACKCHAN_AVATAR_SET_MODE", raising=False)

    caplog.set_level("WARNING")
    assert resolve_configured_avatar_set() is None
    assert "file is missing" in caplog.text


class _BlinkEsp32:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return {"ok": True}, None


@pytest.mark.asyncio
async def test_autoload_configured_avatar_set_skips_when_unset(monkeypatch):
    from stackchan_mcp.gateway import Gateway

    monkeypatch.delenv("STACKCHAN_AVATAR_SET_PATH", raising=False)
    gw = Gateway()
    gw.esp32 = _BlinkEsp32()  # type: ignore[assignment]
    called = False

    async def boom(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("load_avatar_set should not run")

    gw.load_avatar_set = boom  # type: ignore[method-assign]
    await gw._autoload_configured_avatar_set(None, "device-test")  # type: ignore[arg-type]
    assert called is False
    assert gw.esp32.calls == []


@pytest.mark.asyncio
async def test_autoload_configured_avatar_set_loads_and_logs_failure(
    monkeypatch, tmp_path, caplog
):
    from stackchan_mcp.gateway import Gateway

    archive = tmp_path / "classic.rgb565"
    archive.write_bytes(b"x")
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_PATH", str(archive))
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_MODE", "matrix")
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_TIMEOUT", "120")

    gw = Gateway()
    gw.esp32 = _BlinkEsp32()  # type: ignore[assignment]
    calls: list[tuple] = []

    async def fake_load(path: str, mode: str, timeout: float = 60.0):
        calls.append((path, mode, timeout))
        return {"ok": False, "error": "device_timeout"}

    gw.load_avatar_set = fake_load  # type: ignore[method-assign]
    caplog.set_level("WARNING")
    await gw._autoload_configured_avatar_set(None, "device-test")  # type: ignore[arg-type]

    assert calls == [(str(archive), "matrix", 120.0)]
    assert "auto-loading avatar set failed" in caplog.text
    assert "device_timeout" in caplog.text
    assert gw.esp32.calls == [("self.display.set_blink", {"enabled": True})]


@pytest.mark.asyncio
async def test_autoload_blink_failure_does_not_raise(monkeypatch, tmp_path, caplog):
    from stackchan_mcp.gateway import Gateway

    archive = tmp_path / "classic.rgb565"
    archive.write_bytes(b"x")
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_PATH", str(archive))
    monkeypatch.setenv("STACKCHAN_AVATAR_SET_MODE", "matrix")
    gw = Gateway()

    async def fake_load(*_args, **_kwargs):
        return {"ok": True}

    class BoomEsp32:
        async def call_tool(self, _name: str, _arguments: dict):
            raise RuntimeError("no device")

    gw.load_avatar_set = fake_load  # type: ignore[method-assign]
    gw.esp32 = BoomEsp32()  # type: ignore[assignment]
    caplog.set_level("WARNING")
    await gw._autoload_configured_avatar_set(None, "device-test")  # type: ignore[arg-type]
    assert "auto-enabling blink failed" in caplog.text
    assert "no device" in caplog.text
