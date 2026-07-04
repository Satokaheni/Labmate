"""In-process async primitives for the Redis-free local harness (Piece 4).

Three classes provide, entirely in-process, the mechanisms a prior
Redis-backed harness used a message broker for — for a single-process,
single-event-loop deployment:

- ``EventBus``       the per-task agent-event stream and the local-tool
                      result-relay topic.
- ``SignalRegistry`` the cancel flag and out-of-band steer-message queue for
                      a running task.
- ``ResultRegistry``  the goal-result publish + the async wait for it.

All three are pure asyncio (no threads, no external deps) and are safe to
use only from within a single running event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any


class Subscription:
    """An async-iterable handle to one subscriber's queue on a topic.

    Each ``Subscription`` owns a private, unbounded ``asyncio.Queue``. The
    owning ``EventBus`` pushes published frames onto it via
    ``queue.put_nowait``. Iterate with ``async for frame in subscription``.

    Closing (via ``close()``/``aclose()``, ``async with``, or cancellation
    of the iterating task) unregisters the subscription from the bus and
    unblocks a pending ``__anext__`` by pushing a sentinel, which causes the
    iterator to raise ``StopAsyncIteration`` cleanly.
    """

    # Sentinel used to wake a pending get() on close so iteration can end.
    _CLOSED = object()

    def __init__(self, bus: EventBus, topic: str, queue: asyncio.Queue[Any]) -> None:
        self._bus = bus
        self._topic = topic
        self._queue: asyncio.Queue[Any] = queue
        self._closed = False

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> dict:
        # No eager `if self._closed` short-circuit: close() unregisters the
        # subscriber (so no NEW frames arrive) and appends the _CLOSED sentinel
        # AFTER any already-buffered frames. Draining the queue here yields
        # those buffered frames first, then the sentinel stops iteration — so a
        # close() never drops the final events already delivered to this
        # subscriber (important for the turn.done event-stream tail).
        item = await self._queue.get()
        if item is Subscription._CLOSED:
            self._closed = True
            raise StopAsyncIteration
        return item

    def close(self) -> None:
        """Unregister from the bus and unblock any pending iteration."""
        if self._closed:
            return
        self._closed = True
        self._bus._unsubscribe(self._topic, self._queue)
        # Wake a pending get() (if any) so the iterator stops promptly.
        try:
            self._queue.put_nowait(Subscription._CLOSED)
        except asyncio.QueueFull:
            # Unbounded queue in practice; defensive no-op if it ever isn't.
            pass

    async def aclose(self) -> None:
        self.close()

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.close()


class EventBus:
    """Per-topic async fan-out pub/sub.

    Subscribers only receive frames published after they subscribed (no
    history/replay). Publishing is fire-and-forget: it never raises and
    never blocks, even if a subscriber's queue is (theoretically) full or
    the topic has no subscribers at all.
    """

    def __init__(self) -> None:
        self._topics: dict[str, set[asyncio.Queue[Any]]] = {}

    def subscribe(self, topic: str) -> Subscription:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._topics.setdefault(topic, set()).add(queue)
        return Subscription(self, topic, queue)

    def publish(self, topic: str, frame: dict) -> None:
        subscribers = self._topics.get(topic)
        if not subscribers:
            return
        # NOTE: the SAME frame object is fanned out to every subscriber — callers
        # must treat received frames as read-only (do not mutate a received frame).
        for queue in list(subscribers):
            try:
                queue.put_nowait(frame)
            except Exception:
                # Best-effort delivery: drop for this subscriber, never raise.
                pass

    def _unsubscribe(self, topic: str, queue: asyncio.Queue[Any]) -> None:
        subscribers = self._topics.get(topic)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._topics.pop(topic, None)


class SignalRegistry:
    """Per-task steer text (consume-once) + cancel flag."""

    def __init__(self) -> None:
        self._cancelled: set[str] = set()
        self._steer: dict[str, str] = {}

    def request_cancel(self, task_id: str) -> None:
        self._cancelled.add(task_id)

    def is_cancelled(self, task_id: str) -> bool:
        return task_id in self._cancelled

    def write_steer(self, task_id: str, text: str) -> None:
        self._steer[task_id] = text

    def read_and_clear_steer(self, task_id: str) -> str | None:
        return self._steer.pop(task_id, None)

    def clear_task(self, task_id: str) -> None:
        self._cancelled.discard(task_id)
        self._steer.pop(task_id, None)


class ResultRegistry:
    """Per-task result, tolerant of set-before-wait and wait-before-set.

    The resolved VALUE is stored loop-agnostically (``_values``) in addition to
    resolving any pending waiter's future, so an already-set result is returned
    by ``wait_result`` DIRECTLY — never by awaiting the future. This means a
    result set in one event loop is readable from another. (An ``asyncio.Future``
    is bound to the loop it was created in; awaiting it from a different loop
    raises "The future belongs to a different loop" on Python < 3.12. Production
    sets/reads in the one loop, but tests that drive set + read in separate
    ``run_until_complete`` loops depend on this.)
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[dict]] = {}
        self._values: dict[str, dict] = {}

    def _get_or_create_future(self, task_id: str) -> asyncio.Future[dict]:
        future = self._futures.get(task_id)
        if future is None or future.done():
            future = asyncio.get_running_loop().create_future()
            self._futures[task_id] = future
        return future

    def set_result(self, task_id: str, result: dict) -> None:
        self._values[task_id] = result
        future = self._futures.get(task_id)
        if future is not None and not future.done():
            future.set_result(result)

    async def wait_result(self, task_id: str, timeout: float | None = None) -> dict:
        # Fast path: already resolved — return the value directly (loop-agnostic).
        if task_id in self._values:
            return self._values[task_id]
        future = self._get_or_create_future(task_id)
        if timeout is None:
            return await future
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)

    def clear_task(self, task_id: str) -> None:
        self._futures.pop(task_id, None)
        self._values.pop(task_id, None)
