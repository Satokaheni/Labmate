"""
Local filesystem tool executor for the CLI.

When the remote orchestrator emits a tool.request event we run the tool
against the user's disk and post the result back onto
labmate:tool-results:<task_id>. Only read_file / write_file / list_dir are
supported; every path is confined to the workspace root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

TOOL_RESULTS_PREFIX = "labmate:tool-results:"
LOCAL_TOOL_NAMES = frozenset({"read_file", "write_file", "list_dir"})


def _safe_path(path: str, workspace: str) -> Path:
    """Resolve `path` under `workspace`; raise ValueError if it escapes."""
    root = Path(workspace).resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path {path!r} resolves outside workspace")
    return candidate


def execute_local_tool(name: str, args: dict[str, Any], *, workspace: str) -> Any:
    """Run one local file tool synchronously. Raises on bad path / unknown tool."""
    if name == "read_file":
        p = _safe_path(str(args.get("path", "")), workspace)
        return {"content": p.read_text(encoding="utf-8")}
    if name == "write_file":
        p = _safe_path(str(args.get("path", "")), workspace)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(args.get("content", "")), encoding="utf-8")
        return {"ok": True, "bytes": len(str(args.get("content", "")))}
    if name == "list_dir":
        p = _safe_path(str(args.get("path", ".")), workspace)
        return {"entries": sorted(os.listdir(p))}
    raise ValueError(f"unknown local tool: {name}")


async def handle_tool_request(
    redis: aioredis.Redis, ev: dict[str, Any], *, workspace: str
) -> None:
    """Execute the tool for a tool.request event and XADD the result frame."""
    task_id = ev.get("task_id", "")
    tool_request_id = ev.get("tool_request_id", "")
    name = ev.get("name", "")
    args = ev.get("args", {}) or {}
    result: Any = None
    error: str | None = None
    try:
        result = execute_local_tool(name, args, workspace=workspace)
    except Exception as exc:
        error = str(exc)
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
