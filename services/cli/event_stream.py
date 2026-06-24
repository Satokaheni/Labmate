"""Event stream helpers for the CLI.

The active code path uses WSEventStream (ws_client.py). EventStream and
tail_events are re-exported here for backward compatibility with redis_client.py
and existing tests.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

from .redis_event_stream import (  # noqa: F401 – re-exported
    EventStream, tail_events, EVENTS_PREFIX, event_channel
)

__all__ = ["EventStream", "tail_events", "EVENTS_PREFIX", "event_channel",
           "_ToolInterceptingStream", "run_task_with_streaming", "FIRST_EVENT_TIMEOUT"]


class _ToolInterceptingStream:
    """Wraps an EventStream to execute tool.request events against local disk.

    send_result: async callable(tool_request_id, result, error) → None
    Used by LabmateWSClient (sends tool.result over WS) or any other transport.
    """

    def __init__(self, stream, send_result, workspace: str) -> None:
        self._stream = stream
        self._send_result = send_result
        self._workspace = workspace

    async def events(self):
        from services.cli.local_tool_executor import execute_local_tool
        async for ev in self._stream.events():
            if ev.get("type") == "tool.request":
                tool_request_id = ev.get("tool_request_id", "")
                name = ev.get("name", "")
                args = ev.get("args", {}) or {}
                result = None
                error = None
                try:
                    result = execute_local_tool(name, args, workspace=self._workspace)
                except Exception as exc:
                    error = str(exc)
                await self._send_result(tool_request_id, result, error)
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
    workspace: str | None = None,
) -> dict:
    """Race live stream vs fallback spinner; always return get_result() dict.

    Subscribes to the task's event channel. If the first event arrives within
    FIRST_EVENT_TIMEOUT, renders live then reads the canonical result. Otherwise
    renders the spinner and reads the result (original behavior). Caller owns
    push_task() and printing; this helper owns the stream lifecycle.

    When workspace is provided and client has send_tool_result, tool.request
    events are intercepted and executed against local disk before being yielded
    to the renderer.
    """
    stream = client.subscribe_events(task_id)
    try:
        first = await stream.first(timeout=FIRST_EVENT_TIMEOUT)
        if first is not None:
            if workspace is not None and hasattr(client, "send_tool_result"):
                active_stream = _ToolInterceptingStream(
                    stream, client.send_tool_result, workspace
                )
            else:
                active_stream = stream
            await renderer.stream_live(active_stream)
            return await client.get_result(task_id, timeout=result_timeout)
        with renderer.thinking("Working…"):
            return await client.get_result(task_id, timeout=result_timeout)
    finally:
        await stream.aclose()
