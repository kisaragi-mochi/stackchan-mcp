"""Tests for HTTP capture upload helpers + /pcm endpoint."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict

from stackchan_mcp.capture_server import (
    CAPTURE_TOKEN_KEY,
    GATEWAY_KEY,
    PCM_TOKEN_KEY,
    _is_authorized,
    create_capture_app,
    handle_pcm,
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


# ---------------------------------------------------------------------------
# /pcm endpoint — state storage
# ---------------------------------------------------------------------------


def test_capture_app_stores_pcm_token_and_gateway():
    """Create app with pcm_token + gateway keeps both in app state.

    Mirrors the existing capture_token storage test so the /pcm
    authentication and gateway dispatch state is observable from
    integration tests.
    """
    fake_gateway = object()
    app = create_capture_app(
        capture_token="capture-token",
        pcm_token="pcm-token",
        gateway=fake_gateway,
    )

    assert app[PCM_TOKEN_KEY] == "pcm-token"
    assert app[GATEWAY_KEY] is fake_gateway


def test_capture_app_pcm_token_defaults_to_empty():
    """Omitting pcm_token / gateway keeps the legacy single-arg form working.

    Callers that haven't migrated to /pcm should still get a usable app
    for /capture-only deployments.
    """
    app = create_capture_app(capture_token="capture-token")

    assert app[PCM_TOKEN_KEY] == ""
    assert app[GATEWAY_KEY] is None


# ---------------------------------------------------------------------------
# /pcm endpoint — request validation
# ---------------------------------------------------------------------------


def _make_pcm_request(
    app, headers: dict[str, str] | None = None
) -> object:
    """Build a mocked POST /pcm request bound to ``app`` with ``headers``."""
    return make_mocked_request(
        "POST",
        "/pcm",
        headers=CIMultiDict(headers or {}),
        app=app,
    )


@pytest.mark.asyncio
async def test_pcm_rejects_missing_bearer():
    """No Authorization header → 401."""
    app = create_capture_app(pcm_token="secret", gateway=object())
    request = _make_pcm_request(app)

    response = await handle_pcm(request)

    assert response.status == 401
    assert json.loads(response.text) == {"error": "Unauthorized"}


@pytest.mark.asyncio
async def test_pcm_rejects_wrong_bearer():
    """Mismatched token → 401."""
    app = create_capture_app(pcm_token="secret", gateway=object())
    request = _make_pcm_request(
        app, headers={"Authorization": "Bearer wrong"}
    )

    response = await handle_pcm(request)

    assert response.status == 401


@pytest.mark.asyncio
async def test_pcm_rejects_missing_sample_rate():
    """No X-Sample-Rate → 400 with explanatory error."""
    app = create_capture_app(pcm_token="secret", gateway=object())
    request = _make_pcm_request(
        app, headers={"Authorization": "Bearer secret"}
    )

    response = await handle_pcm(request)

    assert response.status == 400
    error = json.loads(response.text)["error"]
    assert "X-Sample-Rate" in error


@pytest.mark.asyncio
async def test_pcm_rejects_invalid_sample_rate():
    """Non-integer X-Sample-Rate → 400."""
    app = create_capture_app(pcm_token="secret", gateway=object())
    request = _make_pcm_request(
        app,
        headers={
            "Authorization": "Bearer secret",
            "X-Sample-Rate": "not-a-number",
        },
    )

    response = await handle_pcm(request)

    assert response.status == 400


@pytest.mark.asyncio
async def test_pcm_rejects_multi_channel():
    """Multi-channel input → 400 (only mono supported, see send_pcm_stream)."""
    app = create_capture_app(pcm_token="secret", gateway=object())
    request = _make_pcm_request(
        app,
        headers={
            "Authorization": "Bearer secret",
            "X-Sample-Rate": "32000",
            "X-Channels": "2",
        },
    )

    response = await handle_pcm(request)

    assert response.status == 400
    error = json.loads(response.text)["error"]
    assert "mono" in error.lower()


@pytest.mark.asyncio
async def test_pcm_returns_503_when_no_gateway():
    """gateway=None → 503 so callers know to retry once it's up."""
    app = create_capture_app(pcm_token="secret", gateway=None)
    request = _make_pcm_request(
        app,
        headers={
            "Authorization": "Bearer secret",
            "X-Sample-Rate": "32000",
        },
    )

    response = await handle_pcm(request)

    assert response.status == 503


@pytest.mark.asyncio
async def test_pcm_no_auth_allows_unauthed_request_when_token_blank():
    """pcm_token="" disables auth, matching the gateway's fallback policy.

    Same shape as the existing /capture behaviour (`if expected_token`
    branch). Lets ad-hoc local development work without juggling tokens.
    The request still needs to fail validation later (missing
    sample-rate / no gateway), but the 401 path is bypassed.
    """
    app = create_capture_app(pcm_token="", gateway=None)
    request = _make_pcm_request(app)  # no Authorization

    response = await handle_pcm(request)

    # We don't auth-reject (would be 401); the next guard (sample-rate)
    # fires first, so 400 here means auth was skipped as intended.
    assert response.status == 400
