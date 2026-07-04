"""Transport-free skill dispatch core.

Extracted from services/skill_worker/worker.py so both the (retired) Redis
worker and the in-process SkillRouter share ONE dispatch implementation.
Runs one skill tool via a SkillRegistry and shapes the result into the
{"ok": bool, "result"|"error": ...} contract callers depend on. No Redis,
no transport of any kind — pure registry.call_tool() + result shaping.
"""

from __future__ import annotations

import logging
from typing import Any

from services.skill_runner.skill_registry import SkillRegistry, SkillUnavailable

_log = logging.getLogger("skill_dispatch")


def _jsonable(obj: Any) -> Any:
    """Make a skill result JSON-serializable.

    registry.call_tool() returns the raw MCP CallToolResult (a pydantic
    model with content blocks) — json.dumps() can't serialize it, which
    previously surfaced every successful call as "internal_error". Dump
    pydantic models to plain JSON; fall back to str() for anything else.
    """
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception:
            return str(obj)
    try:
        import json

        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


async def dispatch(registry: SkillRegistry, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one skill tool via the registry and shape the result. No transport.

    payload: {task_id?, skill, tool, arguments}. Returns
    {"ok": bool, "result"|"error": ...}.
    """
    skill = payload.get("skill", "")
    tool = payload.get("tool", "")
    arguments = payload.get("arguments", {})
    qualified = f"{skill}.{tool}"
    try:
        result = await registry.call_tool(qualified, arguments)
        # Check if the MCP result indicates a tool error (isError=True)
        # This is a normal return, not an exception, so we must inspect the result
        if hasattr(result, "isError") and result.isError:
            return {
                "ok": False,
                "error": "tool_error",
                "result": _jsonable(result),
            }
        return {"ok": True, "result": _jsonable(result)}
    except SkillUnavailable as exc:
        return {"ok": False, "error": "skill_unavailable", "detail": str(exc)}
    except Exception as exc:
        _log.error("dispatch error for %s: %r", qualified, exc)
        return {"ok": False, "error": "dispatch_failed", "detail": str(exc)}
