# A/B Local-Tool Responder + Code-Sandbox Edit Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the A/B from scoring genuinely-successful c1 runs as `ok=False`. The trace showed the model generates tests, fixes the bug, and the final `run_tests` passes — but `write_file` is dead in the A/B harness (no client attached), so `edited_files` never populates and `reconcile_cutoff` can't credit the win.

**Architecture:** Two fixes. **Fix A (test harness):** the A/B harness injects goals straight into `labmate:goals` with no client, so the orchestrator's client-delegated `read_file`/`write_file`/`list_dir` (which emit a `tool.request` and block on `labmate:tool-results:<task_id>`) time out after 30s every time. Add a local-tool responder to the A/B harness that drains `tool.request` events and answers them against the workspace — exactly what the CLI does via `services/cli/event_stream.py`. This removes the measurement artifact. **Fix B (product):** also track files written through the code-sandbox workaround (`cat <<EOF > path`, `open(path,'w')`, …) into `edited_files`, so accounting is correct even when a client is absent (headless/eval) or the model legitimately edits via the sandbox.

**Tech Stack:** Python 3.11, Redis (sync `redis` in the A/B harness; the orchestrator uses `redis.asyncio`), pytest.

## Global Constraints

- Reuse, don't re-implement: Fix A uses `services/cli/local_tool_executor.execute_local_tool` for the fs op and matches `services/orchestrator/local_tools.write_tool_result`'s result-frame format exactly (field `result` = `json.dumps({"tool_request_id","result","error"})`).
- Workspace root = `os.getenv("WORKSPACE_PATH", "/workspace")` — the SAME value the orchestrator uses (`services/orchestrator/main.py:152`), so absolute fixture paths resolve.
- Honesty preserved: Fix B only adds to `edited_files` on a SUCCESSFUL sandbox run (clean exit); it never sets `tests_passed` (that stays gated on a real passing test / assertion run). `edited_files` is only a gate alongside `tests_passed` — over-detection cannot credit an unverified run.
- The A/B harness is RunPod-only and sync-Redis; the responder must run concurrently with the blocking goal poll (background thread).
- stdout-sacred, no tiktoken, Chroma client-server — unchanged here.
- The real proof of Fix A is the live RunPod `TRIALS=3` A/B; unit tests cover the pure frame-handling, not the live Redis loop.

---

### Task 1: A/B local-tool responder (Fix A)

Add a responder the A/B harness runs alongside each goal: it tails `labmate:events:<task_id>`, executes each `tool.request` against the workspace, and writes the result frame the orchestrator is blocking on.

**Files:**
- Create: `eval/seq_ab/local_tool_responder.py`
- Modify: `eval/seq_ab/run_seq_ab.py` (`run_case`)
- Test: `tests/eval/test_local_tool_responder.py` (create; add `tests/eval/__init__.py` if the package dir is new)

**Interfaces:**
- Produces:
  - `handle_event_frame(frame: dict, workspace: str) -> dict | None` — pure: returns the result-frame payload `{"tool_request_id","result","error"}` for a `tool.request` frame, or `None` for any other event. Catches fs errors into `error` (a string), `result=None`.
  - `result_stream_key(task_id: str) -> str` and `events_stream_key(task_id: str) -> str` — the two stream names.
  - `LocalToolResponder(redis_client, task_id, workspace)` with `.start()` / `.stop()` — a background thread draining events and XADD-ing result frames. (Thread/loop validated live, not unit-tested.)

- [ ] **Step 1: Write the failing tests for the pure handler**

Create `tests/eval/__init__.py` (empty) and `tests/eval/test_local_tool_responder.py`:

```python
import json
from eval.seq_ab.local_tool_responder import (
    handle_event_frame,
    result_stream_key,
    events_stream_key,
)


def test_keys():
    assert events_stream_key("t1") == "labmate:events:t1"
    assert result_stream_key("t1") == "labmate:tool-results:t1"


def test_non_tool_request_returns_none():
    assert handle_event_frame({"type": "answer.delta", "text": "hi"}, "/tmp") is None


def test_write_then_read_roundtrip(tmp_path):
    ws = str(tmp_path)
    # write_file
    w = handle_event_frame(
        {"type": "tool.request", "tool_request_id": "r1", "name": "write_file",
         "args": {"path": "sub/f.py", "content": "print(1)\n"}},
        ws,
    )
    assert w["tool_request_id"] == "r1"
    assert w["error"] is None
    assert (tmp_path / "sub" / "f.py").read_text() == "print(1)\n"
    # read_file sees it
    r = handle_event_frame(
        {"type": "tool.request", "tool_request_id": "r2", "name": "read_file",
         "args": {"path": "sub/f.py"}},
        ws,
    )
    assert r["result"]["content"] == "print(1)\n"


def test_bad_path_is_reported_as_error(tmp_path):
    out = handle_event_frame(
        {"type": "tool.request", "tool_request_id": "r3", "name": "write_file",
         "args": {"path": "../escape.py", "content": "x"}},
        str(tmp_path),
    )
    assert out["tool_request_id"] == "r3"
    assert out["error"] is not None
    assert out["result"] is None


def test_absolute_workspace_path_resolves(tmp_path):
    ws = str(tmp_path)
    out = handle_event_frame(
        {"type": "tool.request", "tool_request_id": "r4", "name": "write_file",
         "args": {"path": f"{ws}/abs.py", "content": "ok"}},
        ws,
    )
    assert out["error"] is None
    assert (tmp_path / "abs.py").read_text() == "ok"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/eval/test_local_tool_responder.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the responder**

Create `eval/seq_ab/local_tool_responder.py`:

```python
"""Local-tool responder for the A/B harness.

The orchestrator's read_file/write_file/list_dir are CLIENT-delegated: it emits a
`tool.request` on labmate:events:<task_id> and BLOCKS on labmate:tool-results:<task_id>
for a matching frame. Under the live CLI, services/cli/event_stream.py answers it.
The A/B harness injects goals with no client, so those tools time out (30s) every
call. This responder restores them: it tails the event stream, runs each tool with
the CLI's execute_local_tool, and writes the result frame the orchestrator awaits.

RunPod-only (sync redis). The pure handle_event_frame() is unit-tested; the thread
loop is validated by the live A/B.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from services.cli.local_tool_executor import execute_local_tool

EVENTS_PREFIX = "labmate:events:"
RESULTS_PREFIX = "labmate:tool-results:"
RESULTS_MAXLEN = 200


def events_stream_key(task_id: str) -> str:
    return f"{EVENTS_PREFIX}{task_id}"


def result_stream_key(task_id: str) -> str:
    return f"{RESULTS_PREFIX}{task_id}"


def handle_event_frame(frame: dict, workspace: str) -> dict | None:
    """Pure: a decoded event frame -> the result-frame payload, or None.

    Matches local_tools.write_tool_result's payload shape so the orchestrator's
    request_local_tool can match it: {"tool_request_id", "result", "error"}.
    """
    if not isinstance(frame, dict) or frame.get("type") != "tool.request":
        return None
    tool_request_id = frame.get("tool_request_id", "")
    name = frame.get("name", "")
    args = frame.get("args", {}) or {}
    try:
        result: Any = execute_local_tool(name, args, workspace=workspace)
        return {"tool_request_id": tool_request_id, "result": result, "error": None}
    except Exception as exc:  # noqa: BLE001 — surface any fs/path error to the model
        return {"tool_request_id": tool_request_id, "result": None, "error": str(exc)}


class LocalToolResponder:
    """Background thread: drain tool.request events for one task, answer them."""

    def __init__(self, redis_client, task_id: str, workspace: str):
        self._r = redis_client
        self._task_id = task_id
        self._workspace = workspace
        self._events_key = events_stream_key(task_id)
        self._results_key = result_stream_key(task_id)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_id = "0-0"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _emit_result(self, payload: dict) -> None:
        self._r.xadd(
            self._results_key,
            {"result": json.dumps(payload, default=str)},
            maxlen=RESULTS_MAXLEN,
            approximate=True,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                resp = self._r.xread({self._events_key: self._last_id}, count=50, block=500)
            except Exception:
                time.sleep(0.1)
                continue
            if not resp:
                continue
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    self._last_id = entry_id
                    raw = fields.get("event") or fields.get(b"event")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    if not raw:
                        continue
                    try:
                        frame = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    payload = handle_event_frame(frame, self._workspace)
                    if payload is not None:
                        self._emit_result(payload)
```

- [ ] **Step 4: Run the handler tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/eval/test_local_tool_responder.py -v`
Expected: PASS.

- [ ] **Step 5: Wire the responder into the A/B harness**

In `eval/seq_ab/run_seq_ab.py`, import at the top:

```python
import os
from eval.seq_ab.local_tool_responder import LocalToolResponder
```

In `run_case(r, case)`, wrap the goal injection + poll so a responder runs for the task's lifetime. Locate the block that builds `task_id`, XADDs to `labmate:goals`, and polls `labmate:result:<task_id>`, and bracket it:

```python
    task_id = f"ab-{MODE}-{case['id']}-{int(time.time())}"
    workspace = os.getenv("WORKSPACE_PATH", "/workspace")
    responder = LocalToolResponder(r, task_id, workspace)
    responder.start()
    try:
        payload = json.dumps({"task_id": task_id, "task": case["task"], "session_id": task_id})
        r.xadd("labmate:goals", {"payload": payload})
        # ... existing result-poll loop unchanged ...
    finally:
        responder.stop()
```

> Implementer note: read the real `run_case` first and graft the `responder.start()` / `try/finally: responder.stop()` around the EXISTING xadd+poll body without duplicating it. Keep the existing event-stream read (`r.xrange(labmate:events:...)`) for skill_sequence reporting — the responder only ADDs result frames, it does not consume the goal result.

- [ ] **Step 6: Commit**

```bash
git add eval/seq_ab/local_tool_responder.py eval/seq_ab/run_seq_ab.py tests/eval/__init__.py tests/eval/test_local_tool_responder.py
git commit -m "fix(ab): local-tool responder so write_file/read_file work in the A/B harness"
```

---

### Task 2: Track code-sandbox file writes as edited_files (Fix B)

Even with Fix A, the model sometimes edits via code-sandbox; and in real headless deployments a client may be absent. Detect files written through a successful code-sandbox `run_shell`/`run_python` and add them to `edited_files`, so cut-off credit (`reconcile_cutoff`) and the verification-stop accounting are correct on the sandbox path too.

**Files:**
- Create: `services/orchestrator/sandbox_edits.py`
- Modify: `services/orchestrator/coding_orchestrator.py` (the `call_skill_tool` branch, near the `is_assertion_verification` wiring)
- Test: `tests/services/orchestrator/test_sandbox_edits.py`

**Interfaces:**
- Produces: `detect_sandbox_writes(skill: str, tool: str, arguments: dict, result: dict) -> set[str]` — paths written by a SUCCESSFUL (`_sandbox_exit_zero`) code-sandbox run; empty set otherwise.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_sandbox_edits.py`:

```python
from services.orchestrator.sandbox_edits import detect_sandbox_writes


def _ok_env():
    return {"ok": True, "result": {"content": [{"type": "text",
            "text": '{"stdout": "", "stderr": "", "exit_code": 0}'}], "isError": False}}


def _fail_env():
    return {"ok": True, "result": {"content": [{"type": "text",
            "text": '{"exit_code": 1}'}], "isError": True}}


def test_heredoc_redirect_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "cat <<'EOF' > /workspace/ab_factorial.py\n...\nEOF"},
        _ok_env(),
    )
    assert "/workspace/ab_factorial.py" in paths


def test_simple_redirect_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "echo hi > notes.txt"}, _ok_env(),
    )
    assert "notes.txt" in paths


def test_tee_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "echo x | tee out/result.log"}, _ok_env(),
    )
    assert "out/result.log" in paths


def test_python_open_write_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_python",
        {"code": "open('/workspace/test_f.py', 'w').write('...')"}, _ok_env(),
    )
    assert "/workspace/test_f.py" in paths


def test_python_pathlib_write_text_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_python",
        {"code": "from pathlib import Path\nPath('a/b.py').write_text('x')"}, _ok_env(),
    )
    assert "a/b.py" in paths


def test_no_write_returns_empty():
    assert detect_sandbox_writes(
        "code-sandbox", "run_python", {"code": "print(2+2)"}, _ok_env()
    ) == set()


def test_failed_run_not_counted():
    assert detect_sandbox_writes(
        "code-sandbox", "run_shell", {"cmd": "echo x > f.txt"}, _fail_env()
    ) == set()


def test_other_skill_returns_empty():
    assert detect_sandbox_writes(
        "test-gen", "generate", {"cmd": "echo x > f.txt"}, _ok_env()
    ) == set()


def test_read_only_redirect_not_a_write():
    # input redirection / comparison must not be treated as a write
    assert detect_sandbox_writes(
        "code-sandbox", "run_shell", {"cmd": "cat < input.txt"}, _ok_env()
    ) == set()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_sandbox_edits.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the detector**

Create `services/orchestrator/sandbox_edits.py`:

```python
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
```

- [ ] **Step 4: Run the detector tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_sandbox_edits.py -v`
Expected: PASS.

- [ ] **Step 5: Wire it into the `call_skill_tool` branch**

In `services/orchestrator/coding_orchestrator.py`, import:

```python
from .sandbox_edits import detect_sandbox_writes
```

In the `elif name == "call_skill_tool" ...` branch, right after the `is_assertion_verification(...)` block added previously, add:

```python
                        # Edit-accounting: files written via code-sandbox (the
                        # workaround used when no local-tool client is attached)
                        # must count as edits so reconcile_cutoff can credit a
                        # verified run.
                        _sb_writes = detect_sandbox_writes(
                            args.get("skill", ""), args.get("tool", ""),
                            args.get("arguments", {}), res,
                        )
                        if _sb_writes:
                            edited_files |= _sb_writes
```

- [ ] **Step 6: Run the detector tests + orchestrator subset**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_sandbox_edits.py -q && PYTHONPATH=. python -m pytest tests/services/orchestrator -k "cutoff or sandbox or completion" -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/sandbox_edits.py services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_sandbox_edits.py
git commit -m "fix(react): count code-sandbox file writes as edited_files"
```

---

### Task 3: Whole-suite regression gate

- [ ] **Step 1:** Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator tests/eval -q` — Expected: PASS.
- [ ] **Step 2:** Run: `PYTHONPATH=. python -m pytest tests/ -q 2>&1 | tail -5` — Expected: full suite green (live tests skipped), no new failures.

---

## Self-Review

- **Spec coverage:** Fix A → Task 1 (responder + harness wiring); Fix B → Task 2 (detector + wiring). ✓
- **Removes the distortion (A) AND hardens accounting (B)** — both, A first, as requested.
- **Honesty preserved:** Fix B only adds to `edited_files` (a gate), never to `tests_passed`; it requires a clean sandbox exit; combined with `reconcile_cutoff`'s `edited_files AND tests_passed`, a false-positive path detection alone cannot credit an unverified run. Read-only redirection (`<`) is excluded. ✓
- **Reuse over re-implement:** Fix A uses the CLI's `execute_local_tool` and mirrors `write_tool_result`'s frame shape; workspace = `WORKSPACE_PATH` matches the orchestrator. ✓
- **Type consistency:** `handle_event_frame` returns the exact `{"tool_request_id","result","error"}` payload `request_local_tool` matches on; `detect_sandbox_writes` consumes the same skill_router envelope as `is_assertion_verification`/`shape_sandbox_test_result`. ✓
- **Live caveat:** Fix A's thread/Redis loop is validated by the RunPod `TRIALS=3` A/B (the pure handler is unit-tested). Expected after both land: skill_first **c1 → ~3/3** (write_file works → model finishes cleanly; cut-offs also creditable), with c2/c3 unaffected or improved.
