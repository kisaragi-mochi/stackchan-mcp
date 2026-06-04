"""Tests for stackchan/event server notifications."""

import json
import logging

import pytest

from stackchan_mcp.esp32_client import ESP32Manager
import stackchan_mcp.stdio_server as stdio_server
from stackchan_mcp.stdio_server import (
    STACKCHAN_EVENT_INSTRUCTIONS,
    STACKCHAN_EVENT_METHOD,
    _create_initialization_options,
    create_server,
    notify_stackchan_event,
)


@pytest.mark.asyncio
async def test_stackchan_event_frame_dispatches_to_notify_bridge(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_notify(method, params):
        calls.append((method, params))

    monkeypatch.setattr(stdio_server, "notify_stackchan_event", fake_notify)
    manager = ESP32Manager()

    await manager._handler(
        _FakeWebSocket(
            [
                json.dumps(
                    {
                        "session_id": "session-1",
                        "type": "stackchan-event",
                        "event_type": "touch",
                        "subtype": "tap",
                        "duration_ms": 350,
                        "ts": 123456,
                    }
                )
            ]
        )
    )

    assert calls == [
        (
            STACKCHAN_EVENT_METHOD,
            {
                "event_type": "touch",
                "subtype": "tap",
                "duration_ms": 350,
                "ts": 123456,
                "session_id": "session-1",
            },
        )
    ]


@pytest.mark.parametrize(
    ("overrides", "warning"),
    [
        ({"event_type": "motion"}, "event_type='motion'"),
        ({"subtype": "press"}, "subtype='press'"),
        ({"duration_ms": "350"}, "duration_ms='350'"),
        ({"duration_ms": True}, "duration_ms=True"),
        ({"duration_ms": -1}, "duration_ms=-1"),
        ({"ts": "123456"}, "ts='123456'"),
        ({"ts": True}, "ts=True"),
        ({"ts": -1}, "ts=-1"),
        ({"session_id": ""}, "session_id=''"),
        ({"session_id": None}, "session_id=None"),
    ],
)
@pytest.mark.asyncio
async def test_stackchan_event_malformed_frame_warns_without_notify(
    monkeypatch,
    caplog,
    overrides,
    warning,
):
    calls = []

    async def fake_notify(method, params):
        calls.append((method, params))

    monkeypatch.setattr(stdio_server, "notify_stackchan_event", fake_notify)
    manager = ESP32Manager()
    payload = {
        "session_id": "session-1",
        "type": "stackchan-event",
        "event_type": "touch",
        "subtype": "tap",
        "duration_ms": 350,
        "ts": 123456,
    }
    payload.update(overrides)

    with caplog.at_level(logging.WARNING):
        await manager._emit_stackchan_event(payload)

    assert calls == []
    assert f"Malformed stackchan-event frame: {warning}" in caplog.text


def test_stackchan_event_capability_and_instructions_are_declared():
    server = create_server()
    options = _create_initialization_options(server)

    assert options.capabilities.experimental == {STACKCHAN_EVENT_METHOD: {}}
    assert options.instructions == STACKCHAN_EVENT_INSTRUCTIONS


@pytest.mark.asyncio
async def test_server_run_captures_active_session_before_first_message(monkeypatch):
    observed = []

    class FakeIncomingMessages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            observed.append(stdio_server._active_session)
            raise StopAsyncIteration

    class FakeServerSession:
        def __init__(self, *args, **kwargs):
            self.incoming_messages = FakeIncomingMessages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(stdio_server, "ServerSession", FakeServerSession)
    server = create_server()

    await server.run(None, None, _create_initialization_options(server))

    assert len(observed) == 1
    assert isinstance(observed[0], FakeServerSession)
    assert stdio_server._active_session is None


@pytest.mark.asyncio
async def test_notify_stackchan_event_uses_active_session(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(stdio_server, "_active_session", session)

    params = {
        "event_type": "touch",
        "subtype": "stroke",
        "duration_ms": 900,
        "ts": 222,
        "session_id": "session-1",
    }
    await notify_stackchan_event(STACKCHAN_EVENT_METHOD, params)

    assert session.notifications == [
        {
            "method": STACKCHAN_EVENT_METHOD,
            "params": params,
        }
    ]


@pytest.mark.asyncio
async def test_notify_stackchan_event_without_active_session_logs_and_returns(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(stdio_server, "_active_session", None)

    with caplog.at_level(logging.WARNING):
        await notify_stackchan_event(
            STACKCHAN_EVENT_METHOD,
            {
                "event_type": "touch",
                "subtype": "tap",
                "duration_ms": 350,
                "ts": 123,
                "session_id": "session-1",
            },
        )

    assert "no active MCP session" in caplog.text


class _FakeWebSocket:
    request = None

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        return None


class _FakeSession:
    def __init__(self):
        self.notifications = []

    async def send_notification(self, notification):
        self.notifications.append(
            notification.model_dump(
                by_alias=True,
                mode="json",
                exclude_none=True,
            )
        )
