"""Tests for Stack-chan event notification dispatch modes."""

from pathlib import Path

import pytest

from stackchan_mcp import esp32_client, event_log, stdio_server
from stackchan_mcp.esp32_client import ESP32Manager
from stackchan_mcp.notify_config import (
    DEFAULT_MESSAGE_TEMPLATES,
    MessageTemplate,
    NotifyConfig,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy_enabled", "channels_enabled", "jsonl_enabled"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
async def test_emit_stackchan_event_dispatches_selected_paths(
    monkeypatch,
    caplog,
    legacy_enabled,
    channels_enabled,
    jsonl_enabled,
):
    notify_calls: list[tuple[str, dict]] = []
    log_calls: list[dict] = []

    async def fake_notify(method, params):
        notify_calls.append((method, params))

    def fake_log_event(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(stdio_server, "notify_stackchan_event", fake_notify)
    monkeypatch.setattr(event_log, "log_event", fake_log_event)
    monkeypatch.setattr(esp32_client.time, "time", lambda: 1717000000.25)

    jsonl_path = Path("/tmp/stackchan-events-test.jsonl")
    manager = ESP32Manager(
        notify_config=_notify_config(
            legacy=legacy_enabled,
            channels=channels_enabled,
            jsonl=jsonl_enabled,
            jsonl_path=jsonl_path,
        )
    )

    with caplog.at_level("INFO"):
        await manager._emit_stackchan_event(_payload())

    expected_notify_methods = []
    if legacy_enabled:
        expected_notify_methods.append(stdio_server.STACKCHAN_EVENT_METHOD)
    if channels_enabled:
        expected_notify_methods.append(stdio_server.CHANNEL_NOTIFICATION_METHOD)
    assert [method for method, _ in notify_calls] == expected_notify_methods

    if legacy_enabled:
        legacy_params = dict(notify_calls[0][1])
        assert legacy_params == _expected_meta()
        assert legacy_params["action"] == "head_pat"

    if channels_enabled:
        channel_index = expected_notify_methods.index(stdio_server.CHANNEL_NOTIFICATION_METHOD)
        channel_params = notify_calls[channel_index][1]
        assert channel_params == {
            "content": "(head pat)",
            "meta": _expected_meta(),
        }
        assert channel_params["meta"]["action"] == "head_pat"

    if jsonl_enabled:
        assert log_calls == [
            {
                "event_type": "touch",
                "subtype": "tap",
                "duration_ms": 350,
                "ts": 123456,
                "session_id": "session-1",
                "action": "head_pat",
                "path": jsonl_path,
                "ts_unix": 1717000000.25,
            }
        ]
    else:
        assert log_calls == []

    if not (legacy_enabled or channels_enabled or jsonl_enabled):
        assert notify_calls == []
        assert "received and dropped" in caplog.text


@pytest.mark.asyncio
async def test_custom_message_overrides_action_and_channel_content(monkeypatch):
    notify_calls: list[tuple[str, dict]] = []

    async def fake_notify(method, params):
        notify_calls.append((method, params))

    monkeypatch.setattr(stdio_server, "notify_stackchan_event", fake_notify)
    monkeypatch.setattr(esp32_client.time, "time", lambda: 1717000000.25)

    messages = dict(DEFAULT_MESSAGE_TEMPLATES)
    messages[("touch", "tap")] = MessageTemplate(
        action="head_knock",
        template="(head knock, {duration_ms}ms)",
    )
    manager = ESP32Manager(
        notify_config=_notify_config(
            legacy=False,
            channels=True,
            jsonl=False,
            messages=messages,
        )
    )

    await manager._emit_stackchan_event(_payload())

    assert notify_calls == [
        (
            stdio_server.CHANNEL_NOTIFICATION_METHOD,
            {
                "content": "(head knock, 350ms)",
                "meta": {
                    **_expected_meta(),
                    "action": "head_knock",
                },
            },
        )
    ]


def _notify_config(
    *,
    legacy: bool,
    channels: bool,
    jsonl: bool,
    jsonl_path: Path = Path("/tmp/stackchan-events-test.jsonl"),
    messages: dict[tuple[str, str], MessageTemplate] | None = None,
) -> NotifyConfig:
    return NotifyConfig(
        legacy_event_enabled=legacy,
        channels_enabled=channels,
        jsonl_enabled=jsonl,
        jsonl_path=jsonl_path,
        messages=messages or dict(DEFAULT_MESSAGE_TEMPLATES),
    )


def _payload() -> dict:
    return {
        "event_type": "touch",
        "subtype": "tap",
        "duration_ms": 350,
        "ts": 123456,
        "session_id": "session-1",
    }


def _expected_meta() -> dict:
    return {
        "event_type": "touch",
        "subtype": "tap",
        "duration_ms": 350,
        "action": "head_pat",
        "ts": 123456,
        "ts_unix": 1717000000.25,
        "session_id": "session-1",
    }
