# Test-Execution Toolchain Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `run_tests` ReAct tool actually execute the project's pytest suite on the live host, so edit/fix goals can be verified (c1/c3 in the A/B currently fail because the test path is dead).

**Architecture:** Today `run_tests` builds a `pytest …` command and routes it through the MCP bridge's `exec_run`. That path is doubly dead: (1) `exec_run`'s input schema caps `timeout` at 60000ms but `run_tests` defaults to 120000ms → schema rejection; (2) `exec_run`'s `guardRunBash` blocks any command matching `\bpytest\b`. We reroute `run_tests` to the **code-sandbox** skill's `run_tests` tool via `skill_router.execute()` (the sanctioned path for executing code — on RunPod, where Docker is blocked, code-sandbox degrades to `LocalSubprocessExecutor`, i.e. a plain `pytest` subprocess). Because code-sandbox resolves relative paths against the *skill-worker's* cwd (not the task workspace), we always pass an **absolute** `test_path` rooted at `self.workspace`. We also make unknown-tool errors enumerate the skill's valid tool names so the model stops guessing.

**Tech Stack:** Python 3.11 (orchestrator, skills), pytest + pytest-asyncio, TypeScript (mcp-bridge), Redis Streams (skill dispatch).

## Global Constraints

- stdout is sacred in every MCP server — log to stderr only (`console.error` / `logging`), never `print`/`console.log`.
- Every `litellm.acompletion` call sets `api_key="not-needed"` and `extra_body={"thinking_budget_tokens": …}`. (Not touched here, but do not regress.)
- Tests live under `tests/` mirroring `services/`; async tests use `@pytest.mark.asyncio`; assert structure, not literal LLM text.
- Service URLs come from env vars; never hardcode.
- Do NOT reference or wire the deferred Discord connector.
- New env knobs must have safe defaults and be read via `os.getenv` at call time (not import-frozen) unless an existing sibling is import-frozen.

---

### Task 1: Clamp `run_tests` exec timeout to the bridge cap (defense-in-depth)

Even though Task 3 reroutes `run_tests` off `exec_run`, `build_run_tests_command` stays in the codebase (and is the shape used if anything ever routes a pytest command through the bash seam). Its default/returned `timeout_ms` must never exceed `exec_run`'s hard schema cap of 60000ms, or the call is rejected before it runs.

**Files:**
- Modify: `services/orchestrator/local_tools.py:33-70`
- Test: `tests/services/orchestrator/test_local_tools.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `build_run_tests_command(args) -> tuple[str, int]` with the returned `timeout_ms` guaranteed `<= RUN_TESTS_TIMEOUT_MS_MAX` (60000); new module constant `RUN_TESTS_TIMEOUT_MS_MAX = 60000`.

- [ ] **Step 1: Write the failing test**

Add to `tests/services/orchestrator/test_local_tools.py`:

```python
from services.orchestrator.local_tools import (
    build_run_tests_command,
    RUN_TESTS_TIMEOUT_MS_MAX,
)


def test_build_run_tests_command_clamps_explicit_timeout_to_cap():
    cmd, timeout_ms = build_run_tests_command({"timeout_ms": 120000})
    assert cmd == "pytest"
    assert timeout_ms == RUN_TESTS_TIMEOUT_MS_MAX == 60000


def test_build_run_tests_command_default_timeout_within_cap():
    cmd, timeout_ms = build_run_tests_command({})
    assert timeout_ms <= RUN_TESTS_TIMEOUT_MS_MAX


def test_build_run_tests_command_small_timeout_unchanged():
    _, timeout_ms = build_run_tests_command({"timeout_ms": 5000})
    assert timeout_ms == 5000
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/services/orchestrator/test_local_tools.py -k clamp -v`
Expected: FAIL — `RUN_TESTS_TIMEOUT_MS_MAX` does not exist / 120000 returned.

- [ ] **Step 3: Implement the clamp**

In `services/orchestrator/local_tools.py`, below the existing constants (around line 36):

```python
RUN_TESTS_DEFAULT_CMD = "pytest"
RUN_TESTS_TIMEOUT_MS_DEFAULT = 60000
# exec_run (mcp-bridge schema) hard-caps `timeout` at 60000ms; never exceed it.
RUN_TESTS_TIMEOUT_MS_MAX = 60000
```

In `build_run_tests_command`, replace the timeout-resolution tail (currently lines 63-70) with:

```python
    timeout_ms = args.get("timeout_ms")
    if timeout_ms is None:
        env_to = os.getenv("LABMATE_TEST_TIMEOUT_MS")
        timeout_ms = int(env_to) if env_to else RUN_TESTS_TIMEOUT_MS_DEFAULT
    else:
        timeout_ms = int(timeout_ms)

    if timeout_ms > RUN_TESTS_TIMEOUT_MS_MAX:
        timeout_ms = RUN_TESTS_TIMEOUT_MS_MAX

    return command, timeout_ms
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_local_tools.py -v`
Expected: PASS (all, including the three new cases).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/local_tools.py tests/services/orchestrator/test_local_tools.py
git commit -m "fix(local-tools): clamp run_tests timeout to exec_run 60000ms cap"
```

---

### Task 2: code-sandbox `run_tests` accepts an optional `-k` expression

The flat `run_tests` tool advertises an `expr` (pytest `-k`) parameter. The code-sandbox `run_tests` tool currently has no way to pass it. Add an optional `expr` so Task 3 can forward it without losing the model's ability to scope a run.

**Files:**
- Modify: `services/skills/code-sandbox/executor.py:149-171` (DockerExecutor.run_tests), `:413-457` (LocalSubprocessExecutor.run_tests)
- Modify: `services/skills/code-sandbox/server.py:115-128` (tool schema), `:158-163` (dispatch)
- Test: `tests/services/skills/code-sandbox/test_executor.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `LocalSubprocessExecutor.run_tests(test_path, framework="pytest", timeout=…, expr: str | None = None)` and `DockerExecutor.run_tests(..., expr: str | None = None)`; the `run_tests` MCP tool schema gains an optional `expr: string`.

- [ ] **Step 1: Write the failing test**

Add to `tests/services/skills/code-sandbox/test_executor.py` (create the file if absent; import path is the skill-local module, mirror existing code-sandbox tests for sys.path setup):

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                                "services", "skills", "code-sandbox"))
from executor import LocalSubprocessExecutor  # noqa: E402


def test_run_tests_forwards_k_expression(tmp_path, monkeypatch):
    # A test file with two tests; -k selects only one.
    t = tmp_path / "test_sample.py"
    t.write_text(
        "def test_alpha():\n    assert True\n\n"
        "def test_beta():\n    assert True\n"
    )
    ex = LocalSubprocessExecutor()
    res = ex.run_tests(str(t), expr="alpha")
    # Only test_alpha selected -> exactly 1 passed.
    assert res.passed == 1
    assert res.failed == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/services/skills/code-sandbox/test_executor.py::test_run_tests_forwards_k_expression -v`
Expected: FAIL — `run_tests() got an unexpected keyword argument 'expr'`.

- [ ] **Step 3: Implement `expr` in both executors**

In `services/skills/code-sandbox/executor.py`, `LocalSubprocessExecutor.run_tests` — update the signature and command build (lines ~413-441):

```python
    def run_tests(
        self,
        test_path: str,
        framework: str = "pytest",
        timeout: int = cfg.DEFAULT_TEST_TIMEOUT,
        expr: str | None = None,
    ) -> TestResult:
        if framework != "pytest":
            raise ValueError(f"unsupported framework: {framework}")

        if not os.path.isabs(test_path):
            test_path = os.path.join(os.getcwd(), test_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [sys.executable, "-m", "pytest", test_path, "-q", "--no-header"]
            if expr:
                cmd += ["-k", expr]
            exec_result = self._run_process(cmd, timeout, tmpdir)
```

In `DockerExecutor.run_tests` (lines ~149-157), update the signature and command the same way:

```python
    def run_tests(
        self,
        test_path: str,
        framework: str = "pytest",
        timeout: int = cfg.DEFAULT_TEST_TIMEOUT,
        expr: str | None = None,
    ) -> TestResult:
        ...
        cmd = ["python", "-m", "pytest", test_path, "-q", "--no-header"]
        if expr:
            cmd += ["-k", expr]
```

- [ ] **Step 4: Wire `expr` through the MCP tool**

In `services/skills/code-sandbox/server.py`, add `expr` to the `run_tests` tool schema properties (around line 119-127):

```python
                "properties": {
                    "test_path": {"type": "string"},
                    "framework": {"type": "string", "default": "pytest"},
                    "timeout": {"type": "integer", "default": 120},
                    "expr": {"type": "string"},
                },
```

And forward it in `call_tool` (around line 158-163):

```python
        elif name == "run_tests":
            result = executor.run_tests(
                arguments["test_path"],
                framework=arguments.get("framework", "pytest"),
                timeout=arguments.get("timeout", 120),
                expr=arguments.get("expr"),
            )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/services/skills/code-sandbox/test_executor.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/skills/code-sandbox/executor.py services/skills/code-sandbox/server.py tests/services/skills/code-sandbox/test_executor.py
git commit -m "feat(code-sandbox): run_tests accepts optional -k expr"
```

---

### Task 3: Route the orchestrator `run_tests` tool through code-sandbox

Replace the dead `exec_run` path for `run_tests` with a `skill_router.execute("code-sandbox", "run_tests", …)` dispatch. Add two pure helpers in `local_tools.py` (args builder + result shaper) so the wiring stays thin and unit-testable without a live skill.

**Files:**
- Modify: `services/orchestrator/local_tools.py` (add helpers near the existing run_tests helpers)
- Modify: `services/orchestrator/coding_orchestrator.py:1014-1044` (the `run_tests` branch)
- Test: `tests/services/orchestrator/test_local_tools.py`

**Interfaces:**
- Consumes: `self.skill_router.execute(skill, tool, arguments, timeout=…)` returning `{"ok": bool, "result": <jsonable MCP CallToolResult>}` or `{"ok": False, "error": str}`; `self.workspace` (absolute task workspace path).
- Produces:
  - `build_sandbox_test_args(args: dict, workspace: str) -> dict` → `{"test_path": <abs>, "framework": "pytest", "timeout": <sec int>, "expr"?: str}`.
  - `SANDBOX_TEST_TIMEOUT_S_MAX = 120`.
  - `shape_sandbox_test_result(envelope: dict) -> dict` → `{"ok": bool, "exit_code": int, "raw_output": str}` (same shape `shape_run_tests_result` produces, so `_run_tests_passed` keeps working).

- [ ] **Step 1: Write the failing tests for the two helpers**

Add to `tests/services/orchestrator/test_local_tools.py`:

```python
from services.orchestrator.local_tools import (
    build_sandbox_test_args,
    shape_sandbox_test_result,
    SANDBOX_TEST_TIMEOUT_S_MAX,
)


def test_build_sandbox_test_args_defaults_to_workspace_root():
    a = build_sandbox_test_args({}, "/workspace/proj")
    assert a["test_path"] == "/workspace/proj"
    assert a["framework"] == "pytest"
    assert a["timeout"] == SANDBOX_TEST_TIMEOUT_S_MAX  # 120000ms default -> 120s, clamped


def test_build_sandbox_test_args_resolves_relative_path_against_workspace():
    a = build_sandbox_test_args({"path": "tests/test_x.py"}, "/workspace/proj")
    assert a["test_path"] == "/workspace/proj/tests/test_x.py"


def test_build_sandbox_test_args_keeps_absolute_path():
    a = build_sandbox_test_args({"path": "/abs/test_x.py"}, "/workspace/proj")
    assert a["test_path"] == "/abs/test_x.py"


def test_build_sandbox_test_args_converts_and_clamps_timeout():
    a = build_sandbox_test_args({"timeout_ms": 300000}, "/w")
    assert a["timeout"] == SANDBOX_TEST_TIMEOUT_S_MAX  # 300s clamped to 120s
    b = build_sandbox_test_args({"timeout_ms": 5000}, "/w")
    assert b["timeout"] == 5  # 5000ms -> 5s


def test_build_sandbox_test_args_forwards_expr():
    a = build_sandbox_test_args({"expr": "alpha or beta"}, "/w")
    assert a["expr"] == "alpha or beta"
    assert "expr" not in build_sandbox_test_args({}, "/w")


def test_shape_sandbox_test_result_passing():
    envelope = {
        "ok": True,
        "result": {
            "content": [{"type": "text", "text":
                '{"passed": 3, "failed": 0, "errors": 0, "output": "3 passed", "timed_out": false}'}],
            "isError": False,
        },
    }
    out = shape_sandbox_test_result(envelope)
    assert out == {"ok": True, "exit_code": 0, "raw_output": "3 passed"}


def test_shape_sandbox_test_result_failing():
    envelope = {
        "ok": True,
        "result": {
            "content": [{"type": "text", "text":
                '{"passed": 1, "failed": 2, "errors": 0, "output": "FAILED test_x", "timed_out": false}'}],
            "isError": True,
        },
    }
    out = shape_sandbox_test_result(envelope)
    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert "FAILED" in out["raw_output"]


def test_shape_sandbox_test_result_infra_error():
    out = shape_sandbox_test_result({"ok": False, "error": "skill_unavailable", "detail": "no tool"})
    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert "skill_unavailable" in out["raw_output"]


def test_shape_sandbox_test_result_timed_out():
    envelope = {
        "ok": True,
        "result": {"content": [{"type": "text", "text":
            '{"passed": 0, "failed": 0, "errors": 0, "output": "", "timed_out": true}'}]},
    }
    out = shape_sandbox_test_result(envelope)
    assert out["ok"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/services/orchestrator/test_local_tools.py -k "sandbox" -v`
Expected: FAIL — helpers do not exist.

- [ ] **Step 3: Implement the helpers**

Add to `services/orchestrator/local_tools.py` (below `shape_run_tests_result`):

```python
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
        env_to = os.getenv("LABMATE_TEST_TIMEOUT_MS")
        timeout_ms = int(env_to) if env_to else RUN_TESTS_TIMEOUT_MS_DEFAULT
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
```

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_local_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Rewire the `run_tests` branch in the ReAct loop**

In `services/orchestrator/coding_orchestrator.py`, replace the entire `elif name == "run_tests":` block (lines ~1014-1044) with:

```python
                    elif name == "run_tests":
                        # First-class test runner. pytest is BLOCKED through the
                        # generic exec_run bash seam (sandbox rule), so route to the
                        # code-sandbox skill's run_tests tool, which runs the REAL
                        # pytest suite (LocalSubprocessExecutor on RunPod). Always an
                        # ABSOLUTE test_path rooted at the workspace.
                        if self.skill_router is not None:
                            sb_args = build_sandbox_test_args(args, self.workspace)
                            try:
                                envelope = await self.skill_router.execute(
                                    "code-sandbox",
                                    "run_tests",
                                    sb_args,
                                    timeout=min(sb_args["timeout"] + 15, SKILL_CALL_TIMEOUT),
                                )
                                shaped = shape_sandbox_test_result(envelope)
                                content = ground_tool_result(
                                    json.dumps(shaped), LABMATE_TOOL_RESULT_BUDGET
                                )
                                if _run_tests_passed(content):
                                    tests_passed = True
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "no test runner available"})
```

Update the imports at the top of `coding_orchestrator.py` (the `from .local_tools import (…)` block around line 18-26) to add the two new helpers:

```python
from .local_tools import (
    LOCAL_TOOL_NAMES,
    build_run_tests_command,
    build_sandbox_test_args,
    request_local_tool,
    shape_run_tests_result,
    shape_sandbox_test_result,
    verify_written_content,
)
```

Confirm `SKILL_CALL_TIMEOUT` is already imported/defined in this module (it is used for skill calls). If it is not in scope, add near the other env reads:

```python
SKILL_CALL_TIMEOUT = float(os.getenv("SKILL_CALL_TIMEOUT", "135"))
```

- [ ] **Step 6: Update the verification-stop nudge text (it currently points at the dead path)**

In `services/orchestrator/verification_stop.py`, `build_verify_nudge`, replace the instruction that says "or pytest / npm test via run_bash" — pytest via run_bash is blocked. New body:

```python
    return (
        f"You edited {files} but you have not shown that the tests pass. "
        "Call the run_tests tool now to run the suite (it returns the raw "
        "pass/fail output). Read any failure, fix the code, and re-run. "
        "Only call finish once run_tests reports the tests actually pass."
    )
```

Update the docstring example accordingly. The existing `test_verification_stop.py` asserts structure (edited files appear, returns a non-empty str) — if any test asserts the literal "run_bash" substring, update it to assert "run_tests" instead.

- [ ] **Step 7: Run the affected suites**

Run: `python -m pytest tests/services/orchestrator/test_local_tools.py tests/services/orchestrator/test_verification_stop.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add services/orchestrator/local_tools.py services/orchestrator/coding_orchestrator.py services/orchestrator/verification_stop.py tests/services/orchestrator/test_local_tools.py tests/services/orchestrator/test_verification_stop.py
git commit -m "fix(react): route run_tests through code-sandbox skill (pytest is blocked on exec_run)"
```

---

### Task 4: Unknown-tool errors enumerate the skill's valid tool names

When the model calls `call_skill_tool` with a tool name that does not exist on a skill, the error gives no hint, so the model keeps guessing (`run_python` vs `run_tests` vs `run_shell`). Make the single chokepoint that detects this — `SkillRegistry.call_tool` — list the valid tool names in the error so the model self-corrects on the next turn.

**Files:**
- Modify: `services/skill_runner/skill_registry.py:195-197`
- Modify: `services/skills/code-sandbox/SKILL.md` (add explicit call_skill_tool examples)
- Test: `tests/services/skill_runner/test_skill_registry.py`

**Interfaces:**
- Consumes: `SkillProc.tools: dict[str, dict]` (tool_name -> inputSchema), already populated at registration.
- Produces: on unknown tool, `SkillUnavailable` whose message contains the requested name AND the sorted list of valid tool names for that skill.

- [ ] **Step 1: Write the failing test**

Add to `tests/services/skill_runner/test_skill_registry.py` (mirror existing fixtures in that file for constructing a `SkillRegistry` with a fake `SkillProc`; if helper fixtures exist, reuse them):

```python
import pytest
from services.skill_runner.skill_registry import SkillRegistry, SkillUnavailable, SkillProc


@pytest.mark.asyncio
async def test_call_tool_unknown_tool_lists_valid_names():
    reg = SkillRegistry()
    sp = SkillProc(manifest=None)  # adapt to the real SkillProc constructor in this file
    sp.state = "READY"
    sp.tools = {"run_python": {}, "run_tests": {}, "run_shell": {}}
    reg._skills["code-sandbox"] = sp

    with pytest.raises(SkillUnavailable) as exc:
        await reg.call_tool("code-sandbox.run_pytest", {})

    msg = str(exc.value)
    assert "run_pytest" in msg
    assert "run_python" in msg and "run_tests" in msg and "run_shell" in msg
```

> Implementer note: read the top of `tests/services/skill_runner/test_skill_registry.py` first and construct `SkillProc`/`SkillRegistry` exactly as the existing tests do (the `manifest=None` line above is a placeholder — match the real constructor and any required fields).

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/services/skill_runner/test_skill_registry.py -k unknown_tool_lists -v`
Expected: FAIL — message lacks the valid-tool list.

- [ ] **Step 3: Implement the enumerated error**

In `services/skill_runner/skill_registry.py`, `call_tool`, replace lines 195-197:

```python
        schema = sp.tools.get(tool)
        if schema is None:
            valid = ", ".join(sorted(sp.tools)) or "(none advertised)"
            raise SkillUnavailable(
                f"no tool {tool!r} in skill {ns!r}; valid tools: {valid}"
            )
```

- [ ] **Step 4: Add explicit invocation examples to the code-sandbox SKILL.md**

In `services/skills/code-sandbox/SKILL.md`, under the `## Tools` section, append:

```markdown

## Invoking these tools

Call via `call_skill_tool` with the EXACT tool name (do not guess):

- `call_skill_tool(skill="code-sandbox", tool="run_python", arguments={"code": "...", "timeout": 30})`
- `call_skill_tool(skill="code-sandbox", tool="run_shell", arguments={"cmd": "...", "timeout": 30})`
- `call_skill_tool(skill="code-sandbox", tool="run_tests", arguments={"test_path": "/abs/path", "timeout": 120})`
- `call_skill_tool(skill="code-sandbox", tool="install_packages", arguments={"packages": ["..."]})`

To verify the project's existing suite, prefer the top-level `run_tests` tool (it
routes here for you with an absolute path).
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/services/skill_runner/test_skill_registry.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/skill_runner/skill_registry.py services/skills/code-sandbox/SKILL.md tests/services/skill_runner/test_skill_registry.py
git commit -m "fix(skills): unknown-tool error enumerates valid tool names"
```

---

### Task 5: Whole-suite regression gate

**Files:** none (verification only).

- [ ] **Step 1: Run the orchestrator + memory + skill suites**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator tests/services/skill_runner tests/services/skills -q`
Expected: PASS (no regressions; new tests green).

- [ ] **Step 2: Confirm nothing else imported the old run_tests exec path**

Run: `grep -rn "build_run_tests_command\|shape_run_tests_result" services/ tests/`
Expected: `build_run_tests_command` still referenced (kept for compat) but the `run_tests` ReAct branch now uses `build_sandbox_test_args` / `shape_sandbox_test_result`. No dangling references to a removed symbol.

---

## Self-Review

- **Spec coverage:** #1 timeout clamp → Task 1; #2 route to code-sandbox (with absolute workspace path + ms→s timeout + expr) → Tasks 2+3; #3 tool-name discoverability → Task 4. ✓
- **Live-validation caveat:** mocked tests previously hid this exact class of bug. These unit tests assert the *shape contract* (clamp, abs path, result mapping, enumerated error). The true regression gate is the LIVE smoke plan (`2026-06-27-live-smoke-tests.md`) + the RunPod A/B (`TRIALS=3`). Do NOT consider this "done" until the live A/B shows c1/c3 reaching a real passing `run_tests`.
- **Type consistency:** `shape_sandbox_test_result` returns the same `{ok, exit_code, raw_output}` keys as `shape_run_tests_result`, so `_run_tests_passed` is unchanged. `build_sandbox_test_args` returns the exact kwargs code-sandbox `run_tests` accepts after Task 2 (`test_path`, `framework`, `timeout`, optional `expr`). ✓
- **No placeholders:** the only intentional adapt-to-local note is the `SkillProc` constructor in Task 4 Step 1 (the implementer must match the real fixture) — flagged explicitly. ✓
