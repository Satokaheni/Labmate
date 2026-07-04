"""
Local filesystem tool executor for the CLI.

When the remote orchestrator emits a tool.request event the CLI runs the tool
against the user's disk via execute_local_tool and sends the result back over
the WebSocket via the send_result callback in _ToolInterceptingStream.
read_file / write_file / list_dir / search_files are supported; every path is
confined to the workspace root.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

LOCAL_TOOL_NAMES = frozenset({"read_file", "write_file", "list_dir", "search_files"})


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
    if name == "search_files":
        query = str(args.get("query", ""))
        if not query.strip():
            return {"hits": [], "count": 0}
        subdir = str(args.get("path", "") or "").strip()
        root = _safe_path(subdir, workspace) if subdir else Path(workspace).resolve()
        glob = args.get("glob")
        try:
            max_results = int(args.get("max_results") or 200)
        except (TypeError, ValueError):
            max_results = 200
        max_results = max(1, min(max_results, 1000))
        hits = _search_files(query, root, glob, max_results)
        return {"hits": hits, "count": len(hits)}
    raise ValueError(f"unknown local tool: {name}")


def _search_files(query: str, root: Path, glob: str | None, max_results: int) -> list[dict]:
    """Regex search under root. ripgrep if available, else a bounded Python walk.
    Returns [{"file": <root-relative>, "line": int, "text": str}] (text trimmed)."""
    rg = shutil.which("rg")
    if rg:
        cmd = [
            rg,
            "--line-number",
            "--no-heading",
            "--color=never",
            "--max-count",
            str(max_results),
        ]
        if glob:
            cmd += ["-g", str(glob)]
        cmd += ["-e", query, "."]
        try:
            proc = subprocess.run(
                cmd, cwd=str(root), capture_output=True, text=True, timeout=20, check=False
            )
        except (subprocess.TimeoutExpired, OSError):
            proc = None
        if proc is not None and proc.returncode in (0, 1):  # 1 = no matches, not an error
            hits: list[dict] = []
            for line in proc.stdout.splitlines():
                # rg format: "relpath:line:text"
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                fp, ln, txt = parts
                try:
                    lineno = int(ln)
                except ValueError:
                    continue
                hits.append({"file": fp, "line": lineno, "text": txt[:400]})
                if len(hits) >= max_results:
                    break
            return hits
    # Python fallback: bounded recursive walk.
    try:
        pat = re.compile(query)
    except re.error:
        pat = re.compile(re.escape(query))
    hits = []
    import fnmatch

    for dirpath, dirnames, filenames in os.walk(root):
        # skip common noise dirs to stay bounded
        dirnames[:] = [
            d
            for d in dirnames
            if d not in {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
        ]
        for fn in filenames:
            if glob and not fnmatch.fnmatch(fn, str(glob)):
                continue
            fpath = Path(dirpath) / fn
            try:
                if fpath.stat().st_size > 2_000_000:  # skip >2MB
                    continue
                with fpath.open("r", encoding="utf-8", errors="strict") as fh:
                    for i, line in enumerate(fh, 1):
                        if pat.search(line):
                            rel = str(fpath.relative_to(root))
                            hits.append({"file": rel, "line": i, "text": line.rstrip("\n")[:400]})
                            if len(hits) >= max_results:
                                return hits
            except (OSError, UnicodeDecodeError):
                continue  # binary / unreadable — skip
    return hits
