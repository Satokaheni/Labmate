"""Reference consumer for the agent event stream (labmate:events:<task_id>).

Used by the CLI to render tool selection / lifecycle / reasoning live, and by
tests. A WebSocket gateway for the frontend would consume this same stream the
same way (XREAD BLOCK), then relay frames — no orchestrator change needed.
"""
from __future__ import annotations

import json
from typing import AsyncGenerator

import redis.asyncio as aioredis

from services.orchestrator.events import EVENTS_STREAM_PREFIX


EVENTS_PREFIX = "labmate:events:"


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
