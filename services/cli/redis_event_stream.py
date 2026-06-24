# services/cli/redis_event_stream.py
"""Redis-backed event stream (reference/legacy implementation).

Used by LabmateRedisClient. The active CLI code path uses WSEventStream from
ws_client.py instead.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator, AsyncIterator, Optional

import redis.asyncio as aioredis

EVENTS_PREFIX = "labmate:events:"
EVENTS_STREAM_PREFIX = EVENTS_PREFIX


def event_channel(task_id: str) -> str:
    return f"{EVENTS_PREFIX}{task_id}"


async def tail_events(
    redis_url: str, task_id: str, *, last_id: str = "0", block_ms: int = 5000
) -> AsyncGenerator[dict, None]:
    """Yield decoded events for a task, resuming from last_id (replay-friendly)."""
    r = aioredis.from_url(redis_url, decode_responses=True)
    stream = f"{EVENTS_STREAM_PREFIX}{task_id}"
    cur = last_id
    try:
        while True:
            resp = await r.xread({stream: cur}, count=50, block=block_ms)
            if not resp:
                continue
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    cur = entry_id
                    try:
                        yield json.loads(fields["event"])
                    except (KeyError, json.JSONDecodeError):
                        continue
    finally:
        await r.aclose()


class EventStream:
    """Wraps tail_events() with first-event detection and turn.done termination.

    Usage:
        stream = EventStream(redis_url, task_id)
        first = await stream.first(timeout=2.0)   # None -> fall back to spinner
        if first is not None:
            async for ev in stream.events():      # replays first, then continues
                ...
        await stream.aclose()
    """

    def __init__(self, redis_url: str, task_id: str) -> None:
        self._redis_url = redis_url
        self._task_id = task_id
        self._gen = tail_events(redis_url, task_id)
        self._buffered: Optional[dict] = None

    async def first(self, timeout: float) -> Optional[dict]:
        """Return the first event within timeout seconds, or None.

        The returned event is buffered and replayed as the first item of
        events(), so it is never lost.
        """
        async def _next_event() -> Optional[dict]:
            try:
                return await self._gen.__anext__()
            except StopAsyncIteration:
                return None

        try:
            ev = await asyncio.wait_for(_next_event(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if ev is not None:
            self._buffered = ev
        return ev

    async def events(self) -> AsyncIterator[dict]:
        """Yield events until (and including) turn.done, then stop."""
        if self._buffered is not None:
            ev, self._buffered = self._buffered, None
            yield ev
            if ev.get("type") == "turn.done":
                return
        async for ev in self._gen:
            yield ev
            if ev.get("type") == "turn.done":
                return

    async def aclose(self) -> None:
        await self._gen.aclose()
