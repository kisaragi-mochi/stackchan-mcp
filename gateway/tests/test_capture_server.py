"""Tests for HTTP capture upload helpers."""

from stackchan_mcp.capture_server import (
    CAPTURE_TOKEN_KEY,
    _is_authorized,
    create_capture_app,
)


def test_capture_app_stores_capture_token():
    """Capture app keeps the expected bearer token in app state."""
    app = create_capture_app(capture_token="capture-token")

    assert app[CAPTURE_TOKEN_KEY] == "capture-token"


def test_is_authorized_accepts_matching_bearer():
    """Bearer auth must match exactly."""
    assert _is_authorized("Bearer capture-token", "capture-token") is True


def test_is_authorized_rejects_missing_or_wrong_bearer():
    """Missing or mismatched bearer auth is rejected."""
    assert _is_authorized("", "capture-token") is False
    assert _is_authorized("Bearer wrong-token", "capture-token") is False
