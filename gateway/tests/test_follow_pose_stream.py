import asyncio
import json
import math
from typing import Any

import pytest
import pytest_asyncio

from stackchan_mcp import follow_pose_stream as fps
from stackchan_mcp.follow_pose_stream import (
    FollowPoseStream,
    FollowPoseStreamConfig,
    map_sensor_to_servo,
    step_clamp,
)


_URL_BASE = "ws://" + "example.invalid"


def _url(path: str = "pose") -> str:
    return f"{_URL_BASE}/{path}"


_GET_HEAD_ANGLES = "self.robot.get_head_angles"
_SET_HEAD_ANGLES = "self.robot.set_head_angles"


def _wrap_call_payload(
    payload: dict[str, Any],
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    return {
        "content": [{"text": json.dumps(payload)}],
        "isError": is_error,
    }


def _wrap_get_head_angles(
    yaw: Any,
    pitch: Any,
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    return _wrap_call_payload({"yaw": yaw, "pitch": pitch}, is_error=is_error)


class _FakeESP32:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._replies: dict[str, tuple[Any, Any]] = {}
        self._exc: dict[str, BaseException] = {}

    def set_reply(self, method: str, result: Any, error: Any = None) -> None:
        self._replies[method] = (result, error)
        self._exc.pop(method, None)

    def set_raise(self, method: str, exc: BaseException) -> None:
        self._exc[method] = exc

    async def call_tool(
        self,
        method: str,
        args: dict[str, Any],
    ) -> tuple[Any, Any]:
        self.calls.append((method, args))
        if method in self._exc:
            raise self._exc[method]
        if method in self._replies:
            return self._replies[method]
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


@pytest_asyncio.fixture(autouse=True)
async def _reset_singleton() -> Any:
    await fps.stop_follow()
    yield
    await fps.stop_follow()


def test_map_sensor_to_servo_1to1_within_range() -> None:
    assert map_sensor_to_servo(
        30,
        -10,
        flip_yaw=1,
        flip_pitch=1,
        pitch_center_deg=45,
    ) == (30, 35)


def test_map_sensor_to_servo_saturates_at_clamp() -> None:
    assert map_sensor_to_servo(
        200,
        200,
        flip_yaw=1,
        flip_pitch=1,
        pitch_center_deg=45,
    ) == (90, 85)


def test_map_sensor_to_servo_negative_saturates() -> None:
    assert map_sensor_to_servo(
        -200,
        -200,
        flip_yaw=1,
        flip_pitch=1,
        pitch_center_deg=45,
    ) == (-90, 5)


def test_map_sensor_to_servo_flip_inverts() -> None:
    assert map_sensor_to_servo(
        30,
        0,
        flip_yaw=-1,
        flip_pitch=1,
        pitch_center_deg=45,
    )[0] == -30


def test_step_clamp_within_step_passes() -> None:
    assert step_clamp(target=10, last=8, max_step_deg=5) == 10


def test_step_clamp_above_step_limits() -> None:
    assert step_clamp(target=20, last=0, max_step_deg=5) == 5


def test_step_clamp_below_step_limits() -> None:
    assert step_clamp(target=-20, last=0, max_step_deg=5) == -5



@pytest.mark.asyncio
async def test_seed_from_device_initializes_last_servo() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_reply(_GET_HEAD_ANGLES, _wrap_get_head_angles(60, 70))
    follower = FollowPoseStream(gateway, FollowPoseStreamConfig(url=_url("seed-ok")))

    await follower._seed_from_device()

    assert follower._last_servo_yaw == 60
    assert follower._last_servo_pitch == 70
    assert follower._initial_pose_seeded is True


@pytest.mark.asyncio
async def test_seed_from_device_failure_does_not_lock_seeded_flag() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_reply(
        _GET_HEAD_ANGLES,
        _wrap_get_head_angles(60, 70, is_error=True),
    )
    follower = FollowPoseStream(
        gateway,
        FollowPoseStreamConfig(url=_url("seed-error")),
    )

    await follower._seed_from_device()

    assert follower._last_servo_yaw == 0
    assert follower._last_servo_pitch == 45
    assert follower._last_error == "self.robot.get_head_angles returned isError"
    assert follower._initial_pose_seeded is False

    gateway.esp32.set_reply(_GET_HEAD_ANGLES, _wrap_get_head_angles(30, 55))
    await follower._seed_from_device()

    assert follower._last_servo_yaw == 30
    assert follower._last_servo_pitch == 55
    assert follower._initial_pose_seeded is True


@pytest.mark.asyncio
async def test_seed_from_device_exception_does_not_lock_seeded_flag() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_raise(_GET_HEAD_ANGLES, RuntimeError("seed failed"))
    follower = FollowPoseStream(
        gateway,
        FollowPoseStreamConfig(url=_url("seed-exception")),
    )

    await follower._seed_from_device()

    assert follower._last_servo_yaw == 0
    assert follower._last_servo_pitch == 45
    assert follower._last_error == "seed failed"
    assert follower._initial_pose_seeded is False

    gateway.esp32.set_reply(_GET_HEAD_ANGLES, _wrap_get_head_angles(-20, 65))
    await follower._seed_from_device()

    assert follower._last_servo_yaw == -20
    assert follower._last_servo_pitch == 65
    assert follower._initial_pose_seeded is True


@pytest.mark.asyncio
async def test_seed_from_device_handles_plain_dict_payload() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_reply(_GET_HEAD_ANGLES, {"yaw": 60, "pitch": 70})
    follower = FollowPoseStream(
        gateway,
        FollowPoseStreamConfig(url=_url("seed-plain-dict")),
    )

    await follower._seed_from_device()

    assert follower._last_servo_yaw == 60
    assert follower._last_servo_pitch == 70
    assert follower._initial_pose_seeded is True


@pytest.mark.asyncio
async def test_seed_from_device_unpack_failure_does_not_lock_seeded_flag() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_reply(
        _GET_HEAD_ANGLES,
        {"content": [{"text": "not json"}], "isError": False},
    )
    follower = FollowPoseStream(
        gateway,
        FollowPoseStreamConfig(url=_url("seed-malformed")),
    )

    await follower._seed_from_device()

    assert follower._last_servo_yaw == 0
    assert follower._last_servo_pitch == 45
    assert follower._initial_pose_seeded is False

    gateway.esp32.set_reply(_GET_HEAD_ANGLES, _wrap_get_head_angles(10, 75))
    await follower._seed_from_device()

    assert follower._last_servo_yaw == 10
    assert follower._last_servo_pitch == 75
    assert follower._initial_pose_seeded is True


@pytest.mark.asyncio
async def test_consume_step_clamps_from_seeded_position() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_reply(_GET_HEAD_ANGLES, _wrap_get_head_angles(90, 45))
    cfg = FollowPoseStreamConfig(
        url=_url("seeded-step-clamp"),
        max_step_deg=5,
        smoothing_window=1,
    )
    follower = FollowPoseStream(gateway, cfg)

    await follower._seed_from_device()
    gateway.esp32.calls.clear()
    await follower._consume(_FakeWebSocket([json.dumps({"yaw": -90, "pitch": 0})]))

    assert gateway.esp32.calls == [
        (
            _SET_HEAD_ANGLES,
            {"yaw": 85, "pitch": 45, "speed_dps": 240},
        )
    ]


@pytest.mark.asyncio
async def test_run_does_not_seed_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _FakeGateway()
    cfg = FollowPoseStreamConfig(
        url=_url("no-seed"),
        seed_from_device=False,
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    follower = FollowPoseStream(gateway, cfg)

    class _StopWebSocket:
        def __aiter__(self) -> "_StopWebSocket":
            return self

        async def __anext__(self) -> str:
            follower._stop_event.set()
            raise StopAsyncIteration

    class _Connect:
        async def __aenter__(self) -> _StopWebSocket:
            return _StopWebSocket()

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    def connect(url: str) -> _Connect:
        assert url == cfg.url
        return _Connect()

    monkeypatch.setattr(fps.websockets, "connect", connect)

    await follower._run()

    assert all(call[0] != "self.robot.get_head_angles" for call in gateway.esp32.calls)


@pytest.mark.asyncio
async def test_consume_calls_set_head_angles() -> None:
    gateway = _FakeGateway()
    cfg = FollowPoseStreamConfig(url=_url("consume"), smoothing_window=1)
    follower = FollowPoseStream(gateway, cfg)

    await follower._consume(
        _FakeWebSocket([json.dumps({"yaw": 10, "pitch": -5, "ts": 123})])
    )

    assert gateway.esp32.calls == [
        (
            _SET_HEAD_ANGLES,
            {"yaw": 10, "pitch": 40, "speed_dps": 240},
        )
    ]
    assert follower.status()["frames_received"] == 1
    assert follower.status()["frames_accepted"] == 1
    assert follower.status()["last_frame_ts"] == 123


@pytest.mark.asyncio
async def test_consume_skips_command_on_call_result_isError() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_reply(
        _SET_HEAD_ANGLES,
        {"isError": True, "content": [{"text": json.dumps({"error": "failed"})}]},
    )
    cfg = FollowPoseStreamConfig(url=_url("set-is-error"), smoothing_window=1)
    follower = FollowPoseStream(gateway, cfg)

    await follower._consume(_FakeWebSocket([json.dumps({"yaw": 10, "pitch": 0})]))

    assert follower.status()["commands_sent"] == 0
    assert follower._last_servo_yaw == 0
    assert follower._last_servo_pitch == 45
    assert follower._last_sent_at is None
    assert follower._last_error == "set_head_angles reported isError"


@pytest.mark.asyncio
async def test_consume_skips_command_on_payload_ok_false() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_reply(_SET_HEAD_ANGLES, _wrap_call_payload({"ok": False}))
    cfg = FollowPoseStreamConfig(url=_url("set-ok-false"), smoothing_window=1)
    follower = FollowPoseStream(gateway, cfg)

    await follower._consume(_FakeWebSocket([json.dumps({"yaw": 10, "pitch": 0})]))

    assert follower.status()["commands_sent"] == 0
    assert follower._last_servo_yaw == 0
    assert follower._last_servo_pitch == 45
    assert follower._last_sent_at is None
    assert follower._last_error == "set_head_angles payload reported ok=false"


@pytest.mark.asyncio
async def test_consume_skips_command_on_payload_servo_init_ok_false() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_reply(
        _SET_HEAD_ANGLES,
        _wrap_call_payload({"servo_init_ok": False}),
    )
    cfg = FollowPoseStreamConfig(
        url=_url("set-servo-init-false"),
        smoothing_window=1,
    )
    follower = FollowPoseStream(gateway, cfg)

    await follower._consume(_FakeWebSocket([json.dumps({"yaw": 10, "pitch": 0})]))

    assert follower.status()["commands_sent"] == 0
    assert follower._last_servo_yaw == 0
    assert follower._last_servo_pitch == 45
    assert follower._last_sent_at is None
    assert follower._last_error == (
        "set_head_angles payload reported servo_init_ok=false"
    )


@pytest.mark.asyncio
async def test_consume_advances_on_successful_payload() -> None:
    gateway = _FakeGateway()
    gateway.esp32.set_reply(
        _SET_HEAD_ANGLES,
        _wrap_call_payload({"servo_init_ok": True}),
    )
    cfg = FollowPoseStreamConfig(url=_url("set-success"), smoothing_window=1)
    follower = FollowPoseStream(gateway, cfg)

    await follower._consume(_FakeWebSocket([json.dumps({"yaw": 10, "pitch": 0})]))

    assert follower.status()["commands_sent"] == 1
    assert follower._last_servo_yaw == 10
    assert follower._last_servo_pitch == 45
    assert follower._last_sent_at is not None



@pytest.mark.asyncio
async def test_consume_filters_by_source() -> None:
    gateway = _FakeGateway()
    cfg = FollowPoseStreamConfig(
        url=_url("source-filter"),
        source_filter="airpods",
    )
    follower = FollowPoseStream(gateway, cfg)

    await follower._consume(
        _FakeWebSocket(
            [json.dumps({"source": "dummy", "yaw": 10, "pitch": 0})]
        )
    )

    assert gateway.esp32.calls == []
    assert follower.status()["frames_accepted"] == 0



@pytest.mark.asyncio
async def test_consume_filters_by_frame() -> None:
    gateway = _FakeGateway()
    cfg = FollowPoseStreamConfig(
        url=_url("frame-filter"),
        frame_filter="calibrated",
    )
    follower = FollowPoseStream(gateway, cfg)

    await follower._consume(
        _FakeWebSocket([json.dumps({"frame": "raw", "yaw": 10, "pitch": 0})])
    )

    assert gateway.esp32.calls == []
    assert follower.status()["frames_accepted"] == 0



@pytest.mark.asyncio
async def test_consume_rejects_non_numeric_yaw() -> None:
    gateway = _FakeGateway()
    follower = FollowPoseStream(
        gateway,
        FollowPoseStreamConfig(url=_url("bad-yaw")),
    )

    await follower._consume(_FakeWebSocket([json.dumps({"yaw": "abc", "pitch": 0})]))

    assert gateway.esp32.calls == []
    assert follower.status()["frames_accepted"] == 0



@pytest.mark.asyncio
async def test_consume_rejects_non_dict() -> None:
    gateway = _FakeGateway()
    follower = FollowPoseStream(
        gateway,
        FollowPoseStreamConfig(url=_url("non-dict")),
    )

    await follower._consume(_FakeWebSocket(["42"]))

    assert gateway.esp32.calls == []
    assert follower.status()["frames_accepted"] == 0



@pytest.mark.asyncio
async def test_consume_downsamples(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _FakeGateway()
    cfg = FollowPoseStreamConfig(
        url=_url("downsample"),
        downsample_hz=10,
        max_step_deg=30,
        smoothing_window=1,
    )
    follower = FollowPoseStream(gateway, cfg)
    clock = _Clock()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", clock.time)
    messages = [json.dumps({"yaw": idx, "pitch": 0}) for idx in range(20)]
    tick_s = 0.01

    await follower._consume(_FakeWebSocket(messages, clock=clock, tick_s=tick_s))

    duration = len(messages) * tick_s
    limit = math.ceil(duration * cfg.downsample_hz) + 1
    assert follower.status()["commands_sent"] <= limit
    assert len(gateway.esp32.calls) == follower.status()["commands_sent"]
    assert follower.status()["commands_sent"] >= 1



@pytest.mark.asyncio
async def test_consume_step_clamps_velocity() -> None:
    gateway = _FakeGateway()
    cfg = FollowPoseStreamConfig(
        url=_url("step-clamp"),
        max_step_deg=5,
        smoothing_window=1,
    )
    follower = FollowPoseStream(gateway, cfg)

    await follower._consume(_FakeWebSocket([json.dumps({"yaw": 100, "pitch": 0})]))

    assert gateway.esp32.calls == [
        (
            _SET_HEAD_ANGLES,
            {"yaw": 5, "pitch": 45, "speed_dps": 240},
        )
    ]



@pytest.mark.asyncio
async def test_start_then_start_cancels_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RefusingConnect:
        async def __aenter__(self) -> None:
            raise ConnectionRefusedError("refused")

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    def refusing_connect(_url: str) -> _RefusingConnect:
        return _RefusingConnect()

    monkeypatch.setattr(fps.websockets, "connect", refusing_connect)
    gateway = _FakeGateway()
    cfg_a = FollowPoseStreamConfig(
        url=_url("a"),
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )
    cfg_b = FollowPoseStreamConfig(
        url=_url("b"),
        reconnect_initial_backoff_s=0.01,
        reconnect_max_backoff_s=0.01,
    )

    await fps.start_follow(gateway, cfg_a)
    first = fps._follower
    assert first is not None
    first_task = first._task
    assert first_task is not None
    await asyncio.sleep(0)

    await fps.start_follow(gateway, cfg_b)

    assert fps.get_follow_status()["url"] == _url("b")
    assert first_task.done()



@pytest.mark.asyncio
async def test_stop_when_not_running() -> None:
    assert await fps.stop_follow() == {"running": False}


def test_get_follow_status_when_not_running() -> None:
    assert fps.get_follow_status() == {"running": False}
