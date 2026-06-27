"""Detect files written by a successful code-sandbox run, for edit-accounting.

When a client isn't attached (headless/eval) or the model chooses code-sandbox to
write files, the client-delegated write_file path isn't used, so edited_files would
miss the edit. These heuristics recover the written paths from the sandbox command/
code so reconcile_cutoff / verification accounting stay correct. Pure + heuristic:
only used as a gate alongside tests_passed, so a stray false positive can never
credit an unverified run.
"""
from __future__ import annotations

import json
import re

# shell: `> path`, `>> path` (but NOT `<`), and `tee [-a] path`
_REDIRECT_RE = re.compile(r"(?<![0-9<])>>?\s*([^\s;|&>()]+)")
_TEE_RE = re.compile(r"\btee\b\s+(?:-a\s+)?([^\s;|&()]+)")
# python: open('p', 'w'|'a'|'x'...) and Path('p').write_text/.write_bytes
_OPEN_WRITE_RE = re.compile(
    r"open\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"][^'\"]*[wax][^'\"]*['\"]"
)
_PATH_WRITE_RE = re.compile(
    r"Path\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\.write_(?:text|bytes)"
)


def _sandbox_exit_zero(result: dict) -> bool:
    if not isinstance(result, dict) or not result.get("ok", False):
        return False
    inner = result.get("result")
    if not isinstance(inner, dict):
        return False
    if inner.get("isError") is True:
        return False
    content = inner.get("content")
    if isinstance(content, list):
        for piece in content:
            text = piece.get("text") if isinstance(piece, dict) else None
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict) and "exit_code" in parsed:
                return int(parsed.get("exit_code") or 0) == 0
    return inner.get("isError") is not True


def detect_sandbox_writes(
    skill: str, tool: str, arguments: dict, result: dict
) -> set[str]:
    if skill != "code-sandbox" or tool not in ("run_shell", "run_python"):
        return set()
    if not _sandbox_exit_zero(result):
        return set()
    text = str((arguments or {}).get("cmd") or (arguments or {}).get("code") or "")
    paths: set[str] = set()
    for rx in (_REDIRECT_RE, _TEE_RE, _OPEN_WRITE_RE, _PATH_WRITE_RE):
        for m in rx.finditer(text):
            p = m.group(1).strip().strip("'\"")
            if p and p not in ("/dev/null", "/dev/stdout", "/dev/stderr"):
                paths.add(p)
    return paths
