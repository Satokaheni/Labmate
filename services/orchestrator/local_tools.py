"""
Local tool delegation: the remote orchestrator asks a local client (CLI or
Electron, via ws_gateway) to run a filesystem tool on the USER's machine.

Protocol:
  1. emit  {type:"tool.request", tool_request_id, task_id, name, args}
           onto labmate:events:<task_id>  (the normal event stream)
  2. XREAD BLOCK on  labmate:tool-results:<task_id>  for a frame whose
     decoded JSON has matching tool_request_id
  3. return its `result` payload, or raise on `error` / timeout

Only read_file / write_file / list_dir are delegated. run_bash stays
server-side (sandbox rule). Path validation lives in the CLIENT executor,
never here — the orchestrator never touches the user's disk.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import redis.asyncio as aioredis

from . import events

LOCAL_TOOL_NAMES: frozenset[str] = frozenset({"read_file", "write_file", "list_dir"})
TOOL_RESULTS_PREFIX = "labmate:tool-results:"
TOOL_RESULTS_MAXLEN = 200
DEFAULT_TIMEOUT_S = 30.0


def _current_task_id() -> str:
    em = events.current_emitter.get()
    if em is None:
        raise RuntimeError("request_local_tool called with no active EventEmitter")
    return em._task_id


async def request_local_tool(
    redis: aioredis.Redis,
    name: str,
    args: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Any:
    """Emit a tool.request and block on the per-task tool-results stream.

    Returns the decoded `result` payload. Raises TimeoutError if no matching
    result arrives within `timeout`, or RuntimeError if the client reports an
    error for this request.
    """
    task_id = _current_task_id()
    tool_request_id = uuid.uuid4().hex

    await events.emit(
        "tool.request",
        tool_request_id=tool_request_id,
        name=name,
        args=args,
    )

    results_stream = f"{TOOL_RESULTS_PREFIX}{task_id}"
    # Start reading only NEW entries; a stale result for another request must
    # not be consumed. "$" yields only frames added after this XREAD begins.
    cur = "$"
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"local tool {name!r} (request {tool_request_id}) timed out after {timeout}s"
            )
        block_ms = max(1, int(min(remaining, 5.0) * 1000))
        resp = await redis.xread({results_stream: cur}, count=10, block=block_ms)
        if not resp:
            continue
        for _stream, entries in resp:
            for entry_id, fields in entries:
                cur = entry_id
                raw = fields.get("result")
                if raw is None:
                    continue
                try:
                    frame = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if frame.get("tool_request_id") != tool_request_id:
                    continue
                err = frame.get("error")
                if err:
                    raise RuntimeError(f"local tool {name!r} failed: {err}")
                return frame.get("result")


async def write_tool_result(
    redis: aioredis.Redis,
    task_id: str,
    tool_request_id: str,
    result: Any,
    error: str | None = None,
) -> None:
    """Helper used by local clients (and tests) to post a tool.result frame."""
    await redis.xadd(
        f"{TOOL_RESULTS_PREFIX}{task_id}",
        {
            "result": json.dumps(
                {"tool_request_id": tool_request_id, "result": result, "error": error},
                default=str,
            )
        },
        maxlen=TOOL_RESULTS_MAXLEN,
        approximate=True,
    )
