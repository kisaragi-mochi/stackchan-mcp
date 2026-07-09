import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

from stackchan_mcp import follow_led_stream as fls
from stackchan_mcp import wifi_power_save
from stackchan_mcp.follow_led_stream import (
    FollowLedStream,
    FollowLedStreamConfig,
)


_URL_BASE = "ws://" + "example.invalid"


def _url(path: str = "led") -> str:
    return f"{_URL_BASE}/{path}"


_WIFI_SET_POWER_SAVE = "self.wifi.set_power_save"
_BASE_SET_MANY = "self.led.set_many"
_PORT_B_INIT = "self.port_b.ws2812.init"
_PORT_B_SET_STRIP = "self.port_b.ws2812.set_strip"


class _FakeESP32:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._replies: dict[str, list[tuple[Any, Any]]] = {}
        self._exc: dict[str, list[BaseException]] = {}

    def push_reply(self, method: str, result: Any, error: Any = None) -> None:
        self._replies.setdefault(method, []).append((result, error))

    def push_raise(self, method: str, exc: BaseException) -> None:
        self._exc.setdefault(method, []).append(exc)

    async def call_tool(
        self,
        method: str,
        args: dict[str, Any],
    ) -> tuple[Any, Any]:
        self.calls.append((method, args))
        if self._exc.get(method):
            raise self._exc[method].pop(0)
        if self._replies.get(method):
            return self._replies[method].pop(0)
        if method == _WIFI_SET_POWER_SAVE:
            previous = "max_modem" if args.get("mode") == "none" else "none"
            return {
                "ok": True,
                "previous": previous,
                "current": args.get("mode"),
            }, None
        if method == _PORT_B_INIT:
            return {
                "available": True,
                "ok": True,
                "led_count": args.get("led_count"),
            }, None
        return {"ok": True}, None


class _FakeGateway:
    def __init__(self) -> None:
        self.esp32 = _FakeESP32()


class _FakeWebSocket:
    def __init__(
        self,
        messages: list[str],
        *,
        clock: "_Clock | None" = None,
        tick_s: float = 0.0,
    ) -> None:
        self._messages = messages
        self._clock = clock
        self._tick_s = tick_s
        self._index = 0

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        message = self._messages[self._index]
        self._index += 1
        if self._clock is not None:
            self._clock.advance(self._tick_s)
        return message


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _RefusingConnect:
    async def __aenter__(self) -> None:
        raise ConnectionRefusedError("refused")

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _refusing_connect(_url: str) -> _RefusingConnect:
    return _RefusingConnect()


def _frame(
    *,
    ts: int = 1,
    kind: str = "event",
    colors: list[list[int]] | None = None,
    **extra: Any,
) -> str:
    payload = {
        "ts": ts,
        "kind": kind,
        "colors": colors if colors is not None else [[255, 0, 0]],
        **extra,
    }
    return json.dumps(payload)


def _mark_wifi_ok(follower: FollowLedStream) -> None:
    follower._wifi_ps_apply_result = {
        "ok": True,
        "previous": "min_modem",
        "current": "none",
    }


@pytest_asyncio.fixture(autouse=True)
async def _reset_singleton() -> Any:
    await fls.stop_follow()
    wifi_power_save._clear_for_tests()
    yield
    await fls.stop_follow()
    wifi_power_save._clear_for_tests()


def test_config_validation_accepts_expected_target_led_count_pairs() -> None:
    assert FollowLedStreamConfig(url=_url("base"), target="base_ring").capacity == 12
    assert (
        FollowLedStreamConfig(url=_url("base-12"), target="base_ring", led_count=12)
        .capacity
        == 12
    )
    assert (
        FollowLedStreamConfig(url=_url("port-b"), target="port_b", led_count=18)
        .capacity
        == 18
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "", "target": "base_ring"},
        {"url": _url("bad-target"), "target": "unknown"},
        {"url": _url("base-bad-count"), "target": "base_ring", "led_count": 11},
        {"url": _url("port-missing-count"), "target": "port_b"},
        {"url": _url("port-zero"), "target": "port_b", "led_count": 0},
        {"url": _url("port-too-many"), "target": "port_b", "led_count": 257},
        {"url": _url("fps-zero"), "target": "base_ring", "max_fps": 0},
        {"url": _url("fps-too-high"), "target": "base_ring", "max_fps": 31},
    ],
)
def test_config_validation_rejects_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        FollowLedStreamConfig(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "not json",
        "42",
        json.dumps({"kind": "event", "colors": [[255, 0, 0]]}),
        json.dumps({"ts": "bad", "kind": "event", "colors": [[255, 0, 0]]}),
        json.dumps({"ts": 1, "kind": "unknown", "colors": [[255, 0, 0]]}),
        json.dumps({"ts": 1, "kind": "event", "colors": []}),
        json.dumps({"ts": 1, "kind": "event", "colors": [[255, 0]]}),
        json.dumps({"ts": 1, "kind": "event", "colors": [[255, 0, 256]]}),
        json.dumps({"ts": 1, "kind": "event", "colors": [[0, 0, 0]] * 13}),
    ],
)
async def test_malformed_frames_drop_the_whole_frame(message: str) -> None:
    gateway = _FakeGateway()
    follower = FollowLedStream(
        gateway,
        FollowLedStreamConfig(url=_url("malformed"), target="base_ring"),
    )
    _mark_wifi_ok(follower)

    await follower._consume(_FakeWebSocket([message]))

    assert gateway.esp32.calls == []
    status = follower.status()
    assert status["frames_received"] == 1
    assert status["frames_sent"] == 0
    assert status["frames_dropped"] == 1


@pytest.mark.asyncio
async def test_event_frames_bypass_continuous_rate_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _FakeGateway()
    cfg = FollowLedStreamConfig(
        url=_url("rate-gate"),
        target="base_ring",
        max_fps=10,
    )
    follower = FollowLedStream(gateway, cfg)
    _mark_wifi_ok(follower)
    clock = _Clock()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", clock.time)

    await follower._consume(
        _FakeWebSocket(
            [
                _frame(kind="continuous", colors=[[1, 0, 0]]),
                _frame(kind="continuous", colors=[[2, 0, 0]]),
                _frame(kind="event", colors=[[3, 0, 0]]),
            ],
            clock=clock,
            tick_s=0.01,
        )
    )

    sent_colors = [
        json.loads(arguments["colors"])
        for tool_name, arguments in gateway.esp32.calls
        if tool_name == _BASE_SET_MANY
    ]
    assert sent_colors == [[[1, 0, 0]], [[3, 0, 0]]]
    assert follower.status()["frames_sent"] == 2
    assert follower.status()["frames_dropped"] == 1


@pytest.mark.asyncio
async def test_base_ring_dispatch_uses_json_encoded_set_many_payload() -> None:
    gateway = _FakeGateway()
    follower = FollowLedStream(
        gateway,
        FollowLedStreamConfig(url=_url("base-dispatch"), target="base_ring"),
    )
    _mark_wifi_ok(follower)
    colors = [[32, 0, 0], [0, 32, 0]]

    await follower._consume(_FakeWebSocket([_frame(colors=colors)]))

    assert gateway.esp32.calls == [
        (_BASE_SET_MANY, {"colors": json.dumps(colors)})
    ]
    assert follower.status()["frames_sent"] == 1


@pytest.mark.asyncio
async def test_port_b_dispatch_uses_json_encoded_set_strip_payload() -> None:
    gateway = _FakeGateway()
    follower = FollowLedStream(
        gateway,
        FollowLedStreamConfig(
            url=_url("port-b-dispatch"),
            target="port_b",
            led_count=3,
        ),
    )
    follower._target_ready = True
    _mark_wifi_ok(follower)
    colors = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]

    await follower._consume(_FakeWebSocket([_frame(colors=colors)]))

    assert gateway.esp32.calls == [
        (_PORT_B_SET_STRIP, {"colors": json.dumps(colors)})
    ]
    assert follower.status()["frames_sent"] == 1


@pytest.mark.asyncio
async def test_port_b_start_initializes_strip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fls.websockets, "connect", _refusing_connect)
    gateway = _FakeGateway()
    cfg = FollowLedStreamConfig(
        url=_url("port-b-init"),
        target="port_b",
        led_count=5,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )

    status = await fls.start_follow(gateway, cfg)

    assert status["running"] is True
    assert gateway.esp32.calls[:2] == [
        (_WIFI_SET_POWER_SAVE, {"mode": "none"}),
        (_PORT_B_INIT, {"led_count": 5}),
    ]
    await fls.stop_follow()


@pytest.mark.asyncio
async def test_port_b_init_failure_makes_start_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fls.websockets, "connect", _refusing_connect)
    gateway = _FakeGateway()
    gateway.esp32.push_reply(
        _PORT_B_INIT,
        {"available": False, "ok": False, "error": "strip unavailable"},
    )
    cfg = FollowLedStreamConfig(
        url=_url("port-b-init-fail"),
        target="port_b",
        led_count=5,
    )

    with pytest.raises(RuntimeError, match="strip unavailable"):
        await fls.start_follow(gateway, cfg)

    assert fls.get_follow_status() == {"running": False}
    assert (_PORT_B_INIT, {"led_count": 5}) in gateway.esp32.calls
    assert (_WIFI_SET_POWER_SAVE, {"mode": "max_modem"}) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_start_then_start_replaces_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fls.websockets, "connect", _refusing_connect)
    gateway = _FakeGateway()
    cfg_a = FollowLedStreamConfig(
        url=_url("a"),
        target="base_ring",
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    cfg_b = FollowLedStreamConfig(
        url=_url("b"),
        target="base_ring",
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )

    await fls.start_follow(gateway, cfg_a)
    first = fls._follower
    assert first is not None
    first_task = first._task
    assert first_task is not None
    await asyncio.sleep(0)

    await fls.start_follow(gateway, cfg_b)

    status = fls.get_follow_status()
    assert status["url"] == _url("b")
    assert status["target"] == "base_ring"
    assert status["max_fps"] == 30.0
    assert status["frames_received"] == 0
    assert status["frames_sent"] == 0
    assert status["frames_dropped"] == 0
    assert first_task.done()
    stop_status = await fls.stop_follow()
    assert stop_status["running"] is False
    assert fls.get_follow_status() == {"running": False}


@pytest.mark.asyncio
async def test_status_when_not_running() -> None:
    assert fls.get_follow_status() == {"running": False}
    assert await fls.stop_follow() == {"running": False}


@pytest.mark.asyncio
async def test_device_disconnect_invalidates_then_reinitializes_port_b() -> None:
    gateway = _FakeGateway()
    gateway.esp32.push_reply(
        _PORT_B_SET_STRIP,
        None,
        {"code": -32000, "message": "ESP32 not connected"},
    )
    gateway.esp32.push_reply(_PORT_B_SET_STRIP, {"ok": True})
    follower = FollowLedStream(
        gateway,
        FollowLedStreamConfig(
            url=_url("recovery"),
            target="port_b",
            led_count=2,
        ),
    )
    follower._target_ready = True
    _mark_wifi_ok(follower)

    await follower._consume(
        _FakeWebSocket(
            [
                _frame(colors=[[1, 0, 0], [0, 1, 0]]),
                _frame(ts=2, colors=[[0, 0, 1], [2, 2, 2]]),
            ]
        )
    )

    assert gateway.esp32.calls == [
        (_PORT_B_SET_STRIP, {"colors": json.dumps([[1, 0, 0], [0, 1, 0]])}),
        (_WIFI_SET_POWER_SAVE, {"mode": "none"}),
        (_PORT_B_INIT, {"led_count": 2}),
        (_PORT_B_SET_STRIP, {"colors": json.dumps([[0, 0, 1], [2, 2, 2]])}),
    ]
    status = follower.status()
    assert status["frames_sent"] == 1
    assert status["frames_dropped"] == 1
    assert status["wifi_ps_apply_result"]["ok"] is True


@pytest.mark.asyncio
async def test_port_b_unavailable_dispatch_recovers_and_retries_same_frame() -> None:
    gateway = _FakeGateway()
    gateway.esp32.push_reply(
        _PORT_B_SET_STRIP,
        {
            "available": False,
            "ok": False,
            "error": "strip not initialized",
        },
    )
    gateway.esp32.push_reply(_PORT_B_SET_STRIP, {"ok": True})
    follower = FollowLedStream(
        gateway,
        FollowLedStreamConfig(
            url=_url("port-b-unavailable-recovery"),
            target="port_b",
            led_count=2,
        ),
    )
    follower._target_ready = True
    _mark_wifi_ok(follower)
    colors = [[3, 0, 0], [0, 3, 0]]

    await follower._consume(_FakeWebSocket([_frame(colors=colors)]))

    assert gateway.esp32.calls == [
        (_PORT_B_SET_STRIP, {"colors": json.dumps(colors)}),
        (_WIFI_SET_POWER_SAVE, {"mode": "none"}),
        (_PORT_B_INIT, {"led_count": 2}),
        (_PORT_B_SET_STRIP, {"colors": json.dumps(colors)}),
    ]
    status = follower.status()
    assert status["frames_sent"] == 1
    assert status["frames_dropped"] == 0
    assert status["wifi_ps_apply_result"]["ok"] is True


@pytest.mark.asyncio
async def test_port_b_unavailable_retry_failure_drops_frame_without_extra_retry() -> None:
    gateway = _FakeGateway()
    first_colors = [[4, 0, 0], [0, 4, 0]]
    second_colors = [[0, 0, 4], [4, 4, 4]]
    gateway.esp32.push_reply(
        _PORT_B_SET_STRIP,
        {
            "available": False,
            "ok": False,
            "error": "strip not initialized",
        },
    )
    gateway.esp32.push_reply(
        _PORT_B_SET_STRIP,
        {
            "available": False,
            "ok": False,
            "error": "still unavailable",
        },
    )
    gateway.esp32.push_reply(_PORT_B_SET_STRIP, {"ok": True})
    follower = FollowLedStream(
        gateway,
        FollowLedStreamConfig(
            url=_url("port-b-unavailable-retry-failure"),
            target="port_b",
            led_count=2,
        ),
    )
    follower._target_ready = True
    _mark_wifi_ok(follower)

    await follower._consume(
        _FakeWebSocket(
            [
                _frame(colors=first_colors),
                _frame(ts=2, colors=second_colors),
            ]
        )
    )

    assert gateway.esp32.calls == [
        (_PORT_B_SET_STRIP, {"colors": json.dumps(first_colors)}),
        (_WIFI_SET_POWER_SAVE, {"mode": "none"}),
        (_PORT_B_INIT, {"led_count": 2}),
        (_PORT_B_SET_STRIP, {"colors": json.dumps(first_colors)}),
        (_WIFI_SET_POWER_SAVE, {"mode": "none"}),
        (_PORT_B_INIT, {"led_count": 2}),
        (_PORT_B_SET_STRIP, {"colors": json.dumps(second_colors)}),
    ]
    status = follower.status()
    assert status["frames_sent"] == 1
    assert status["frames_dropped"] == 1
    assert "still unavailable" in status["last_error"]


@pytest.mark.asyncio
async def test_source_and_frame_filters_skip_silently() -> None:
    gateway = _FakeGateway()
    follower = FollowLedStream(
        gateway,
        FollowLedStreamConfig(
            url=_url("filters"),
            target="base_ring",
            source_filter="stage",
            frame_filter="calibrated",
        ),
    )
    _mark_wifi_ok(follower)

    await follower._consume(
        _FakeWebSocket(
            [
                _frame(source="other", frame="calibrated"),
                _frame(source="stage", frame="raw"),
            ]
        )
    )

    assert gateway.esp32.calls == []
    assert follower.status()["frames_received"] == 2
    assert follower.status()["frames_dropped"] == 0


@pytest.mark.asyncio
async def test_wifi_power_save_refcount_restores_only_after_last_release() -> None:
    client = _FakeESP32()

    first, second = await asyncio.gather(
        wifi_power_save.acquire_wifi_power_save(client),
        wifi_power_save.acquire_wifi_power_save(client),
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert client.calls == [(_WIFI_SET_POWER_SAVE, {"mode": "none"})]

    one_left = await wifi_power_save.release_wifi_power_save(client)
    assert one_left["skipped"] is True
    assert one_left["ref_count"] == 1
    assert client.calls == [(_WIFI_SET_POWER_SAVE, {"mode": "none"})]

    restored = await wifi_power_save.release_wifi_power_save(client)
    assert restored["ok"] is True
    assert client.calls == [
        (_WIFI_SET_POWER_SAVE, {"mode": "none"}),
        (_WIFI_SET_POWER_SAVE, {"mode": "max_modem"}),
    ]
