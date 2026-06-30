"""
Transport-agnostic agent event stream.

The orchestrator publishes JSON events to a per-task Redis Stream
`labmate:events:<task_id>`. Any client tails it with XREAD BLOCK (CLI directly;
a future WebSocket gateway relays). Event shapes are a subset of
FRONTEND_SPEC.md §4 StreamEvent.

A task-scoped EventEmitter lives in the `current_emitter` ContextVar, set once
per goal in main._handle, so deeply-nested emit sites (skill router, ReAct
executor) call the module-level `emit()` without threading an emitter through
every signature. No emitter set (e.g. unit tests) => emit is a no-op.

CRITICAL: emission is best-effort. A Redis failure must never break a task, so
every XADD is wrapped in try/except. Never write to stdout — log to stderr.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar
from typing import Any

import redis.asyncio as aioredis

_log = logging.getLogger("events")

# llama-server's --reasoning-budget-message injects a literal "</think>" when the
# thinking budget is hit (and budget-0 tool-selection calls can yield only that
# tag). Strip stray <think>/</think> markers so reasoning events never surface the
# raw tag as their "thinking" text.
_THINK_TAG_RE = re.compile(r"</?think>")


def clean_reasoning(text: str) -> str:
    """Remove <think>/</think> markers and surrounding whitespace from reasoning."""
    if not text:
        return ""
    return _THINK_TAG_RE.sub("", text).strip()


EVENTS_STREAM_PREFIX = "labmate:events:"
EVENTS_MAXLEN = 2000

current_emitter: ContextVar[EventEmitter | None] = ContextVar("current_emitter", default=None)


def extract_reasoning(response: Any) -> str:
    """Pull message.reasoning_content from a litellm response; '' if absent."""
    try:
        choices = getattr(response, "choices", None)
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        rc = getattr(message, "reasoning_content", None) if message else None
        return clean_reasoning(rc or "")
    except Exception:
        return ""


def reasoning_summary(text: str) -> str:
    """First non-empty line of reasoning, trimmed to 120 chars."""
    text = clean_reasoning(text)
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return (text or "")[:120]


def tool_event_display(tool_name: str, args: dict | None) -> tuple[str, str]:
    """Return (kind, display_name) for a tool.start event.

    Skill-loading/using tools are surfaced as kind='skill' and labelled with the
    SKILL's name (not the mechanism), so the UI shows what was actually used:
      - load_skill       -> the loaded skill (args['name'])
      - call_skill_tool  -> the skill whose tool ran (args['skill'])
    Every other tool is a plain kind='tool' shown by its own name. Falls back to
    the mechanism name when the expected arg is missing.
    """
    args = args or {}
    if tool_name == "call_skill_tool":
        return "skill", str(args.get("skill") or tool_name)
    if tool_name == "load_skill":
        return "skill", str(args.get("name") or tool_name)
    return "tool", tool_name


class EventEmitter:
    """Publishes ordered task events to a per-task Redis Stream (best-effort)."""

    def __init__(self, redis: aioredis.Redis, task_id: str) -> None:
        self._redis = redis
        self._task_id = task_id
        self._seq = 0

    async def emit(self, type: str, **fields: Any) -> None:
        self._seq += 1
        evt = {
            "type": type,
            "task_id": self._task_id,
            "seq": self._seq,
            "ts": time.time(),
            **fields,
        }
        try:
            await self._redis.xadd(
                f"{EVENTS_STREAM_PREFIX}{self._task_id}",
                {"event": json.dumps(evt, default=str)},
                maxlen=EVENTS_MAXLEN,
                approximate=True,
            )
        except Exception as exc:
            _log.warning("event emit failed (%s): %s", type, exc)


async def emit(type: str, **fields: Any) -> None:
    """Module-level emit: routes to the task-scoped emitter, or no-ops."""
    em = current_emitter.get()
    if em is None:
        return
    await em.emit(type, **fields)


CANCEL_PREFIX = "labmate:cancel:"


async def is_cancelled(redis: aioredis.Redis, task_id: str) -> bool:
    """Check if a cancel signal has been written for this task (best-effort)."""
    try:
        return bool(await redis.exists(f"{CANCEL_PREFIX}{task_id}"))
    except Exception:
        return False


STEER_PREFIX = "labmate:steer:"
STEER_TTL = 300  # seconds — a stale steer self-expires if never drained


async def write_steer(redis: aioredis.Redis, task_id: str, text: str) -> None:
    """Queue an out-of-band steer message for a running task (best-effort).

    Overwrites any pending-but-undrained steer (latest user instruction wins).
    """
    try:
        await redis.set(f"{STEER_PREFIX}{task_id}", text, ex=STEER_TTL)
    except Exception as exc:  # never let signalling break the caller
        _log.warning("write_steer failed for %s: %s", task_id, exc)


async def read_and_clear_steer(redis: aioredis.Redis, task_id: str) -> str | None:
    """Atomically read AND delete the pending steer for task_id (consume-once).

    Uses GETDEL so a steer is delivered to exactly one turn. Returns None when
    no steer is pending or on any Redis error (best-effort).
    """
    try:
        return await redis.getdel(f"{STEER_PREFIX}{task_id}")
    except Exception as exc:
        _log.warning("read_and_clear_steer failed for %s: %s", task_id, exc)
        return None


def current_task_id() -> str | None:
    """The active task's id from the task-scoped EventEmitter, or None.

    Mirrors local_tools._current_task_id() but returns None instead of raising
    when no emitter is set (unit tests / no active task), so the ReAct loop can
    degrade to "no steer/cancel channel" cleanly.
    """
    em = current_emitter.get()
    return em._task_id if em is not None else None
