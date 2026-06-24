from __future__ import annotations

import json
from typing import AsyncGenerator, Optional

import redis.asyncio as aioredis

GOALS_STREAM = "labmate:goals"
EVENTS_STREAM_PREFIX = "labmate:events:"
TOOL_RESULTS_PREFIX = "labmate:tool-results:"


async def push_task(
    redis: aioredis.Redis,
    task_id: str,
    *,
    task: str,
    session_id: str,
    user_id: str = "",
    workspace_id: str = "",
) -> None:
    """Submit a goal to the orchestrator exactly like services/cli/redis_client.py."""
    payload = json.dumps(
        {
            "task_id": task_id,
            "task": task,
            "session_id": session_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
        }
    )
    await redis.xadd(GOALS_STREAM, {"payload": payload})


async def tail_task_events(
    redis: aioredis.Redis,
    task_id: str,
    *,
    last_id: str = "0",
    block_ms: int = 5000,
) -> AsyncGenerator[dict, None]:
    """Yield decoded events for a task, stopping after turn.done."""
    stream = f"{EVENTS_STREAM_PREFIX}{task_id}"
    cur = last_id
    while True:
        resp = await redis.xread({stream: cur}, count=50, block=block_ms)
        if not resp:
            continue
        for _stream, entries in resp:
            for entry_id, fields in entries:
                cur = entry_id
                raw = fields.get("event")
                if raw is None:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield ev
                if ev.get("type") == "turn.done":
                    return


def translate_event(raw: dict, *, turn_id: str) -> Optional[dict]:
    """Translate an orchestrator snake_case event into a frontend StreamEvent.

    Returns None for events the frontend stream contract does not include
    (e.g. answer.done — the frontend assembles the answer from answer.delta).
    """
    etype = raw.get("type")

    if etype == "turn.start":
        return {
            "type": "node.enter",
            "turnId": turn_id,
            "node": raw.get("node", "plan_node"),
            "thinkingBudget": raw.get("thinking_budget", 0),
        }

    if etype == "reasoning":
        return {"type": "reasoning.delta", "turnId": turn_id, "text": raw.get("text", "")}

    if etype == "tool.start":
        return {
            "type": "tool.start",
            "turnId": turn_id,
            "toolCall": {
                "id": raw.get("tool_id", ""),
                "name": raw.get("name", "tool"),
                "kind": raw.get("kind", "tool"),
                "summary": raw.get("summary", ""),
                "reasoningWhy": raw.get("reasoning_why", ""),
                "args": raw.get("args", {}),
            },
        }

    if etype == "tool.done":
        return {
            "type": "tool.done",
            "turnId": turn_id,
            "toolId": raw.get("tool_id", ""),
            "status": raw.get("status", "done"),
            "summary": raw.get("summary", ""),
            "result": raw.get("result"),
            "durationMs": raw.get("duration_ms", 0),
        }

    if etype == "tool.request":
        return {
            "type": "tool.request",
            "turnId": turn_id,
            "toolRequestId": raw.get("tool_request_id", ""),
            "name": raw.get("name", ""),
            "args": raw.get("args", {}),
        }

    if etype == "answer.delta":
        return {"type": "answer.delta", "turnId": turn_id, "text": raw.get("text", "")}

    if etype == "turn.done":
        return {"type": "turn.done", "turnId": turn_id, "status": raw.get("status", "complete")}

    if etype == "context":
        return {"type": "context.update", "window": raw.get("window", {})}

    if etype == "agent_status":
        return {"type": "agent.status", "status": raw.get("status", {})}

    if etype == "artifact_created":
        return {
            "type": "artifact.created",
            "turnId": turn_id,
            "artifact": raw.get("artifact", {}),
        }

    return None


async def write_tool_result(
    redis: aioredis.Redis,
    task_id: str,
    tool_request_id: str,
    result,
    error: Optional[str] = None,
) -> None:
    """Write a local-tool result frame to labmate:tool-results:<task_id>."""
    await redis.xadd(
        f"{TOOL_RESULTS_PREFIX}{task_id}",
        {
            "result": json.dumps(
                {"tool_request_id": tool_request_id, "result": result, "error": error},
                default=str,
            )
        },
        maxlen=200,
        approximate=True,
    )
