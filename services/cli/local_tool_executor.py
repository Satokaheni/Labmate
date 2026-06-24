"""
Local filesystem tool executor for the CLI.

When the remote orchestrator emits a tool.request event the CLI runs the tool
against the user's disk via execute_local_tool and sends the result back over
the WebSocket via the send_result callback in _ToolInterceptingStream.
Only read_file / write_file / list_dir are supported; every path is confined
to the workspace root.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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
