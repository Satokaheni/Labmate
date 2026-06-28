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
import os
import shlex
import time
import uuid
from typing import Any

import redis.asyncio as aioredis

from . import events

LOCAL_TOOL_NAMES: frozenset[str] = frozenset({"read_file", "write_file", "list_dir"})
TOOL_RESULTS_PREFIX = "labmate:tool-results:"
TOOL_RESULTS_MAXLEN = 200
DEFAULT_TIMEOUT_S = 30.0

RUN_TESTS_DEFAULT_CMD = "pytest"
RUN_TESTS_TIMEOUT_MS_DEFAULT = 60000
# exec_run (mcp-bridge schema) hard-caps `timeout` at 60000ms; never exceed it.
RUN_TESTS_TIMEOUT_MS_MAX = 60000


def build_run_tests_command(args: dict[str, Any]) -> tuple[str, int]:
    """Pure builder: ReAct tool args -> (shell command string, timeout_ms).

    The base runner is LABMATE_TEST_CMD (default "pytest"); callers may scope
    the run with args["path"] (a file/dir/nodeid) and args["expr"] (a pytest
    -k expression). Timeout precedence: args["timeout_ms"] > LABMATE_TEST_TIMEOUT_MS
    > RUN_TESTS_TIMEOUT_MS_DEFAULT. No subprocess is launched here — this is a
    pure, unit-testable shaping function; execution happens via the bash seam.
    """
    base = os.getenv("LABMATE_TEST_CMD", RUN_TESTS_DEFAULT_CMD).strip() or RUN_TESTS_DEFAULT_CMD
    parts = [base]

    path = str(args.get("path") or "").strip()
    if path:
        parts.append(shlex.quote(path) if (" " in path) else path)

    expr = str(args.get("expr") or "").strip()
    if expr:
        # A multi-word -k expression ("a or b") must be a single shell arg.
        parts.append("-k")
        parts.append(shlex.quote(expr) if (" " in expr) else expr)

    command = " ".join(parts)

    timeout_ms = args.get("timeout_ms")
    if timeout_ms is None:
        env_to = os.getenv("LABMATE_TEST_TIMEOUT_MS")
        timeout_ms = int(env_to) if env_to else RUN_TESTS_TIMEOUT_MS_DEFAULT
    else:
        timeout_ms = int(timeout_ms)

    if timeout_ms > RUN_TESTS_TIMEOUT_MS_MAX:
        timeout_ms = RUN_TESTS_TIMEOUT_MS_MAX

    return command, timeout_ms


def shape_run_tests_result(exit_code: int, raw_output: str) -> dict[str, Any]:
    """Pure shaper: a finished test run -> {ok, exit_code, raw_output}.

    `ok` mirrors the real process exit code (0 == pass). `raw_output` is the
    RAW combined stdout/stderr, only tail-truncated to 8000 chars — NEVER
    summarized, so the model sees real pass/fail text (the failing-assertion
    lines) and cannot fabricate "all tests pass".
    """
    raw = raw_output or ""
    return {
        "ok": int(exit_code) == 0,
        "exit_code": int(exit_code),
        "raw_output": raw[-8000:],
    }


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


def verify_written_content(requested: str, readback: Any) -> str | None:
    """Compare what we asked to write against what the file reads back.

    Returns None when the read-back content matches the requested content
    exactly (write confirmed applied). Otherwise returns an explicit error
    string the model will see in the tool result, so a write that silently
    did not apply (the "code was not successfully updated" failure) surfaces
    as a hard, visible error instead of a phantom success.

    read_file (execute_local_tool, used by both the live CLI client and the
    A/B local-tool responder) returns the canonical shape ``{"content": str}``,
    so unwrap that before comparing. Any other non-string read-back
    (None/list/etc.) is treated as a mismatch.
    """
    if isinstance(readback, dict) and isinstance(readback.get("content"), str):
        readback = readback["content"]
    if isinstance(readback, str) and readback == requested:
        return None
    got_len = len(readback) if isinstance(readback, str) else "n/a (non-text read-back)"
    return (
        "write verification failed: file content after write did not match the "
        f"content that was requested (requested {len(requested)} chars, "
        f"read back {got_len}). The write may not have applied — re-read the "
        "file and try again; do NOT report the file as updated."
    )


SANDBOX_TEST_TIMEOUT_S_MAX = 120


def build_sandbox_test_args(args: dict[str, Any], workspace: str) -> dict[str, Any]:
    """Shape ReAct run_tests args into a code-sandbox run_tests call.

    code-sandbox resolves a relative test_path against the SKILL-WORKER's cwd,
    not the task workspace, so we always hand it an ABSOLUTE path rooted at the
    workspace. Timeout is converted ms->s and clamped to SANDBOX_TEST_TIMEOUT_S_MAX.
    """
    path = str(args.get("path") or "").strip()
    if path:
        test_path = path if os.path.isabs(path) else os.path.join(workspace, path)
    else:
        test_path = workspace

    timeout_ms = args.get("timeout_ms")
    if timeout_ms is None:
        # For sandbox, default to the max timeout (sandbox is remote and robust)
        timeout_s = SANDBOX_TEST_TIMEOUT_S_MAX
    else:
        timeout_ms = int(timeout_ms)
        timeout_s = max(1, timeout_ms // 1000)
        if timeout_s > SANDBOX_TEST_TIMEOUT_S_MAX:
            timeout_s = SANDBOX_TEST_TIMEOUT_S_MAX

    out: dict[str, Any] = {
        "test_path": test_path,
        "framework": "pytest",
        "timeout": timeout_s,
    }
    expr = str(args.get("expr") or "").strip()
    if expr:
        out["expr"] = expr
    return out


def _extract_test_result_payload(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Dig the code-sandbox TestResult JSON out of a skill_router envelope."""
    result = envelope.get("result")
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            for piece in content:
                text = piece.get("text") if isinstance(piece, dict) else None
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict) and "passed" in parsed and "failed" in parsed:
                    return parsed
        if "passed" in result and "failed" in result:
            return result
    return None


def shape_sandbox_test_result(envelope: dict[str, Any]) -> dict[str, Any]:
    """code-sandbox run_tests envelope -> {ok, exit_code, raw_output}.

    Mirrors shape_run_tests_result so the verification-stop signal (_run_tests_passed)
    keeps working. ok requires failed==0 AND errors==0 AND not timed_out AND the
    dispatch itself succeeded. Any infra failure (skill_unavailable, timeout,
    dispatch_failed) shapes to ok=False with the reason in raw_output.
    """
    if not envelope.get("ok", False):
        reason = str(envelope.get("error") or "test dispatch failed")
        detail = envelope.get("detail")
        raw = f"{reason}: {detail}" if detail else reason
        return {"ok": False, "exit_code": 1, "raw_output": raw[-8000:]}

    payload = _extract_test_result_payload(envelope)
    if payload is None:
        raw = json.dumps(envelope.get("result"))[-8000:]
        return {"ok": False, "exit_code": 1, "raw_output": raw}

    failed = int(payload.get("failed") or 0)
    errors = int(payload.get("errors") or 0)
    timed_out = bool(payload.get("timed_out"))
    output = str(payload.get("output") or "")
    ok = failed == 0 and errors == 0 and not timed_out
    return {"ok": ok, "exit_code": 0 if ok else 1, "raw_output": output[-8000:]}
