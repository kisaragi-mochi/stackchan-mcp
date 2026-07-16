"""Command-queue watchdog tests: head-of-queue timeout + saturation shedding.

Covers the two failure modes the watchdog exists for (a hung dispatch
stalling every queued command; a saturated queue rejecting one-shot
commands while cosmetic LED traffic occupies it) plus the JSONL event
trail both interventions leave.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

import stackchan_mcp.queue as queue_module
from stackchan_mcp.event_log import PATH_ENV_VAR
from stackchan_mcp.http_server import build_app
from stackchan_mcp.queue import (
    DROPPED_ERROR_CODE,
    HEAD_TIMEOUT_ERROR_CODE,
    CommandDropped,
    CommandQueue,
    HeadTimeout,
    QueueFull,
    QueueItem,
    build_dropped_error,
    build_head_timeout_error,
    dispatch_timeout_for,
    is_droppable_tool,
)
from tests.test_http_server import (
    FakeGateway,
    _call_tool,
    _client,
    _initialize,
    _wait_for_queue_depth,
)


def _make_item(tool_name: str, *, request_id: int = 1) -> QueueItem:
    return QueueItem(
        correlation_id=f"{tool_name}-{request_id}",
        client_session_id=None,
        client_request_id=request_id,
        tool_name=tool_name,
        arguments={},
        response_future=asyncio.get_running_loop().create_future(),
        enqueued_at=0.0,
    )


def _read_queue_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text().splitlines() if line]
    return [line for line in lines if line.get("event_type") == "queue"]


def test_droppable_classification() -> None:
    # Cosmetic LED-class traffic may be shed.
    for tool in (
        "set_led",
        "set_leds",
        "set_all_leds",
        "clear_leds",
        "port_b_ws2812_set_pixel",
        "port_b_ws2812_set_strip",
        "port_b_ws2812_refresh",
        "port_b_ws2812_clear",
    ):
        assert is_droppable_tool(tool), tool
    # Explicit one-shot commands must never be shed. move_head stays
    # protected even though breathing micro-sway rides the same tool.
    for tool in (
        "say",
        "set_avatar",
        "take_photo",
        "move_head",
        "set_blink",
        "load_avatar_set",
        "take_photo",
        "port_b_ws2812_init",
    ):
        assert not is_droppable_tool(tool), tool


def test_dispatch_timeout_overrides() -> None:
    assert dispatch_timeout_for("get_device_info") == queue_module.DISPATCH_TIMEOUT_S
    assert dispatch_timeout_for("say") > queue_module.DISPATCH_TIMEOUT_S
    assert dispatch_timeout_for("load_avatar_set") > queue_module.DISPATCH_TIMEOUT_S


@pytest.mark.asyncio
async def test_head_timeout_force_dequeues_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queue_module, "DISPATCH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(queue_module, "DISPATCH_TIMEOUT_OVERRIDES", {})

    queue = CommandQueue(capacity=4)
    hung = _make_item("say", request_id=1)
    healthy = _make_item("get_device_info", request_id=2)
    queue.enqueue(hung)
    queue.enqueue(healthy)

    never = asyncio.Event()

    async def dispatch(item: QueueItem) -> str:
        if item.tool_name == "say":
            await never.wait()
        return item.tool_name

    dispatcher = asyncio.create_task(queue.run_dispatcher(dispatch))
    try:
        # The queue self-heals: the item behind the hung head completes.
        assert await asyncio.wait_for(healthy.response_future, timeout=2.0) == (
            "get_device_info"
        )
        with pytest.raises(HeadTimeout) as excinfo:
            await hung.response_future
    finally:
        dispatcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatcher

    assert excinfo.value.tool_name == "say"
    error = build_head_timeout_error(excinfo.value)
    assert error["code"] == HEAD_TIMEOUT_ERROR_CODE
    assert error["data"]["tool_name"] == "say"


@pytest.mark.asyncio
async def test_head_timeout_appends_jsonl_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events_path = tmp_path / "stackchan-events.jsonl"
    monkeypatch.setenv(PATH_ENV_VAR, str(events_path))
    monkeypatch.setattr(queue_module, "DISPATCH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(queue_module, "DISPATCH_TIMEOUT_OVERRIDES", {})

    queue = CommandQueue(capacity=2)
    hung = _make_item("take_photo")
    queue.enqueue(hung)

    async def dispatch(item: QueueItem) -> str:
        await asyncio.Event().wait()
        return item.tool_name

    dispatcher = asyncio.create_task(queue.run_dispatcher(dispatch))
    try:
        with pytest.raises(HeadTimeout):
            await asyncio.wait_for(hung.response_future, timeout=2.0)
    finally:
        dispatcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatcher

    events = _read_queue_events(events_path)
    assert len(events) == 1
    event = events[0]
    assert event["subtype"] == "head_timeout"
    assert event["action"] == "take_photo"
    assert event["session_id"] == "gateway"
    assert event["duration_ms"] == 50


@pytest.mark.asyncio
async def test_backpressure_evicts_oldest_droppable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events_path = tmp_path / "stackchan-events.jsonl"
    monkeypatch.setenv(PATH_ENV_VAR, str(events_path))

    queue = CommandQueue(capacity=3)
    led_old = _make_item("set_all_leds", request_id=1)
    keeper = _make_item("get_device_info", request_id=2)
    led_new = _make_item("set_led", request_id=3)
    for item in (led_old, keeper, led_new):
        queue.enqueue(item)
    assert queue.depth == 3

    incoming = _make_item("say", request_id=4)
    queue.enqueue_with_backpressure(incoming)

    # Oldest droppable evicted; everything else (including the newer LED
    # frame) kept, incoming admitted at the tail.
    assert queue.depth == 3
    with pytest.raises(CommandDropped) as excinfo:
        await led_old.response_future
    assert excinfo.value.reason == "evicted_oldest_droppable"
    error = build_dropped_error(excinfo.value)
    assert error["code"] == DROPPED_ERROR_CODE
    assert error["data"]["tool_name"] == "set_all_leds"
    assert not keeper.response_future.done()
    assert not led_new.response_future.done()
    assert not incoming.response_future.done()

    remaining = [queued.tool_name for queued in queue._queue._queue]
    assert remaining == ["get_device_info", "set_led", "say"]

    events = _read_queue_events(events_path)
    assert len(events) == 1
    assert events[0]["subtype"] == "drop_oldest"
    assert events[0]["action"] == "set_all_leds"


@pytest.mark.asyncio
async def test_backpressure_drops_incoming_droppable_when_nothing_evictable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events_path = tmp_path / "stackchan-events.jsonl"
    monkeypatch.setenv(PATH_ENV_VAR, str(events_path))

    queue = CommandQueue(capacity=2)
    queue.enqueue(_make_item("say", request_id=1))
    queue.enqueue(_make_item("move_head", request_id=2))

    with pytest.raises(CommandDropped) as excinfo:
        queue.enqueue_with_backpressure(_make_item("set_all_leds", request_id=3))
    assert excinfo.value.reason == "queue_full_incoming_droppable"
    assert queue.depth == 2

    events = _read_queue_events(events_path)
    assert len(events) == 1
    assert events[0]["subtype"] == "drop_incoming"
    assert events[0]["action"] == "set_all_leds"


@pytest.mark.asyncio
async def test_queue_events_honor_explicit_event_log_path(tmp_path: Path) -> None:
    # The HTTP daemon passes the notify.yml JSONL path so queue events land
    # in the same file as device events; env/default resolution is only the
    # fallback.
    events_path = tmp_path / "notify-configured.jsonl"
    queue = CommandQueue(capacity=2, event_log_path=events_path)
    queue.enqueue(_make_item("say", request_id=1))
    queue.enqueue(_make_item("move_head", request_id=2))

    with pytest.raises(CommandDropped):
        queue.enqueue_with_backpressure(_make_item("set_all_leds", request_id=3))

    events = _read_queue_events(events_path)
    assert len(events) == 1
    assert events[0]["subtype"] == "drop_incoming"


@pytest.mark.asyncio
async def test_backpressure_keeps_queue_full_for_non_droppable() -> None:
    queue = CommandQueue(capacity=2)
    queue.enqueue(_make_item("say", request_id=1))
    queue.enqueue(_make_item("take_photo", request_id=2))

    with pytest.raises(QueueFull):
        queue.enqueue_with_backpressure(_make_item("move_head", request_id=3))
    assert queue.depth == 2


@pytest.mark.asyncio
async def test_http_head_timeout_returns_jsonrpc_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queue_module, "DISPATCH_TIMEOUT_S", 0.1)
    monkeypatch.setattr(queue_module, "DISPATCH_TIMEOUT_OVERRIDES", {})

    async def hang(_item: QueueItem):
        await asyncio.Event().wait()

    queue = CommandQueue(capacity=2)
    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=hang,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        response = await _call_tool(
            client,
            session_id=session_id,
            name="get_device_info",
            request_id=20,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 20
    assert payload["error"]["code"] == HEAD_TIMEOUT_ERROR_CODE
    assert payload["error"]["data"]["tool_name"] == "get_device_info"


@pytest.mark.asyncio
async def test_http_saturation_evicts_led_and_admits_say() -> None:
    # No dispatcher: items stay queued, so capacity=1 saturates immediately.
    queue = CommandQueue(capacity=1)
    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        led_call = asyncio.create_task(
            _call_tool(
                client,
                session_id=session_id,
                name="set_all_leds",
                arguments={"r": 1, "g": 2, "b": 3},
                request_id=30,
            )
        )
        await _wait_for_queue_depth(queue, 1)
        say_call = asyncio.create_task(
            _call_tool(
                client,
                session_id=session_id,
                name="say",
                arguments={"text": "hello"},
                request_id=31,
            )
        )
        # The LED call resolves with the drop error once evicted.
        led_response = await asyncio.wait_for(led_call, timeout=2.0)
        assert queue.depth == 1
        say_call.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await say_call

    assert led_response.status_code == 200
    payload = led_response.json()
    assert payload["id"] == 30
    assert payload["error"]["code"] == DROPPED_ERROR_CODE
    assert payload["error"]["data"]["tool_name"] == "set_all_leds"
    assert payload["error"]["data"]["reason"] == "evicted_oldest_droppable"
