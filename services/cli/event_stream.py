"""Reference consumer for the agent event stream (labmate:events:<task_id>).

Used by the CLI to render tool selection / lifecycle / reasoning live, and by
tests. A WebSocket gateway for the frontend would consume this same stream the
same way (XREAD BLOCK), then relay frames — no orchestrator change needed.
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


class _ToolInterceptingStream:
    """Wraps an EventStream to execute tool.request events against local disk."""

    def __init__(self, stream: EventStream, redis, workspace: str) -> None:
        self._stream = stream
        self._redis = redis
        self._workspace = workspace

    async def events(self):
        from services.cli.local_tool_executor import handle_tool_request
        async for ev in self._stream.events():
            if ev.get("type") == "tool.request":
                await handle_tool_request(self._redis, ev, workspace=self._workspace)
            yield ev

    async def aclose(self) -> None:
        await self._stream.aclose()


FIRST_EVENT_TIMEOUT = 2.0  # seconds to wait before falling back to spinner


async def run_task_with_streaming(
    client,
    renderer,
    task_id: str,
    result_timeout: float = 300.0,
    *,
    redis=None,
    workspace: str | None = None,
) -> dict:
    """Race live stream vs fallback spinner; always return get_result() dict.

    Subscribes to the task's event channel. If the first event arrives within
    FIRST_EVENT_TIMEOUT, renders live then reads the canonical result. Otherwise
    renders the spinner and reads the result (original behavior). Caller owns
    push_task() and printing; this helper owns the stream lifecycle.

    When redis and workspace are provided, tool.request events are intercepted
    and executed against local disk before being yielded to the renderer.
    """
    stream = client.subscribe_events(task_id)
    try:
        first = await stream.first(timeout=FIRST_EVENT_TIMEOUT)
        if first is not None:
            if redis is not None and workspace is not None:
                active_stream = _ToolInterceptingStream(stream, redis, workspace)
            else:
                active_stream = stream
            await renderer.stream_live(active_stream)
            return await client.get_result(task_id, timeout=result_timeout)
        with renderer.thinking("Working…"):
            return await client.get_result(task_id, timeout=result_timeout)
    finally:
        await stream.aclose()
