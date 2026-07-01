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
        raw_offset = args.get("offset")
        raw_limit = args.get("limit")
        if raw_offset is None and raw_limit is None:
            return {"content": p.read_text(encoding="utf-8")}
        text = p.read_text(encoding="utf-8")
        # Split on \n only (not all Unicode line boundaries) to match TypeScript behavior.
        # This ensures files with \x0b, \f, etc. are handled identically across platforms.
        lines = text.split("\n")
        # Restore newlines on all lines except possibly the last (splitlines semantics).
        lines_with_nl = [
            line + "\n" if i < len(lines) - 1 else line for i, line in enumerate(lines)
        ]
        # Drop the synthetic trailing empty string that split('\n') produces on a
        # newline-terminated file — we don't want it to count as a real line.
        last_line = lines_with_nl[-1] if lines_with_nl else ""
        normalized = lines_with_nl[:-1] if last_line == "" else lines_with_nl
        start = max(0, int(raw_offset) - 1) if raw_offset is not None else 0
        selected = normalized[start:]
        if raw_limit is not None:
            selected = selected[: int(raw_limit)]
        return {"content": "".join(selected)}
    if name == "write_file":
        p = _safe_path(str(args.get("path", "")), workspace)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(args.get("content", "")), encoding="utf-8")
        return {"ok": True, "bytes": len(str(args.get("content", "")))}
    if name == "list_dir":
        p = _safe_path(str(args.get("path", ".")), workspace)
        return {"entries": sorted(os.listdir(p))}
    raise ValueError(f"unknown local tool: {name}")
