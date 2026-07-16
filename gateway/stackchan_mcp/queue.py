"""Bounded command queue for Issue #178 Phase B chunk 1.

This module follows the command-queue design in
``docs/178-http-transport-spike.md`` and intentionally stays independent from
the HTTP MCP server, MCP SDK objects, and ESP32 gateway wiring.

Queue watchdog
--------------

Two failure modes observed under sustained load are handled here rather
than in the HTTP layer:

* **Head-of-queue stall.** The dispatcher is single-flight: one hung
  dispatch (a TTS engine that never answers, a device wedged mid-upload)
  used to stall every queued command behind it until the process was
  restarted. ``run_dispatcher`` now bounds each dispatch with a per-tool
  timeout (:data:`DISPATCH_TIMEOUT_S`, long-running tools get overrides
  in :data:`DISPATCH_TIMEOUT_OVERRIDES`), force-dequeues the head on
  expiry, fails its response future with :class:`HeadTimeout`, and moves
  on to the next item.

* **Saturation lockout.** A full queue used to reject every newcomer
  uniformly, so a burst of cosmetic LED frames could starve one-shot
  commands (``say``, ``set_avatar``, ``take_photo``, ``move_head``).
  :meth:`CommandQueue.enqueue_with_backpressure` instead evicts the
  oldest queued *droppable* item (:data:`DROPPABLE_TOOLS` — cosmetic,
  superseded-by-the-next-frame traffic) to admit the newcomer. Explicit
  one-shot commands are never dropped; when nothing is evictable the
  behaviour falls back to the original ``QueueFull`` rejection.

Both interventions are logged at WARNING and appended to the stackchan
JSONL event log (``event_type="queue"``) so drops are observable
downstream rather than silent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COMMAND_QUEUE_SIZE_ENV = "STACKCHAN_COMMAND_QUEUE_SIZE"
DEFAULT_COMMAND_QUEUE_CAPACITY = 32
QUEUE_FULL_ERROR_CODE = -32000
QUEUE_FULL_MESSAGE = "stackchan command queue is full"
QUEUE_FULL_RETRY_AFTER_MS = 250

# Head-of-queue dispatch budget. Constants by design (not env-tunable):
# the values encode protocol knowledge, not deployment preference.
DISPATCH_TIMEOUT_S = 30.0
# Tools whose legitimate runtime exceeds the default budget. Values are
# upper bounds for a *healthy* run, with margin; they exist so the
# watchdog never cuts a working call short:
# - say: TTS synthesis + real-time-paced Opus streaming of the full
#   utterance (a long paragraph can stream for over a minute).
# - listen: capture window runs for the caller-requested duration plus
#   STT turnaround.
# - load_avatar_set: device-side HTTP fetch + SPIFFS/PSRAM adoption of a
#   multi-hundred-KB archive (~40 s observed for a 90-frame set).
DISPATCH_TIMEOUT_OVERRIDES: dict[str, float] = {
    "say": 120.0,
    "listen": 120.0,
    "load_avatar_set": 180.0,
}
HEAD_TIMEOUT_ERROR_CODE = -32001
HEAD_TIMEOUT_MESSAGE = "stackchan command timed out at queue head"

# Cosmetic, high-frequency traffic where the next frame supersedes the
# dropped one. Explicit one-shot commands (say / set_avatar /
# take_photo / move_head / ...) are deliberately absent: they must
# never be shed. port_b_ws2812_init is also excluded — it is setup, not
# a frame.
DROPPABLE_TOOLS = frozenset(
    {
        "set_led",
        "set_leds",
        "set_all_leds",
        "clear_leds",
        "port_b_ws2812_set_pixel",
        "port_b_ws2812_set_strip",
        "port_b_ws2812_refresh",
        "port_b_ws2812_clear",
    }
)
DROPPED_ERROR_CODE = -32002
DROPPED_MESSAGE = "stackchan command dropped by queue backpressure"

QUEUE_EVENT_TYPE = "queue"
QUEUE_EVENT_SESSION_ID = "gateway"

DispatchFn = Callable[["QueueItem"], Awaitable[Any]]


def dispatch_timeout_for(tool_name: str) -> float:
    """Return the head-of-queue dispatch budget for ``tool_name``."""
    return DISPATCH_TIMEOUT_OVERRIDES.get(tool_name, DISPATCH_TIMEOUT_S)


def is_droppable_tool(tool_name: str) -> bool:
    """Return whether ``tool_name`` may be shed under queue saturation."""
    return tool_name in DROPPABLE_TOOLS


@dataclass(frozen=True)
class QueueItem:
    """One ESP32-bound command and the future that receives its response."""

    correlation_id: str
    client_session_id: str | None
    client_request_id: int | str
    tool_name: str
    arguments: dict[str, Any]
    response_future: asyncio.Future[Any]
    enqueued_at: float


class QueueFull(Exception):
    """Raised by CommandQueue.enqueue when capacity is reached."""

    def __init__(self, queue_depth: int, capacity: int) -> None:
        self.queue_depth = queue_depth
        self.capacity = capacity
        super().__init__(
            f"{QUEUE_FULL_MESSAGE} (queue_depth={queue_depth}, capacity={capacity})"
        )


class HeadTimeout(Exception):
    """Set on a response future when its dispatch exceeded the head budget."""

    def __init__(self, tool_name: str, timeout_s: float) -> None:
        self.tool_name = tool_name
        self.timeout_s = timeout_s
        super().__init__(
            f"{HEAD_TIMEOUT_MESSAGE} (tool={tool_name}, timeout_s={timeout_s:g})"
        )


class CommandDropped(Exception):
    """Command shed by queue backpressure.

    Raised by :meth:`CommandQueue.enqueue_with_backpressure` when the
    *incoming* droppable command is rejected, and set on the response
    future of a queued victim evicted to admit a newer command.
    """

    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"{DROPPED_MESSAGE} (tool={tool_name}, reason={reason})")


class CommandQueue:
    """Asyncio-backed bounded FIFO queue for serialized command dispatch."""

    def __init__(
        self,
        capacity: int | None = None,
        *,
        event_log_path: Path | None = None,
    ) -> None:
        self._capacity = capacity if capacity is not None else _capacity_from_env()
        if self._capacity < 1:
            raise ValueError("command queue capacity must be at least 1")
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue(
            maxsize=self._capacity
        )
        # Where watchdog events are appended. None falls back to the
        # event log's own resolution (STACKCHAN_EVENTS_PATH env / default);
        # the HTTP daemon passes the notify.yml JSONL path so queue events
        # land in the same file as device events.
        self._event_log_path = event_log_path

    @property
    def capacity(self) -> int:
        """Return the maximum number of queued commands."""
        return self._capacity

    @property
    def depth(self) -> int:
        """Return the current number of queued commands."""
        return self._queue.qsize()

    def enqueue(self, item: QueueItem) -> None:
        """Add an item without blocking, raising QueueFull on saturation."""
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise QueueFull(self.depth, self.capacity) from exc

    def enqueue_with_backpressure(self, item: QueueItem) -> None:
        """Enqueue ``item``, shedding droppable traffic when saturated.

        On a full queue, the oldest queued droppable item is evicted (its
        response future fails with :class:`CommandDropped`) and ``item``
        takes its place. When nothing is evictable, a droppable ``item``
        is itself rejected with :class:`CommandDropped`; a non-droppable
        ``item`` falls back to the original :class:`QueueFull` rejection
        so callers keep their retry/backoff behaviour.
        """
        try:
            self.enqueue(item)
            return
        except QueueFull:
            pass

        victim = self._evict_oldest_droppable()
        if victim is not None:
            queued_ms = int((time.monotonic() - victim.enqueued_at) * 1000)
            self._record_queue_event(
                "drop_oldest",
                victim.tool_name,
                duration_ms=max(queued_ms, 0),
                detail=f"evicted for incoming {item.tool_name}",
            )
            if not victim.response_future.done():
                victim.response_future.set_exception(
                    CommandDropped(victim.tool_name, "evicted_oldest_droppable")
                )
            # Capacity freed by the eviction; a concurrent producer cannot
            # interleave here (single-threaded event loop, no awaits since
            # the eviction).
            self.enqueue(item)
            return

        if is_droppable_tool(item.tool_name):
            self._record_queue_event(
                "drop_incoming",
                item.tool_name,
                duration_ms=0,
                detail="queue full, no evictable items",
            )
            raise CommandDropped(item.tool_name, "queue_full_incoming_droppable")

        raise QueueFull(self.depth, self.capacity)

    def _evict_oldest_droppable(self) -> QueueItem | None:
        """Remove and return the oldest queued droppable item, if any."""
        raw_queue = self._queue._queue  # deque[QueueItem]; owned by this class
        for queued in raw_queue:
            if is_droppable_tool(queued.tool_name):
                raw_queue.remove(queued)
                # Keep join()/unfinished-task bookkeeping consistent for
                # the item that will never reach the dispatcher.
                self._queue.task_done()
                return queued
        return None

    async def get(self) -> QueueItem:
        """Await the next queued item in FIFO order."""
        return await self._queue.get()

    async def run_dispatcher(self, dispatch_fn: DispatchFn) -> None:
        """Dispatch commands one at a time and complete each response future.

        The injected dispatch function is awaited to completion before the
        next queue item is fetched, bounded by a per-tool watchdog timeout
        (:func:`dispatch_timeout_for`). A dispatch that exceeds its budget
        is cancelled and force-dequeued: its response future fails with
        :class:`HeadTimeout` and the loop continues with the next item, so
        one hung command can no longer stall the whole queue. Other
        exceptions from dispatch_fn are delivered to the item's
        response_future so the dispatcher loop can continue.
        """
        while True:
            item = await self.get()
            timeout_s = dispatch_timeout_for(item.tool_name)
            try:
                result = await asyncio.wait_for(dispatch_fn(item), timeout_s)
            except (asyncio.TimeoutError, TimeoutError):
                self._record_queue_event(
                    "head_timeout",
                    item.tool_name,
                    duration_ms=int(timeout_s * 1000),
                    detail="dispatch cancelled, continuing with next item",
                )
                if not item.response_future.done():
                    item.response_future.set_exception(
                        HeadTimeout(item.tool_name, timeout_s)
                    )
            except Exception as exc:
                if not item.response_future.done():
                    item.response_future.set_exception(exc)
            else:
                if not item.response_future.done():
                    item.response_future.set_result(result)
            finally:
                self._queue.task_done()

    def _record_queue_event(
        self,
        subtype: str,
        tool_name: str,
        *,
        duration_ms: int,
        detail: str,
    ) -> None:
        """Log a watchdog intervention and append it to the JSONL event log.

        Queue events reuse the stackchan event log so downstream consumers
        see drops and head timeouts next to device events. ``ts`` (firmware
        uptime) has no meaning for gateway-originated entries and is written
        as 0; ``session_id`` is the fixed marker ``"gateway"``. Persistence
        is fire-and-forget: the import is lazy and every failure is
        swallowed after a WARNING, so event-log issues can never affect
        dispatch.
        """
        logger.warning(
            "queue watchdog: %s tool=%s duration_ms=%d depth=%d/%d (%s)",
            subtype,
            tool_name,
            duration_ms,
            self.depth,
            self.capacity,
            detail,
        )
        try:
            from .event_log import log_event

            log_event(
                event_type=QUEUE_EVENT_TYPE,
                subtype=subtype,
                duration_ms=duration_ms,
                ts=0,
                session_id=QUEUE_EVENT_SESSION_ID,
                action=tool_name,
                path=self._event_log_path,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("queue event log persistence failed: %s", exc)


def build_queue_full_error(
    queue_depth: int,
    retry_after_ms: int = QUEUE_FULL_RETRY_AFTER_MS,
) -> dict[str, Any]:
    """Build the JSON-RPC inner error payload for queue saturation."""
    return {
        "code": QUEUE_FULL_ERROR_CODE,
        "message": QUEUE_FULL_MESSAGE,
        "data": {
            "queue_depth": queue_depth,
            "retry_after_ms": retry_after_ms,
        },
    }


def build_head_timeout_error(exc: HeadTimeout) -> dict[str, Any]:
    """Build the JSON-RPC inner error payload for a head-of-queue timeout."""
    return {
        "code": HEAD_TIMEOUT_ERROR_CODE,
        "message": HEAD_TIMEOUT_MESSAGE,
        "data": {
            "tool_name": exc.tool_name,
            "timeout_s": exc.timeout_s,
        },
    }


def build_dropped_error(exc: CommandDropped) -> dict[str, Any]:
    """Build the JSON-RPC inner error payload for a backpressure drop."""
    return {
        "code": DROPPED_ERROR_CODE,
        "message": DROPPED_MESSAGE,
        "data": {
            "tool_name": exc.tool_name,
            "reason": exc.reason,
        },
    }


def _capacity_from_env() -> int:
    raw_capacity = os.getenv(COMMAND_QUEUE_SIZE_ENV)
    if raw_capacity is None or raw_capacity == "":
        return DEFAULT_COMMAND_QUEUE_CAPACITY
    try:
        capacity = int(raw_capacity)
    except ValueError as exc:
        raise ValueError(
            f"{COMMAND_QUEUE_SIZE_ENV} must be an integer"
        ) from exc
    if capacity < 1:
        raise ValueError(f"{COMMAND_QUEUE_SIZE_ENV} must be at least 1")
    return capacity


def _make_smoke_item(
    response_future: asyncio.Future[Any],
    tool_name: str = "smoke.tool",
) -> QueueItem:
    return QueueItem(
        correlation_id=str(uuid.uuid4()),
        client_session_id="smoke-session",
        client_request_id=1,
        tool_name=tool_name,
        arguments={"value": "smoke"},
        response_future=response_future,
        enqueued_at=time.monotonic(),
    )


async def _run_smoke() -> None:
    queue = CommandQueue(capacity=1)
    response_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    queue.enqueue(_make_smoke_item(response_future))

    async def dispatch_fn(item: QueueItem) -> dict[str, Any]:
        return {
            "ok": True,
            "correlation_id": item.correlation_id,
            "tool_name": item.tool_name,
        }

    dispatcher = asyncio.create_task(queue.run_dispatcher(dispatch_fn))
    result = await asyncio.wait_for(response_future, timeout=1.0)
    assert result["ok"] is True
    assert result["tool_name"] == "smoke.tool"

    dispatcher.cancel()
    with suppress(asyncio.CancelledError):
        await dispatcher

    full_queue = CommandQueue(capacity=1)
    full_queue.enqueue(
        _make_smoke_item(asyncio.get_running_loop().create_future(), "full.first")
    )
    try:
        full_queue.enqueue(
            _make_smoke_item(
                asyncio.get_running_loop().create_future(),
                "full.second",
            )
        )
    except QueueFull as exc:
        assert exc.queue_depth == 1
        assert build_queue_full_error(exc.queue_depth)["code"] == -32000
    else:
        raise AssertionError("QueueFull was not raised")


if __name__ == "__main__":
    asyncio.run(_run_smoke())
    print("smoke: PASS")
