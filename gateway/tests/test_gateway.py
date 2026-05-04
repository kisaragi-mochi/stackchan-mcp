"""Tests for gateway module."""

import pytest

from stackchan_mcp.gateway import Gateway, get_gateway


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
    await gw.start()
    assert gw._running is True
    assert gw.esp32._server is not None

    await gw.stop()
    assert gw._running is False
