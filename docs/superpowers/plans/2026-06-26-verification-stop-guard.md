# Verification-Stop Guard (hermes pattern) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the ReAct loop from accepting a `finish` that claims completion after editing code but without ever running tests and seeing them pass — inject a synthetic nudge and re-enter the loop, capped, falling back to an honest summary.

**Architecture:** A new pure module `services/orchestrator/verification_stop.py` exposes two side-effect-free helpers (`needs_verification`, `build_verify_nudge`). `_run_react_loop` in `coding_orchestrator.py` is wired to track, per run, the set of files edited (every successful `write_file` call) and whether a passing verification has occurred (a `run_tests` result with `ok`/`exit_code == 0`, or a `run_bash` pytest invocation that exited 0). In the `finish` branch, if `needs_verification(...)` returns True, the loop appends a synthetic `{"role": "user"}` nudge and continues instead of returning; after `MAX_VERIFY_NUDGES` nudges it accepts the finish but annotates the summary honestly. The guard is additive: a run that edited nothing, or edited and verified, finishes exactly as today.

**Tech Stack:** Python 3.11, asyncio, pytest + pytest-asyncio, pytest-bdd, respx (`fake_model` fixture). No new dependencies.

## Global Constraints

Copied verbatim from `CLAUDE.md` — every task implicitly includes these:

- **stdout is sacred in MCP servers** — never `print()`/`console.log()`; the orchestrator is not an MCP server but keep all diagnostics on `logging`/stderr.
- **llama.cpp** — every model call must set `extra_body={"thinking_budget_tokens": ...}` and pass `api_key="not-needed"`. (This plan adds NO new model calls; the guard is purely string/state logic.)
- **Gemma tokenizer — never tiktoken.** Do not import `tiktoken`.
- **Testing rules:** tests live under `tests/` mirroring `services/`; `@pytest.mark.asyncio` on all async tests; `pytest` + `pytest-asyncio` only; assert structure, not literal LLM text.
- **File naming:** Python files `snake_case.py`; Python functions `snake_case`; Python classes `PascalCase`.
- **Additive + regression-safe + deterministic.** No removals from `State` or existing behavior. A goal that edits AND runs passing tests, or a goal that edits nothing, finishes exactly as today — the guard fires ONLY on edited-without-passing-tests.
- **Env knob:** `MAX_VERIFY_NUDGES` (default `2`), read with `int(os.getenv("MAX_VERIFY_NUDGES", "2"))`.

### Dependency: `run_tests` tool (sibling plan)

This guard needs a way for the loop to observe a passing test run. A **sibling plan** — `docs/superpowers/plans/2026-06-26-run-tests-tool.md` — adds a first-class `run_tests` tool that returns `{"ok": bool, "exit_code": int, "raw_output": str}`. **ASSUME that tool exists.** This plan's wire-in (Task 3) treats a verification as having passed when EITHER:

1. the loop dispatched a `run_tests` tool call whose parsed result has `ok is True` OR `exit_code == 0`; OR
2. the loop dispatched a `run_bash` tool call whose `command` invokes pytest (contains the token `pytest`) AND whose result does not look like a failure (no `error` key and no `"failed"` / non-zero exit indicator in the parsed result).

The `run_bash` path is a best-effort secondary signal so the guard still works before/independently of `run_tests`; the `run_tests` path is the primary, deterministic signal. Both are handled by the pure `record_verification(...)` logic in Task 3's wire-in helper, kept narrow and deterministic. If the sibling plan is not yet merged, the `run_tests` branch of Task 3 is still safe (the tool name simply never appears in `_turn_tools`), and Task 3's fake_model test for the `run_tests` path documents the expected contract.

---

## File Map

| File | Create/Modify | Responsibility |
|------|---------------|----------------|
| `services/orchestrator/verification_stop.py` | **Create** | Pure helpers: `needs_verification(edited_files, tests_passed, nudges_used, max_nudges) -> bool` and `build_verify_nudge(edited_files) -> str`. No I/O, no model, no global state. Fully unit-testable. |
| `services/orchestrator/coding_orchestrator.py` | **Modify** `_run_react_loop` (currently lines 319–596) | Track `edited_files: set[str]` (on successful `write_file`) and `tests_passed: bool` (on a passing `run_tests`/`run_bash` pytest). In the `finish` branch (currently lines 435–439), consult `needs_verification`; if True and under the cap, append a synthetic `{"role":"user"}` nudge from `build_verify_nudge` and `continue`; otherwise accept finish (annotating the summary honestly once the cap is hit). |
| `tests/services/orchestrator/test_verification_stop.py` | **Create** | Unit tests for the two pure helpers — all branches, no model. |
| `tests/services/orchestrator/features/verification_stop.feature` | **Create** | `@mocked` Gherkin contract (4 scenarios). |
| `tests/services/orchestrator/test_verification_stop_bdd.py` | **Create** | pytest-bdd step defs binding the feature to `_run_react_loop` via scripted `litellm.acompletion` responses (same pattern as `test_iteration_budget_bdd.py`). |

**Verified facts (read from the live tree, branch `feat/agentic-fix-loop`):**
- `_run_react_loop` is `services/orchestrator/coding_orchestrator.py:319`. The `while True:` loop body starts at line 352; the `for tc in tool_calls:` dispatch at line 425; the `finish` branch at lines **435–439** (`if name == "finish": return {...}`); the tool-result append at lines 582–586; the cheap-turn refund at lines 592–593.
- The loop calls `acompletion_with_failover(...)` (line 368), which delegates to `litellm.acompletion` (`services/orchestrator/model_client.py:123`). Existing BDD tests therefore patch `services.orchestrator.coding_orchestrator.litellm.acompletion` with `side_effect=[...]` and it flows through — confirmed in `test_iteration_budget_bdd.py`.
- Local file tools: `LOCAL_TOOL_NAMES = frozenset({"read_file", "write_file", "list_dir"})` (`services/orchestrator/local_tools.py:28`). `write_file` is dispatched through the `elif name in LOCAL_TOOL_NAMES:` branch (lines 515–527); its result is `json.dumps({"result": result})` on success or `json.dumps({"error": ...})` on failure.
- `run_bash` is dispatched at lines 529–546; its result content is the raw command stdout/stderr string (or `{"error": ...}` JSON on exception).
- `CHEAP_TOOLS` (`iteration_budget.py:23`) and the refund logic at lines 592–593 are unaffected by this plan.
- `react_execute` (line 211) is the public dispatcher; in `skill_first`/`react` modes it calls `_run_react_loop(goal, self.max_steps)`. The `_pin_skill_first_sequencing` autouse fixture in `test_coding_orchestrator.py` pins the mode for loop tests.

---

## Behavior (BDD) — Gherkin

Full content of `tests/services/orchestrator/features/verification_stop.feature`:

```gherkin
@mocked
Feature: Verification-stop guard
  The ReAct loop must not accept a finish that claims completion after editing
  code without ever running tests and seeing them pass. When that happens the
  loop injects a synthetic user nudge and re-enters, capped at MAX_VERIFY_NUDGES;
  after the cap it accepts the finish but the summary is annotated honestly.

  Background:
    Given a verification-stop AsyncOrchestrator with no skill router and no mcp

  Scenario: Edit then finish without tests is nudged, then verifies and finishes
    Given MAX_VERIFY_NUDGES is "2"
    And the model writes file "src/app.py" on turn 1
    And the model calls finish with summary "I fixed the bug and all tests pass" on turn 2
    And the model calls run_tests on turn 3 with a passing result
    And the model calls finish with summary "tests pass" on turn 4
    When the verification-stop loop runs the goal "fix the bug in src/app.py"
    Then the result ok is True
    And the result summary contains "tests pass"
    And a verification nudge was injected exactly 1 time
    And the model was called exactly 4 times

  Scenario: Edit plus a passing test finishes immediately with no nudge
    Given MAX_VERIFY_NUDGES is "2"
    And the model writes file "src/app.py" on turn 1
    And the model calls run_tests on turn 2 with a passing result
    And the model calls finish with summary "done, tests pass" on turn 3
    When the verification-stop loop runs the goal "fix and verify src/app.py"
    Then the result ok is True
    And the result summary contains "done, tests pass"
    And a verification nudge was injected exactly 0 times

  Scenario: A goal that edits nothing finishes immediately with no nudge
    Given MAX_VERIFY_NUDGES is "2"
    And the model calls finish with summary "2 + 2 = 4" on turn 1
    When the verification-stop loop runs the goal "what is 2 plus 2"
    Then the result ok is True
    And the result summary contains "2 + 2 = 4"
    And a verification nudge was injected exactly 0 times

  Scenario: Cap respected — after MAX_VERIFY_NUDGES the finish is accepted honestly
    Given MAX_VERIFY_NUDGES is "1"
    And the model writes file "src/app.py" on turn 1
    And the model calls finish with summary "all done" on turn 2
    And the model calls finish with summary "still done" on turn 3
    When the verification-stop loop runs the goal "fix src/app.py"
    Then the result ok is True
    And a verification nudge was injected exactly 1 time
    And the result summary contains "not verified"
```

---

## Task 1: Pure helper — `needs_verification`

**Files:**
- Create: `services/orchestrator/verification_stop.py`
- Test: `tests/services/orchestrator/test_verification_stop.py`

**Interfaces:**
- Consumes: nothing (pure stdlib).
- Produces: `needs_verification(edited_files: set[str], tests_passed: bool, nudges_used: int, max_nudges: int) -> bool` — returns True iff the loop should inject another nudge instead of accepting `finish`. True only when: at least one file was edited AND tests have NOT passed AND `nudges_used < max_nudges`. False in every other case (no edits, tests passed, or cap reached).

- [ ] **Step 1: Write the failing test**

Create `tests/services/orchestrator/test_verification_stop.py`:

```python
# tests/services/orchestrator/test_verification_stop.py
from __future__ import annotations

from services.orchestrator.verification_stop import needs_verification


def test_no_edits_never_needs_verification():
    # Edited nothing -> guard must not fire, regardless of tests_passed.
    assert needs_verification(set(), tests_passed=False, nudges_used=0, max_nudges=2) is False
    assert needs_verification(set(), tests_passed=True, nudges_used=0, max_nudges=2) is False


def test_edited_without_passing_tests_under_cap_needs_verification():
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=0, max_nudges=2) is True
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=1, max_nudges=2) is True


def test_edited_with_passing_tests_does_not_need_verification():
    assert needs_verification({"src/app.py"}, tests_passed=True, nudges_used=0, max_nudges=2) is False


def test_cap_reached_does_not_need_verification():
    # nudges_used == max_nudges -> stop nudging, accept finish honestly.
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=2, max_nudges=2) is False
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=3, max_nudges=2) is False


def test_zero_cap_never_nudges():
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=0, max_nudges=0) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_verification_stop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.verification_stop'`.

- [ ] **Step 3: Write minimal implementation**

Create `services/orchestrator/verification_stop.py`:

```python
"""Verification-stop guard (hermes pattern) — pure helpers.

The ReAct loop must not accept a ``finish`` that claims completion after the
agent edited code but never ran tests and saw them pass. These two
side-effect-free functions encode that policy so the loop wiring stays thin and
the rule is unit-testable without a model.

See docs/superpowers/plans/2026-06-26-verification-stop-guard.md
"""
from __future__ import annotations


def needs_verification(
    edited_files: set[str],
    tests_passed: bool,
    nudges_used: int,
    max_nudges: int,
) -> bool:
    """Should the loop inject another verification nudge instead of finishing?

    True iff the agent edited at least one file, has NOT shown a passing test
    run, and we have not yet spent the nudge budget. False otherwise — which
    covers: edited nothing (finish as today), tests already passed (finish as
    today), and cap reached (accept finish, summary annotated honestly by the
    caller).
    """
    if not edited_files:
        return False
    if tests_passed:
        return False
    return nudges_used < max_nudges
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_verification_stop.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/verification_stop.py tests/services/orchestrator/test_verification_stop.py
git commit -m "feat(orchestrator): add needs_verification pure helper for verification-stop guard"
```

---

## Task 2: Pure helper — `build_verify_nudge`

**Files:**
- Modify: `services/orchestrator/verification_stop.py`
- Test: `tests/services/orchestrator/test_verification_stop.py` (append)

**Interfaces:**
- Consumes: `needs_verification` (same module).
- Produces: `build_verify_nudge(edited_files: set[str]) -> str` — a deterministic synthetic user-message body listing the edited files (sorted, for byte-stability) and instructing the model to run the relevant verification command, read any failure, repair, and only finish once tests pass.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_verification_stop.py`:

```python
from services.orchestrator.verification_stop import build_verify_nudge


def test_build_verify_nudge_lists_files_sorted_and_is_deterministic():
    files = {"src/b.py", "src/a.py"}
    msg1 = build_verify_nudge(files)
    msg2 = build_verify_nudge(files)
    # Deterministic: same input -> identical output (prefix-cache friendly).
    assert msg1 == msg2
    # Files appear sorted, so set ordering cannot perturb the text.
    assert "src/a.py" in msg1
    assert "src/b.py" in msg1
    assert msg1.index("src/a.py") < msg1.index("src/b.py")


def test_build_verify_nudge_instructs_run_then_fix_then_finish():
    msg = build_verify_nudge({"src/app.py"})
    lowered = msg.lower()
    # Must tell the model to run tests, read failures, fix, and only then finish.
    assert "test" in lowered
    assert "fail" in lowered or "failure" in lowered
    assert "finish" in lowered


def test_build_verify_nudge_handles_empty_set():
    # Defensive: caller only invokes this when files exist, but it must not crash.
    msg = build_verify_nudge(set())
    assert isinstance(msg, str) and msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_verification_stop.py -k build_verify_nudge -v`
Expected: FAIL — `ImportError: cannot import name 'build_verify_nudge'`.

- [ ] **Step 3: Write minimal implementation**

Append to `services/orchestrator/verification_stop.py`:

```python
def build_verify_nudge(edited_files: set[str]) -> str:
    """A synthetic user message that forces the agent back into the loop.

    Deterministic (files sorted) so the appended tail stays byte-stable across
    runs. Mirrors the hermes verification-stop nudge: run the verification
    command, read any failure, repair the code, re-run, finish only on pass.
    """
    files = ", ".join(sorted(edited_files)) if edited_files else "the files you changed"
    return (
        f"You edited {files} but you have not shown that the tests pass. "
        "Run the relevant verification command now (e.g. the run_tests tool, "
        "or pytest / npm test via run_bash). Read any failure output, fix the "
        "code, and re-run. Only call finish once the tests actually pass."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_verification_stop.py -v`
Expected: PASS — 8 passed.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/verification_stop.py tests/services/orchestrator/test_verification_stop.py
git commit -m "feat(orchestrator): add build_verify_nudge synthetic-message helper"
```

---

## Task 3: Wire the guard into `_run_react_loop`

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (`_run_react_loop`, lines 319–596)
- Test: `tests/services/orchestrator/test_coding_orchestrator.py` (append a fake-model test exercising the nudge path)

**Interfaces:**
- Consumes: `needs_verification`, `build_verify_nudge` from `services.orchestrator.verification_stop`.
- Produces: no new public signature. Adds three run-local variables inside `_run_react_loop` — `edited_files: set[str]`, `tests_passed: bool`, `verify_nudges_used: int` — plus `max_verify_nudges = int(os.getenv("MAX_VERIFY_NUDGES", "2"))`. The `finish` branch and the `write_file`/`run_tests`/`run_bash` result handling are updated. No change to the `{"ok": bool, "summary": str}` return shape.

- [ ] **Step 1: Add the import**

At the top of `coding_orchestrator.py`, after the existing `from .iteration_budget import IterationBudget, CHEAP_TOOLS` line (line 20), add:

```python
from .verification_stop import needs_verification, build_verify_nudge
```

- [ ] **Step 2: Initialize run-local guard state**

In `_run_react_loop`, immediately after the `loop_detector = LoopDetector()` line (currently line 342), add:

```python
        # Verification-stop guard (hermes pattern). Track which files this run
        # edited and whether a passing verification has been observed. The guard
        # fires ONLY when the model tries to finish after editing without a
        # passing test run, and is capped at MAX_VERIFY_NUDGES.
        edited_files: set[str] = set()
        tests_passed: bool = False
        verify_nudges_used: int = 0
        max_verify_nudges = int(os.getenv("MAX_VERIFY_NUDGES", "2"))
```

- [ ] **Step 3: Replace the `finish` branch with the guard**

Replace the current `finish` branch (lines 435–439):

```python
                    if name == "finish":
                        return {
                            "ok": True,
                            "summary": str(args.get("summary", ""))[:2000],
                        }
```

with:

```python
                    if name == "finish":
                        summary = str(args.get("summary", ""))[:2000]
                        if needs_verification(
                            edited_files, tests_passed,
                            verify_nudges_used, max_verify_nudges,
                        ):
                            verify_nudges_used += 1
                            await events.emit(
                                "verify.nudge",
                                files=sorted(edited_files),
                                nudge=verify_nudges_used,
                                max_nudges=max_verify_nudges,
                            )
                            messages.append({
                                "role": "user",
                                "content": build_verify_nudge(edited_files),
                            })
                            # Re-enter the loop: do not return, do not run the
                            # remaining tool calls in this assistant turn.
                            break
                        # Either no verification was owed, or the nudge cap was
                        # reached. If we edited without ever verifying, annotate
                        # the summary honestly rather than claiming a pass.
                        if edited_files and not tests_passed:
                            summary = (
                                summary + " [verification-stop: tests were NOT "
                                "verified to pass within the nudge budget]"
                            )[:2000]
                        return {"ok": True, "summary": summary}
```

> Note: `break` exits the `for tc in tool_calls:` loop. The enclosing `while True:` then runs the cheap-turn refund check (lines 592–593) and loops back to the next model call — which now sees the appended nudge. Because `finish` is the terminal tool, real transcripts put it last/alone in a turn, so abandoning the remaining tool calls in that turn is correct and matches the existing early-return semantics.

- [ ] **Step 4: Record edited files on a successful `write_file`**

In the `elif name in LOCAL_TOOL_NAMES:` branch (lines 515–527), after `content` is assigned, add tracking. Replace:

```python
                    elif name in LOCAL_TOOL_NAMES:
                        if self.redis is not None:
                            try:
                                result = await request_local_tool(
                                    self.redis, name, args
                                )
                                content = json.dumps({"result": result}, default=str)
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps(
                                {"error": "no local tool client connected"}
                            )
```

with:

```python
                    elif name in LOCAL_TOOL_NAMES:
                        if self.redis is not None:
                            try:
                                result = await request_local_tool(
                                    self.redis, name, args
                                )
                                content = json.dumps({"result": result}, default=str)
                                # Verification-stop: record a successful edit.
                                if name == "write_file":
                                    _path = str(args.get("path", "")).strip()
                                    if _path:
                                        edited_files.add(_path)
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps(
                                {"error": "no local tool client connected"}
                            )
```

- [ ] **Step 5: Record a passing verification — add a `run_tests` branch and extend `run_bash`**

Insert a new `run_tests` dispatch branch immediately BEFORE the existing `elif name == "run_bash":` branch (line 529). This consumes the sibling `run_tests` tool (see Dependency section):

```python
                    elif name == "run_tests":
                        if self.mcp is not None:
                            try:
                                obs = await self.mcp.call_tool(
                                    "run_tests",
                                    {
                                        "path": args.get("path", ""),
                                        "cwd": self.workspace,
                                    },
                                )
                                content = "\n".join(
                                    c.text for c in obs.content if hasattr(c, "text")
                                )
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "no test runner available"})
                        # Verification-stop: a passing run_tests result clears the guard.
                        if _run_tests_passed(content):
                            tests_passed = True
```

Then, at the END of the existing `elif name == "run_bash":` branch (immediately after `content` is assigned, before the `elif name == "code_semantic_search":` at line 548), add the best-effort pytest signal:

```python
                        # Verification-stop: a passing `pytest` via run_bash also
                        # counts as a verification (secondary signal).
                        if "pytest" in str(args.get("command", "")) and _run_bash_passed(content):
                            tests_passed = True
```

- [ ] **Step 6: Add the two narrow result-parsing helpers**

Add these module-level functions to `coding_orchestrator.py` (near the other module-level helpers such as `_infer_language`; place them above the `AsyncOrchestrator`/`CodingOrchestrator` class). They are deterministic and string-only:

```python
def _run_tests_passed(content: str) -> bool:
    """True if a run_tests tool result indicates a pass.

    The run_tests tool returns {"ok": bool, "exit_code": int, "raw_output": str}.
    A result whose JSON has ok True or exit_code 0 is a pass. Non-JSON or an
    error result is treated as NOT passed (the guard stays armed).
    """
    import json as _json
    try:
        data = _json.loads(content)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if "error" in data:
        return False
    if data.get("ok") is True:
        return True
    return data.get("exit_code") == 0


def _run_bash_passed(content: str) -> bool:
    """Best-effort: did a `pytest` run_bash invocation pass?

    run_bash returns raw stdout/stderr text (or an {"error": ...} JSON blob on
    failure). Treat an {"error": ...} JSON as failed. Otherwise treat the output
    as passing UNLESS it shows a pytest failure marker ("failed", "error",
    "Traceback"). Conservative on purpose: a false negative only costs one extra
    nudge, a false positive would defeat the guard.
    """
    import json as _json
    try:
        data = _json.loads(content)
        if isinstance(data, dict) and "error" in data:
            return False
    except (TypeError, ValueError):
        pass
    lowered = str(content).lower()
    if "failed" in lowered or "traceback" in lowered or " error" in lowered:
        return False
    return "passed" in lowered or "ok" in lowered
```

- [ ] **Step 7: Write the fake-model regression test (nudge path)**

Append to `tests/services/orchestrator/test_coding_orchestrator.py`:

```python
def _vt_tool_msg(name, arguments):
    """A litellm-style assistant message that calls a single tool (verify-stop tests)."""
    tc = MagicMock()
    tc.id = f"call-{name}"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.mark.asyncio
async def test_verification_stop_nudges_then_accepts_after_tests_pass(monkeypatch):
    """Edit -> finish(no tests) must be nudged, not accepted. After run_tests
    passes, the next finish is accepted with the honest summary intact."""
    monkeypatch.setenv("MAX_VERIFY_NUDGES", "2")
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")

    # Stub redis so write_file "succeeds" through request_local_tool.
    async def _fake_local_tool(redis, name, args):
        return {"path": args.get("path"), "written": True}
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.request_local_tool",
        AsyncMock(side_effect=_fake_local_tool),
    )
    orch.redis = MagicMock()  # truthy so the write_file branch is taken

    # Stub mcp so run_tests returns a passing result.
    mcp = AsyncMock()
    mcp_result = MagicMock()
    mcp_result.content = [MagicMock(text=json.dumps({"ok": True, "exit_code": 0, "raw_output": "1 passed"}))]
    mcp.call_tool.return_value = mcp_result
    orch.mcp = mcp

    responses = [
        _vt_tool_msg("write_file", {"path": "src/app.py", "content": "x = 1"}),
        _vt_tool_msg("finish", {"summary": "I fixed the bug and all tests pass"}),
        _vt_tool_msg("run_tests", {"path": "tests/"}),
        _vt_tool_msg("finish", {"summary": "tests pass"}),
    ]
    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=responses) as mock:
        result = await orch.react_execute("fix the bug in src/app.py")

    assert result["ok"] is True
    # The first finish was rejected and nudged; the second (post-verify) accepted.
    assert "tests pass" in result["summary"]
    # All 4 scripted turns were consumed -> the nudge re-entered the loop.
    assert mock.call_count == 4


@pytest.mark.asyncio
async def test_verification_stop_no_edits_finishes_immediately(monkeypatch):
    """A goal that edits nothing finishes on the first finish — guard never fires."""
    monkeypatch.setenv("MAX_VERIFY_NUDGES", "2")
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    responses = [_vt_tool_msg("finish", {"summary": "2 + 2 = 4"})]
    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=responses) as mock:
        result = await orch.react_execute("what is 2 plus 2")
    assert result["ok"] is True
    assert result["summary"] == "2 + 2 = 4"
    assert mock.call_count == 1


@pytest.mark.asyncio
async def test_verification_stop_cap_accepts_finish_honestly(monkeypatch):
    """After MAX_VERIFY_NUDGES the finish is accepted but annotated honestly."""
    monkeypatch.setenv("MAX_VERIFY_NUDGES", "1")
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")

    async def _fake_local_tool(redis, name, args):
        return {"path": args.get("path"), "written": True}
    monkeypatch.setattr(
        "services.orchestrator.coding_orchestrator.request_local_tool",
        AsyncMock(side_effect=_fake_local_tool),
    )
    orch.redis = MagicMock()

    responses = [
        _vt_tool_msg("write_file", {"path": "src/app.py", "content": "x = 1"}),
        _vt_tool_msg("finish", {"summary": "all done"}),     # nudged (1/1)
        _vt_tool_msg("finish", {"summary": "still done"}),   # cap reached -> accepted
    ]
    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=responses) as mock:
        result = await orch.react_execute("fix src/app.py")

    assert result["ok"] is True
    assert "not verified" in result["summary"].lower()
    assert mock.call_count == 3
```

- [ ] **Step 8: Run the new tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -k verification_stop -v`
Expected: PASS — 3 passed.

- [ ] **Step 9: Run the full coding_orchestrator suite for regressions**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -q`
Expected: PASS — all existing tests still green (the guard is additive; no-edit and edit+verify paths behave exactly as before).

- [ ] **Step 10: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): wire verification-stop guard into _run_react_loop finish branch"
```

---

## Task 4: BDD contract — feature + step defs

**Files:**
- Create: `tests/services/orchestrator/features/verification_stop.feature`
- Create: `tests/services/orchestrator/test_verification_stop_bdd.py`

**Interfaces:**
- Consumes: `AsyncOrchestrator`, `react_execute`, the `run_async` helper from `tests/conftest.py`, and the scripted-response pattern from `test_iteration_budget_bdd.py`.
- Produces: 4 bound `@mocked` scenarios.

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/verification_stop.feature` with the EXACT Gherkin from the "Behavior (BDD)" section above (copy it verbatim).

- [ ] **Step 2: Write the step-def module (the failing test)**

Create `tests/services/orchestrator/test_verification_stop_bdd.py`:

```python
# tests/services/orchestrator/test_verification_stop_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

scenarios("features/verification_stop.feature")


# ── helpers ──────────────────────────────────────────────────────────────────

def _tool_call_msg(name: str, arguments: dict):
    tc = MagicMock()
    tc.id = f"call-{name}"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(_tool_call_msg("finish", {"summary": "filler"}))


@pytest.fixture
def ctx():
    return {"responses": [], "result": None, "mock": None, "nudges": 0}


# ── Background ───────────────────────────────────────────────────────────────

@given("a verification-stop AsyncOrchestrator with no skill router and no mcp")
def _orch(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    # write_file flows through request_local_tool -> stub redis truthy.
    orch.redis = MagicMock()
    # run_tests flows through mcp.call_tool -> stub a PASSING result.
    mcp = AsyncMock()
    mcp_result = MagicMock()
    mcp_result.content = [MagicMock(
        text=json.dumps({"ok": True, "exit_code": 0, "raw_output": "1 passed"})
    )]
    mcp.call_tool.return_value = mcp_result
    orch.mcp = mcp
    ctx["orch"] = orch


# ── Given steps ──────────────────────────────────────────────────────────────

@given(parsers.parse('MAX_VERIFY_NUDGES is "{value}"'))
def _set_max(ctx, value, monkeypatch):
    monkeypatch.setenv("MAX_VERIFY_NUDGES", value)


@given(parsers.parse('the model writes file "{path}" on turn {turn:d}'))
def _write_on_turn(ctx, path, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg(
        "write_file", {"path": path, "content": "x = 1"}
    )


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_on_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn 1'))
def _finish_turn1(ctx, summary):
    _ensure_len(ctx, 1)
    ctx["responses"][0] = _tool_call_msg("finish", {"summary": summary})


@given(parsers.parse('the model calls run_tests on turn {turn:d} with a passing result'))
def _run_tests_on_turn(ctx, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("run_tests", {"path": "tests/"})


# ── When step ────────────────────────────────────────────────────────────────

@when(parsers.parse('the verification-stop loop runs the goal "{goal}"'))
def _run(ctx, goal):
    # Count nudges by intercepting the emitted verify.nudge event.
    import services.orchestrator.events as _events

    captured = []

    class _Emitter:
        async def emit(self, type, **f):
            captured.append(type)

    token = _events.current_emitter.set(_Emitter())
    try:
        with patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=ctx["responses"],
        ) as mock:
            ctx["result"] = run_async(ctx["orch"].react_execute(goal))
            ctx["mock"] = mock
    finally:
        _events.current_emitter.reset(token)
    ctx["nudges"] = captured.count("verify.nudge")


# ── Then steps ───────────────────────────────────────────────────────────────

@then("the result ok is True")
def _ok_true(ctx):
    assert ctx["result"]["ok"] is True


@then(parsers.parse('the result summary contains "{needle}"'))
def _summary_contains(ctx, needle):
    assert needle.lower() in ctx["result"]["summary"].lower()


@then(parsers.parse('a verification nudge was injected exactly {n:d} time'))
@then(parsers.parse('a verification nudge was injected exactly {n:d} times'))
def _nudge_count(ctx, n):
    assert ctx["nudges"] == n


@then(parsers.parse('the model was called exactly {n:d} times'))
def _call_count(ctx, n):
    assert ctx["mock"].call_count == n
```

> The autouse `_pin_skill_first_sequencing` fixture lives in `test_coding_orchestrator.py`, which does NOT apply here. `AsyncOrchestrator.react_execute` reads the module-level `SEQUENCING_MODE`; these scenarios drive `react_execute` with `skill_router=None`, so `_run_skill_first` returns `None` and the loop runs regardless of mode EXCEPT `replan`. Pin `skill_first` explicitly inside `_orch` if the module default is `replan`: add `import services.orchestrator.coding_orchestrator as _co; _co.SEQUENCING_MODE = "skill_first"` is NOT used — instead set it via the same monkeypatch pattern. To keep it deterministic, add this `@given` autouse pin to `_orch`:
>
> ```python
> # at the end of _orch, pin the dispatcher so the loop (not replan) runs:
> import services.orchestrator.coding_orchestrator as _co
> ctx["_mode_token"] = getattr(_co, "SEQUENCING_MODE", "skill_first")
> _co.SEQUENCING_MODE = "skill_first"
> ```
>
> and restore it in `_run`'s `finally` block: `_co.SEQUENCING_MODE = ctx["_mode_token"]`. (Implementer: prefer `monkeypatch.setattr` if a `monkeypatch` arg is added to `_orch` — cleaner teardown.)

- [ ] **Step 3: Run the BDD scenarios to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_verification_stop_bdd.py -v`
Expected: PASS — 4 scenarios passed.

- [ ] **Step 4: Run the whole orchestrator suite for regressions**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — all green (684 baseline + the new verification-stop tests; no existing test regresses).

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/verification_stop.feature tests/services/orchestrator/test_verification_stop_bdd.py
git commit -m "test(orchestrator): BDD contract for verification-stop guard"
```

---

## Self-Review

**1. Spec coverage:**
- Pure helper `needs_verification(edited_files, tests_passed, nudges_used, max_nudges) -> bool` → Task 1. ✓
- Pure helper `build_verify_nudge(edited_files) -> str` → Task 2. ✓
- Track files edited (`write_file`) within `_run_react_loop` → Task 3, Step 4. ✓
- Track tests run AND passed (`run_tests` ok/exit_code==0, or `run_bash` pytest pass) → Task 3, Steps 5–6. ✓
- `finish` branch: if edited-without-passing-tests, inject synthetic `{"role":"user"}` nudge and continue → Task 3, Step 3. ✓
- Cap at `MAX_VERIFY_NUDGES` (default 2); after cap accept finish with honest summary → Task 3, Step 3 (the `if edited_files and not tests_passed:` annotation) + cap test in Step 7. ✓
- Additive + regression-safe: no-edit and edit+verify finish exactly as today → Task 3 Steps 9, Task 4 Step 4 regression runs + the dedicated `no_edits` test. ✓
- Deterministic: `build_verify_nudge` sorts files; helpers are pure; `_run_tests_passed`/`_run_bash_passed` are string-only. ✓
- Env knob `MAX_VERIFY_NUDGES` default 2 → Global Constraints + Task 3 Step 2. ✓
- BDD: `@mocked` feature with the 4 required scenarios + step defs using the existing `fake_model`/scripted-response contract → Task 4. ✓
- Honor CLAUDE.md (no new model calls, no tiktoken, snake_case, `@pytest.mark.asyncio`) → no new `litellm` calls added; all asserts are structural. ✓

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". Every code step shows full code. The one cross-reference (BDD step defs reusing the `_tool_call_msg` shape) is repeated verbatim, not referenced. ✓

**3. Type consistency:** `needs_verification(edited_files: set[str], tests_passed: bool, nudges_used: int, max_nudges: int) -> bool` and `build_verify_nudge(edited_files: set[str]) -> str` are used with identical signatures in Tasks 1–4. The loop-local names `edited_files` / `tests_passed` / `verify_nudges_used` / `max_verify_nudges` are consistent across Steps 2–5. The result parsers `_run_tests_passed` / `_run_bash_passed` are defined in Step 6 and called in Step 5. ✓

**4. run_tests dependency (explicit):** This guard's primary verification signal is the `run_tests` tool from the sibling plan `docs/superpowers/plans/2026-06-26-run-tests-tool.md` (returns `{ok, exit_code, raw_output}`). Task 3 adds the dispatch branch for it and treats a result with `ok is True` or `exit_code == 0` as a pass; a `run_bash` pytest run that exits clean is a best-effort secondary signal. If the sibling plan is not yet merged, the `run_tests` dispatch branch is harmless (the tool name never appears), and the secondary `run_bash` path still arms/clears the guard — so this plan is independently testable, but its full intended behavior depends on `run_tests` existing. Implementers MUST land the sibling `run_tests` plan (or confirm the tool is registered in `prompt_assembler.py`) for the primary path to be exercised live.

**5. Known interaction:** The `break` in the nudged `finish` branch abandons any remaining tool calls in the same assistant turn. `finish` is terminal, so well-formed transcripts emit it alone/last; this matches the existing early-`return` semantics and the loop-detection ordering (loop detection only sees genuinely dispatched tools, which `finish` never was). No interaction with the IterationBudget refund (the nudged turn made a real `finish` decision, not a cheap read) — the refund check at lines 592–593 runs after the `for`-loop `break` and correctly sees the turn's tools.
