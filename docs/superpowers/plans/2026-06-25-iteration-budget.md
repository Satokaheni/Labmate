# Iteration Budget (grace call + cheap-call refund) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bare `for step in range(self.max_steps)` hard cut in the ReAct loop with a pure `IterationBudget` that grants ONE grace call on exhaustion and refunds cheap read-only iterations, so genuine work is never starved one step short of `finish`.

**Architecture:** A new focused, pure (no async, no I/O) module `services/orchestrator/iteration_budget.py` holds the `IterationBudget` class — a thread-safe consume/refund counter with a one-shot grace call. `AsyncOrchestrator.react_execute` constructs one budget per goal (cap from `LABMATE_MAX_ITERATIONS`, default = the existing `max_steps`), consumes a unit per ReAct turn, refunds the unit when an iteration's only tool calls were pure reads (configurable `CHEAP_TOOLS` set: `read_file`, `list_dir`, `code_semantic_search`), and on exhaustion runs exactly one grace turn before stopping with a clear `"budget exhausted"` outcome. The change is additive: with the default cap and no cheap-call refunds the loop terminates identically to today, except the exhaustion path now ends with a grace turn instead of a bare cut.

**Tech Stack:** Python 3.11+, `threading.Lock`, pytest, pytest-asyncio, pytest-bdd, respx (via the shared `fake_model` fixture), litellm (mocked).

## Global Constraints

- Python files: `snake_case.py`; classes PascalCase; functions `snake_case` (CLAUDE.md File Naming Conventions).
- Do NOT modify `core/`, `tools/`, or `main.py` — M2 baseline must stay runnable (CLAUDE.md "What NOT to Do").
- Every litellm request in this module's call path keeps `extra_body={"thinking_budget_tokens": 2048}` exactly as the existing ReAct loop sets it (CLAUDE.md Critical Rule 6 — every request must set `thinking_budget_tokens` explicitly).
- Do NOT import `tiktoken`; do NOT use `chromadb.PersistentClient`/`EphemeralClient` (CLAUDE.md "What NOT to Do"). Neither is touched here.
- Env knob: `LABMATE_MAX_ITERATIONS` read via `os.getenv`, default = the constructor's `max_steps` value (currently `6`). Read it once in `react_execute`, not at import time, so tests can monkeypatch the env per-test.
- Additive only: the public signature of `AsyncOrchestrator.react_execute(self, goal: str) -> dict` is unchanged; the return shape stays `{"ok": bool, "summary": str}`.

---

## Behavior (BDD) — Gherkin

This is the full `.feature` file. It is created verbatim in Task 4.

```gherkin
@mocked
Feature: Iteration budget with grace call and cheap-call refund
  The ReAct executor is bounded by an iteration budget rather than a bare
  step cap. When the budget is exhausted the model gets exactly one grace
  call (a final chance to call finish); a read-only iteration is refunded so
  genuine work is not starved by inspection calls; and a normal finish before
  exhaustion is completely unaffected.

  Background:
    Given an AsyncOrchestrator with no skill router and no mcp

  Scenario: Normal finish before exhaustion is unaffected
    Given the iteration budget cap is 6
    And the model calls finish on its first turn with summary "all done"
    When react_execute runs the goal "do the thing"
    Then the result ok is True
    And the result summary contains "all done"
    And the budget used count is 1

  Scenario: Budget exhausts then grants exactly one grace call
    Given the iteration budget cap is 2
    And every model turn calls run_bash with command "echo loop"
    When react_execute runs the goal "loop forever"
    Then the model was called exactly 3 times
    And the result ok is False
    And the result summary contains "budget exhausted"

  Scenario: Grace call that finishes succeeds
    Given the iteration budget cap is 1
    And the model calls run_bash with command "echo work" on turn 1
    And the model calls finish with summary "finished on grace" on turn 2
    When react_execute runs the goal "needs one more step"
    Then the result ok is True
    And the result summary contains "finished on grace"

  Scenario: A read-only iteration is refunded so an extra working step is allowed
    Given the iteration budget cap is 2
    And the model calls list_dir with path "." on turn 1
    And the model calls run_bash with command "echo a" on turn 2
    And the model calls run_bash with command "echo b" on turn 3
    And the model calls finish with summary "done after refund" on turn 4
    When react_execute runs the goal "inspect then work twice"
    Then the result ok is True
    And the result summary contains "done after refund"
    And the model was called exactly 4 times
```

---

## File Map

| File | Responsibility | Action |
|------|----------------|--------|
| `services/orchestrator/iteration_budget.py` | Pure `IterationBudget` class (consume / refund / grace / used / remaining) + `CHEAP_TOOLS` default set. No async, no I/O. | Create |
| `services/orchestrator/coding_orchestrator.py` | `react_execute` ReAct loop: replace `for step in range(self.max_steps)` with budget-driven `while` loop; track which tools ran per turn; refund cheap-only turns; run one grace turn on exhaustion. | Modify (lines ~412–611) |
| `tests/services/orchestrator/test_iteration_budget.py` | Thorough unit tests for `IterationBudget` (pure). | Create |
| `tests/services/orchestrator/features/iteration_budget.feature` | Gherkin behavior spec (`@mocked`). | Create |
| `tests/services/orchestrator/test_iteration_budget_bdd.py` | pytest-bdd step defs binding the feature to `react_execute` via a fake model. | Create |
| `tests/services/orchestrator/test_coding_orchestrator.py` | Update the one existing exhaustion test whose asserted summary string changes (`max_steps reached` → `budget exhausted`). | Modify (lines 578–600) |

**Precondition (from the foundation plan — assumed already merged):** `tests/conftest.py` provides a `fake_model` respx fixture and pytest-bdd is installed/configured. This plan's BDD step defs (Task 5) and the existing pattern of patching `services.orchestrator.coding_orchestrator.litellm.acompletion` with `side_effect=[...]` (used throughout `test_coding_orchestrator.py`) are both available. The BDD step defs below patch `litellm.acompletion` directly (matching the existing unit-test style) so they do not depend on the exact `fake_model` API surface.

---

## Task 1: IterationBudget core — consume / used / remaining

**Files:**
- Create: `services/orchestrator/iteration_budget.py`
- Test: `tests/services/orchestrator/test_iteration_budget.py`

**Interfaces:**
- Produces: `IterationBudget(max_total: int)`; methods `consume() -> bool`, `refund() -> None`, `grace() -> bool`; properties `used: int`, `remaining: int`, `grace_used: bool`. Module constant `CHEAP_TOOLS: frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_iteration_budget.py
from __future__ import annotations

import pytest

from services.orchestrator.iteration_budget import IterationBudget, CHEAP_TOOLS


@pytest.mark.mocked
class TestIterationBudgetCore:
    def test_starts_unused_with_full_remaining(self):
        b = IterationBudget(max_total=6)
        assert b.max_total == 6
        assert b.used == 0
        assert b.remaining == 6

    def test_consume_decrements_remaining_and_returns_true(self):
        b = IterationBudget(max_total=3)
        assert b.consume() is True
        assert b.used == 1
        assert b.remaining == 2

    def test_consume_returns_false_when_exhausted(self):
        b = IterationBudget(max_total=2)
        assert b.consume() is True
        assert b.consume() is True
        # Third consume is over cap -> False, used stays at the cap
        assert b.consume() is False
        assert b.used == 2
        assert b.remaining == 0

    def test_remaining_never_negative(self):
        b = IterationBudget(max_total=1)
        b.consume()
        b.consume()  # rejected
        b.consume()  # rejected
        assert b.remaining == 0
        assert b.remaining >= 0

    def test_cheap_tools_contains_pure_reads(self):
        assert "read_file" in CHEAP_TOOLS
        assert "list_dir" in CHEAP_TOOLS
        assert "code_semantic_search" in CHEAP_TOOLS
        # Writes / execution are NOT cheap
        assert "run_bash" not in CHEAP_TOOLS
        assert "write_file" not in CHEAP_TOOLS
        assert "call_skill_tool" not in CHEAP_TOOLS
        assert "finish" not in CHEAP_TOOLS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/orchestrator/test_iteration_budget.py::TestIterationBudgetCore -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.iteration_budget'`

- [ ] **Step 3: Write the minimal implementation**

```python
# services/orchestrator/iteration_budget.py
"""Iteration budget for the ReAct loop — a pure, thread-safe step counter.

Replaces the bare ``for step in range(max_steps)`` hard cut in
``AsyncOrchestrator.react_execute``. The budget:

  * consumes one unit per ReAct turn,
  * grants exactly ONE grace turn after exhaustion (a final chance for the
    model to call ``finish``),
  * refunds the unit for cheap, read-only iterations (see ``CHEAP_TOOLS``)
    so inspection calls do not starve genuine work.

Pure module: no async, no I/O, no orchestrator imports. Fully unit-testable
on its own.
"""

from __future__ import annotations

import threading

# Tool names whose iterations are refunded: pure reads / inspection that
# should not eat into the working budget. Keep this in sync with the
# read-only tools exposed by AsyncOrchestrator.react_execute.
CHEAP_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_dir",
    "code_semantic_search",
})


class IterationBudget:
    """Thread-safe iteration counter with a one-shot grace call.

    ``consume()`` decrements the budget by one and returns whether the turn
    was allowed. Once ``used`` reaches ``max_total``, ``consume()`` returns
    ``False`` and ``grace()`` may be called exactly once to allow a single
    final turn. ``refund()`` returns one unit (used for cheap read-only
    turns) but never lets ``used`` go below zero and never raises ``used``
    above ``max_total``.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._grace_used = False
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Consume one iteration. Returns True if the turn is allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (cheap read-only turns).

        Never drops below zero; never exceeds ``max_total`` (a refund cannot
        manufacture budget that was never consumed).
        """
        with self._lock:
            if self._used > 0:
                self._used -= 1

    def grace(self) -> bool:
        """Grant the single grace turn after exhaustion.

        Returns True the first time it is called, False on every subsequent
        call — so the grace turn fires exactly once.
        """
        with self._lock:
            if self._grace_used:
                return False
            self._grace_used = True
            return True

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)

    @property
    def grace_used(self) -> bool:
        with self._lock:
            return self._grace_used


__all__ = ["IterationBudget", "CHEAP_TOOLS"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/orchestrator/test_iteration_budget.py::TestIterationBudgetCore -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/iteration_budget.py tests/services/orchestrator/test_iteration_budget.py
git commit -m "feat(orchestrator): add pure IterationBudget core (consume/used/remaining)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Refund semantics — restore one unit, never exceed max_total

**Files:**
- Modify: `tests/services/orchestrator/test_iteration_budget.py` (append a test class)

**Interfaces:**
- Consumes: `IterationBudget` from Task 1 (`consume`, `refund`, `used`, `remaining`).

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/services/orchestrator/test_iteration_budget.py

@pytest.mark.mocked
class TestIterationBudgetRefund:
    def test_refund_restores_one_unit(self):
        b = IterationBudget(max_total=3)
        b.consume()
        b.consume()
        assert b.used == 2
        b.refund()
        assert b.used == 1
        assert b.remaining == 2

    def test_refund_cannot_go_below_zero(self):
        b = IterationBudget(max_total=3)
        # No consume yet — refund must be a no-op, not negative.
        b.refund()
        assert b.used == 0
        assert b.remaining == 3

    def test_refund_cannot_exceed_max_total(self):
        b = IterationBudget(max_total=2)
        b.consume()
        # Two refunds against a single consume: used floors at 0, so remaining
        # never exceeds max_total (a refund cannot manufacture budget).
        b.refund()
        b.refund()
        assert b.used == 0
        assert b.remaining == 2  # not 3

    def test_refund_then_consume_allows_extra_turn(self):
        # Cap of 2: consume a cheap turn, refund it, then 2 working turns fit.
        b = IterationBudget(max_total=2)
        assert b.consume() is True   # cheap turn
        b.refund()                   # refunded
        assert b.consume() is True   # work 1
        assert b.consume() is True   # work 2
        assert b.consume() is False  # now exhausted
        assert b.used == 2
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `pytest tests/services/orchestrator/test_iteration_budget.py::TestIterationBudgetRefund -v`
Expected: PASS — the Task 1 implementation already satisfies refund semantics (these tests lock that behavior in). If any fail, fix `refund()` in `iteration_budget.py` before continuing.

- [ ] **Step 3: No implementation change needed**

The Task 1 `refund()` already floors at zero and never exceeds `max_total`. This task exists to pin the refund contract with dedicated tests.

- [ ] **Step 4: Commit**

```bash
git add tests/services/orchestrator/test_iteration_budget.py
git commit -m "test(orchestrator): lock IterationBudget refund bounds (>=0, <=max_total)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Grace semantics — fires exactly once after exhaustion

**Files:**
- Modify: `tests/services/orchestrator/test_iteration_budget.py` (append a test class)

**Interfaces:**
- Consumes: `IterationBudget` from Task 1 (`consume`, `grace`, `grace_used`).

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/services/orchestrator/test_iteration_budget.py

@pytest.mark.mocked
class TestIterationBudgetGrace:
    def test_grace_fires_once_then_never_again(self):
        b = IterationBudget(max_total=1)
        b.consume()                 # cap reached
        assert b.consume() is False
        assert b.grace_used is False
        assert b.grace() is True    # first grace allowed
        assert b.grace_used is True
        assert b.grace() is False   # second grace denied
        assert b.grace() is False   # still denied

    def test_grace_available_even_before_exhaustion(self):
        # grace() is a one-shot flag independent of used count; the LOOP is
        # responsible for only calling it after consume() returns False.
        b = IterationBudget(max_total=5)
        assert b.grace() is True
        assert b.grace() is False

    def test_grace_does_not_change_used_count(self):
        b = IterationBudget(max_total=2)
        b.consume()
        b.consume()
        assert b.used == 2
        b.grace()
        assert b.used == 2  # grace is orthogonal to the consume counter
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/services/orchestrator/test_iteration_budget.py::TestIterationBudgetGrace -v`
Expected: PASS — Task 1's `grace()`/`grace_used` already satisfy these. If any fail, fix `grace()` before continuing.

- [ ] **Step 3: No implementation change needed**

Task 1's `grace()` already returns `True` exactly once and `False` thereafter, and does not touch `_used`. This task pins the one-shot contract.

- [ ] **Step 4: Run the full IterationBudget unit suite**

Run: `pytest tests/services/orchestrator/test_iteration_budget.py -v`
Expected: PASS (all classes: core + refund + grace)

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/test_iteration_budget.py
git commit -m "test(orchestrator): lock IterationBudget one-shot grace contract

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire IterationBudget into the ReAct loop (grace + cheap-call refund)

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (imports near line 13; loop at lines ~412–611)
- Modify: `tests/services/orchestrator/test_coding_orchestrator.py` (existing exhaustion test, lines 578–600)
- Test (new, in this task): `tests/services/orchestrator/test_coding_orchestrator.py` (append grace/refund unit tests)

**Interfaces:**
- Consumes: `IterationBudget`, `CHEAP_TOOLS` from Task 1; the existing `react_execute` structure.
- Produces: `react_execute` still returns `{"ok": bool, "summary": str}`. On exhaustion (no `finish` reached even after the grace turn) the summary is `"budget exhausted"`. Reads env `LABMATE_MAX_ITERATIONS` (default = `self.max_steps`).

- [ ] **Step 1: Add the import**

In `services/orchestrator/coding_orchestrator.py`, add after the existing local imports (the block ending at line 15, `from .local_tools import LOCAL_TOOL_NAMES, request_local_tool`):

```python
import os
from .iteration_budget import IterationBudget, CHEAP_TOOLS
```

(Note: `import os` — confirm it is not already imported at the top of the file; if it is, add only the `from .iteration_budget` line.)

- [ ] **Step 2: Write the failing wire-in tests**

Append to `tests/services/orchestrator/test_coding_orchestrator.py` (after the existing `TestReactExecute` class — reuse its `_make_orch` / `_make_tool_call_response` helpers via a new sibling class):

```python
@pytest.mark.mocked
class TestReactExecuteBudget:
    """IterationBudget wire-in: grace call on exhaustion + cheap-call refund."""

    def _make_orch(self, max_steps=6):
        from services.orchestrator.coding_orchestrator import AsyncOrchestrator
        return AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp",
                                 max_steps=max_steps)

    def _bash_resp(self, command):
        return _msg_with_tool_call("run_bash", json.dumps({"command": command}))

    def _list_dir_resp(self, path):
        return _msg_with_tool_call("list_dir", json.dumps({"path": path}))

    def _finish_resp(self, summary):
        return _msg_with_tool_call("finish", json.dumps({"summary": summary}))

    @pytest.mark.asyncio
    async def test_exhaustion_grants_exactly_one_grace_call(self):
        """Cap 2 + always-run_bash => model is called cap+1 (=3) times, then stops."""
        orch = self._make_orch(max_steps=2)
        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="output")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        responses = [
            MagicMock(choices=[MagicMock(message=self._bash_resp("echo loop"))])
            for _ in range(10)
        ]

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=responses) as m:
            result = await orch.react_execute("loop forever")

        assert m.await_count == 3  # cap (2) + one grace turn
        assert result["ok"] is False
        assert "budget exhausted" in result["summary"]

    @pytest.mark.asyncio
    async def test_grace_call_that_finishes_succeeds(self):
        """Cap 1: one working turn exhausts the budget, the grace turn calls finish."""
        orch = self._make_orch(max_steps=1)
        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="output")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        r1 = MagicMock(choices=[MagicMock(message=self._bash_resp("echo work"))])
        r2 = MagicMock(choices=[MagicMock(message=self._finish_resp("finished on grace"))])

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2]) as m:
            result = await orch.react_execute("needs one more step")

        assert m.await_count == 2
        assert result["ok"] is True
        assert "finished on grace" in result["summary"]

    @pytest.mark.asyncio
    async def test_read_only_iteration_is_refunded(self):
        """Cap 2: a list_dir turn is refunded, so two run_bash turns + finish fit."""
        orch = self._make_orch(max_steps=2)
        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="output")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        r1 = MagicMock(choices=[MagicMock(message=self._list_dir_resp("."))])
        r2 = MagicMock(choices=[MagicMock(message=self._bash_resp("echo a"))])
        r3 = MagicMock(choices=[MagicMock(message=self._bash_resp("echo b"))])
        r4 = MagicMock(choices=[MagicMock(message=self._finish_resp("done after refund"))])

        # list_dir routes through local tools; with redis=None it returns a
        # structured error but still counts as a cheap (refunded) read turn.
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2, r3, r4]) as m:
            result = await orch.react_execute("inspect then work twice")

        assert m.await_count == 4  # refund of the list_dir turn buys the 4th call
        assert result["ok"] is True
        assert "done after refund" in result["summary"]

    @pytest.mark.asyncio
    async def test_env_var_overrides_max_steps(self, monkeypatch):
        """LABMATE_MAX_ITERATIONS overrides the constructor max_steps default."""
        monkeypatch.setenv("LABMATE_MAX_ITERATIONS", "1")
        orch = self._make_orch(max_steps=6)  # constructor says 6...
        mcp = AsyncMock()
        mcp_result = MagicMock()
        mcp_result.content = [MagicMock(text="output")]
        mcp_result.isError = False
        mcp.call_tool.return_value = mcp_result
        orch.mcp = mcp

        responses = [
            MagicMock(choices=[MagicMock(message=self._bash_resp("echo loop"))])
            for _ in range(10)
        ]
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=responses) as m:
            result = await orch.react_execute("loop")

        # ...but the env knob clamps to 1 => 1 working turn + 1 grace = 2 calls.
        assert m.await_count == 2
        assert "budget exhausted" in result["summary"]
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `pytest tests/services/orchestrator/test_coding_orchestrator.py::TestReactExecuteBudget -v`
Expected: FAIL — current loop hard-cuts at `max_steps` and returns `"max_steps reached"`; there is no grace turn and no refund, so `await_count` and the summary string differ.

- [ ] **Step 4: Implement the budget-driven loop**

In `services/orchestrator/coding_orchestrator.py`, replace the loop body. The current code (lines ~412–611) is:

```python
        # ReAct loop
        try:
            for step in range(self.max_steps):
                r = await litellm.acompletion(
                    model="openai/gemma-4-31b",
                    ...
                )
                ...
                    # Append tool result
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    })

            # Max steps reached
            return {"ok": False, "summary": "max_steps reached"}

        except Exception as exc:
            return {"ok": False, "summary": f"error: {str(exc)[:1000]}"}
```

Change ONLY the loop scaffolding (the `try:` header, the `for` line, the per-turn refund bookkeeping, and the terminal `return`). Keep the entire inner body (the litellm call, reasoning emit, assistant-turn append, tool-call dispatch, event emits, tool-result append) byte-for-byte identical. The new scaffolding:

```python
        # ReAct loop — bounded by an IterationBudget (replaces the bare
        # range(max_steps) cap). The budget grants ONE grace turn after
        # exhaustion and refunds cheap read-only iterations (CHEAP_TOOLS).
        cap = int(os.getenv("LABMATE_MAX_ITERATIONS", str(self.max_steps)))
        budget = IterationBudget(max_total=cap)
        try:
            while True:
                # Consume one unit; on exhaustion take the single grace turn,
                # else stop with a clear "budget exhausted" outcome.
                if not budget.consume():
                    if not budget.grace():
                        return {"ok": False, "summary": "budget exhausted"}
                    # grace turn: fall through and run one more iteration.

                # Track tools used this turn so a cheap-only turn can be refunded.
                _turn_tools: list[str] = []

                r = await litellm.acompletion(
                    model="openai/gemma-4-31b",
                    api_base=self._gemma_base,
                    api_key="not-needed",
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    extra_body={"thinking_budget_tokens": 2048},
                )

                msg = r.choices[0].message

                # Emit reasoning event if present
                _turn_reasoning = events.extract_reasoning(r)
                if _turn_reasoning:
                    await events.emit(
                        "reasoning", node="execute",
                        summary=events.reasoning_summary(_turn_reasoning),
                        text=_turn_reasoning,
                    )

                # Check for tool calls early (before appending assistant turn)
                tool_calls = getattr(msg, "tool_calls", None)

                # Append assistant turn
                if hasattr(msg, "model_dump"):
                    msg_dict = msg.model_dump()
                else:
                    msg_dict = {
                        "role": "assistant",
                        "content": msg.content or "",
                    }
                    if tool_calls:
                        msg_dict["tool_calls"] = [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in tool_calls
                        ]
                messages.append(msg_dict)

                # Check for tool calls (already extracted above)
                if not tool_calls:
                    # No tool calls — return the content directly
                    return {
                        "ok": True,
                        "summary": (msg.content or "")[:2000],
                    }

                # Process each tool call
                for tc in tool_calls:
                    name = tc.function.name
                    _turn_tools.append(name)
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except (json.JSONDecodeError, ValueError):
                        args = {}

                    content = ""

                    if name == "finish":
                        return {
                            "ok": True,
                            "summary": str(args.get("summary", ""))[:2000],
                        }

                    # ── KEEP the entire existing tool-dispatch + event-emit body
                    #    here UNCHANGED: tool.start emit, load_skill / call_skill_tool /
                    #    LOCAL_TOOL_NAMES / run_bash / code_semantic_search branches,
                    #    artifact emit, tool.done emit, and the tool-result append. ──

                    # Append tool result
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    })

                # Refund this turn if EVERY tool call it made was a cheap read.
                # Pure inspection (read_file / list_dir / code_semantic_search)
                # must not starve genuine work. A turn with no tool calls already
                # returned above, so _turn_tools is non-empty here.
                if _turn_tools and all(t in CHEAP_TOOLS for t in _turn_tools):
                    budget.refund()

        except Exception as exc:
            return {"ok": False, "summary": f"error: {str(exc)[:1000]}"}
```

**Implementation note for the engineer:** the block marked `── KEEP … UNCHANGED ──` is the existing code at lines ~487–608 (from `# Emit tool.start for all non-finish tools` through the `tool.done` emit, immediately before `# Append tool result`). Do not retype it — leave those lines exactly as they are. The only edits are: (a) the new `cap`/`budget` lines and `while True:` replacing `for step in range(self.max_steps):`; (b) the `_turn_tools` list initialisation and the `_turn_tools.append(name)` line; (c) the refund check at the end of the `while` body; (d) deleting the old `# Max steps reached` / `return {"ok": False, "summary": "max_steps reached"}` lines (the budget-exhausted return now lives at the top of the loop).

- [ ] **Step 5: Run the new wire-in tests**

Run: `pytest tests/services/orchestrator/test_coding_orchestrator.py::TestReactExecuteBudget -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Update the existing exhaustion test for the new summary string**

The pre-existing test at lines 578–600 asserts `"max_steps" in result["summary"]`. The termination semantics intentionally changed: exhaustion now returns `"budget exhausted"` (after the grace turn). Update it. The test uses `max_steps=2` and supplies only two `run_bash` responses via `side_effect`, but the loop now makes a third (grace) model call — so add a third response so `side_effect` does not raise `StopIteration`.

Replace lines 578–600 (`test_react_execute_max_steps_exhausted`) with:

```python
    @pytest.mark.asyncio
    async def test_react_execute_budget_exhausted(self):
        """Loop exhausts the budget (incl. the grace turn) without finish — ok=False."""
        orch = self._make_orch(max_steps=2)

        # cap=2 working turns + 1 grace turn = 3 model calls before stopping.
        r1 = self._make_tool_call_response("run_bash", {"command": "ls"})
        r2 = self._make_tool_call_response("run_bash", {"command": "pwd"})
        r3 = self._make_tool_call_response("run_bash", {"command": "whoami"})

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[r1, r2, r3]):
            # Mock MCP to return bash result
            mcp = AsyncMock()
            mcp_result = MagicMock()
            mcp_result.content = [MagicMock(text="output")]
            mcp_result.isError = False
            mcp.call_tool.return_value = mcp_result
            orch.mcp = mcp

            result = await orch.react_execute("do something")
            assert result["ok"] is False
            assert "budget exhausted" in result["summary"]
```

- [ ] **Step 7: Run the full coding_orchestrator suite to confirm no regressions**

Run: `pytest tests/services/orchestrator/test_coding_orchestrator.py -v`
Expected: PASS — all prior tests still green. The default cap equals `max_steps` (6) so finish-before-exhaustion paths (`test_react_execute_finish_immediately`, `test_react_execute_direct_content_no_tools`, `test_react_execute_with_skill_router_load_skill`, `test_react_execute_run_bash_without_mcp`, etc.) terminate exactly as before; only the renamed exhaustion test changed.

- [ ] **Step 8: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): budget-driven ReAct loop with grace call + cheap-call refund

Replaces the bare range(max_steps) cap with IterationBudget: one grace turn
on exhaustion (final chance to call finish) and a refund for read-only turns
(read_file/list_dir/code_semantic_search) so inspection does not starve work.
Env knob LABMATE_MAX_ITERATIONS (default = max_steps). Exhaustion summary is
now 'budget exhausted'.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: BDD feature + step defs

**Files:**
- Create: `tests/services/orchestrator/features/iteration_budget.feature`
- Create: `tests/services/orchestrator/test_iteration_budget_bdd.py`

**Interfaces:**
- Consumes: `AsyncOrchestrator.react_execute` (now budget-driven, from Task 4); `_msg_with_tool_call` pattern (replicated locally so this file is self-contained).

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/iteration_budget.feature` with the FULL Gherkin from the "Behavior (BDD) — Gherkin" section above (copy it verbatim, including the `@mocked` tag and all four scenarios).

- [ ] **Step 2: Write the step defs (failing — file does not exist yet)**

```python
# tests/services/orchestrator/test_iteration_budget_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator

scenarios("features/iteration_budget.feature")


# ── helpers ────────────────────────────────────────────────────────────────

def _tool_call_msg(name: str, arguments: dict):
    """A litellm-style assistant message that calls a single tool."""
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


@pytest.fixture
def ctx():
    """Mutable scenario context: orchestrator, scripted responses, result."""
    return {"cap": 6, "responses": [], "result": None, "mock": None}


# ── Background ───────────────────────────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and no mcp")
def _orch(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    # A stub MCP so run_bash returns output without a real bridge.
    mcp = AsyncMock()
    mcp_result = MagicMock()
    mcp_result.content = [MagicMock(text="output")]
    mcp_result.isError = False
    mcp.call_tool.return_value = mcp_result
    orch.mcp = mcp
    ctx["orch"] = orch


# ── Given steps ──────────────────────────────────────────────────────────────

@given(parsers.parse("the iteration budget cap is {cap:d}"))
def _set_cap(ctx, cap):
    ctx["cap"] = cap
    ctx["orch"].max_steps = cap


@given(parsers.parse('the model calls finish on its first turn with summary "{summary}"'))
def _finish_first(ctx, summary):
    ctx["responses"] = [_tool_call_msg("finish", {"summary": summary})]


@given(parsers.parse('every model turn calls run_bash with command "{command}"'))
def _always_bash(ctx, command):
    # Enough scripted responses to cover cap + grace without StopIteration.
    ctx["responses"] = [
        _tool_call_msg("run_bash", {"command": command}) for _ in range(20)
    ]


@given(parsers.parse('the model calls run_bash with command "{command}" on turn {turn:d}'))
def _bash_on_turn(ctx, command, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("run_bash", {"command": command})


@given(parsers.parse('the model calls list_dir with path "{path}" on turn {turn:d}'))
def _list_dir_on_turn(ctx, path, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("list_dir", {"path": path})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_on_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        # Filler that should never actually be consumed in a well-formed scenario.
        ctx["responses"].append(_tool_call_msg("run_bash", {"command": "echo filler"}))


# ── When step ────────────────────────────────────────────────────────────────

@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run(ctx, goal):
    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=ctx["responses"]) as mock:
        import asyncio
        ctx["result"] = asyncio.get_event_loop().run_until_complete(
            ctx["orch"].react_execute(goal)
        )
        ctx["mock"] = mock


# ── Then steps ───────────────────────────────────────────────────────────────

@then("the result ok is True")
def _ok_true(ctx):
    assert ctx["result"]["ok"] is True


@then("the result ok is False")
def _ok_false(ctx):
    assert ctx["result"]["ok"] is False


@then(parsers.parse('the result summary contains "{needle}"'))
def _summary_contains(ctx, needle):
    assert needle in ctx["result"]["summary"]


@then(parsers.parse("the budget used count is {n:d}"))
def _used_is(ctx, n):
    # One model call per consumed unit (no refunds in the finish-first scenario).
    assert ctx["mock"].await_count == n


@then(parsers.parse("the model was called exactly {n:d} times"))
def _called_n(ctx, n):
    assert ctx["mock"].await_count == n
```

**Note on `_run`:** if the foundation plan configured `pytest-asyncio` in `auto` mode, an `async def` step is not reliably awaited by pytest-bdd, so the step drives the coroutine explicitly via `run_until_complete`. If the foundation's conftest provides an event loop fixture/helper for BDD, prefer it; the explicit drive above is self-contained and dependency-free.

- [ ] **Step 3: Run the BDD suite to verify it fails first, then passes**

Run: `pytest tests/services/orchestrator/test_iteration_budget_bdd.py -v`
Expected first run (before Task 4 merged): FAIL. After Task 4 is merged: PASS (4 scenarios). Since this plan executes Task 4 before Task 5, expect: PASS (4 scenarios — the four `Scenario:` blocks).

If you see `pytest_bdd` collection errors about a missing feature file, confirm the `scenarios("features/iteration_budget.feature")` path is relative to this test file's directory (`tests/services/orchestrator/`).

- [ ] **Step 4: Commit**

```bash
git add tests/services/orchestrator/features/iteration_budget.feature tests/services/orchestrator/test_iteration_budget_bdd.py
git commit -m "test(orchestrator): BDD coverage for iteration budget grace + refund

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Full regression sweep

**Files:** none (verification only).

- [ ] **Step 1: Run the whole orchestrator test directory**

Run: `pytest tests/services/orchestrator/ -v 2>&1 | tail -40`
Expected: PASS — the new budget/grace/refund suites green AND every pre-existing orchestrator test still green (the default cap == `max_steps` keeps normal-finish behavior identical).

- [ ] **Step 2: Run the targeted budget + wire-in subset**

Run:
```bash
pytest tests/services/orchestrator/test_iteration_budget.py \
       tests/services/orchestrator/test_iteration_budget_bdd.py \
       tests/services/orchestrator/test_coding_orchestrator.py -v 2>&1 | tail -40
```
Expected: PASS (all three files).

- [ ] **Step 3: Commit (if any incidental fixups were needed)**

```bash
git add -A
git commit -m "test(orchestrator): green regression sweep for iteration budget

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage**

| Requirement (from prompt) | Task |
|---|---|
| `IterationBudget` is pure + fully unit-tested | Tasks 1–3 (`iteration_budget.py`, no async/I/O; core/refund/grace suites) |
| `consume` decrements; `remaining` never negative | Task 1 (`test_consume_decrements_remaining_and_returns_true`, `test_remaining_never_negative`) |
| grace fires exactly once after exhaustion | Task 3 (`test_grace_fires_once_then_never_again`) + Task 4 loop wiring |
| refund restores one unit | Task 2 (`test_refund_restores_one_unit`) |
| refund cannot exceed max_total | Task 2 (`test_refund_cannot_exceed_max_total`) |
| Focused pure module `services/orchestrator/iteration_budget.py`, no async in core | Task 1 (module uses only `threading`) |
| Wire into ReAct loop, replacing `range()` cap | Task 4 |
| ONE grace call on exhaustion, then stop with clear outcome | Task 4 (`while`/`consume`/`grace` scaffolding; `"budget exhausted"`) |
| Refund cheap/no-op read iterations (configurable set) | Task 1 `CHEAP_TOOLS` + Task 4 per-turn refund check |
| Env knob `LABMATE_MAX_ITERATIONS` default = current max_steps | Task 4 (`int(os.getenv("LABMATE_MAX_ITERATIONS", str(self.max_steps)))`) |
| Normal-finish behavior unchanged | Task 4 Step 7 regression; Task 5 scenario 1 |
| Regression-safe: default budget == max_steps | Task 4 default; Task 6 sweep |
| Additive only | No public signature changes; only the exhaustion summary string changed (called out below) |
| BDD `.feature` (`@mocked`), step defs, unit TDD tests | Tasks 1–3 (unit), Task 5 (feature + step defs) |

**2. Placeholder scan**

No `TBD`/`TODO`/"add error handling"/"similar to Task N". The one place that says "KEEP … UNCHANGED" is an explicit instruction to preserve existing, already-written code (lines ~487–608) rather than retype ~120 lines of event-emit/dispatch logic verbatim; the surrounding scaffolding is given in full so the engineer knows exactly what changes. Every code step shows real code.

**3. Type consistency**

- `IterationBudget(max_total: int)`, `consume() -> bool`, `refund() -> None`, `grace() -> bool`, `used`/`remaining`/`grace_used` properties — identical names used in Tasks 1–5.
- `CHEAP_TOOLS` is a `frozenset[str]` imported in both Task 1 tests and Task 4 loop; membership checks use the same tool-name strings exposed by `react_execute` (`read_file`, `list_dir`, `code_semantic_search`, `run_bash`, `write_file`, `call_skill_tool`, `finish`).
- `react_execute` return shape `{"ok": bool, "summary": str}` is preserved throughout.

**4. Existing test that MUST be updated (called out per the prompt)**

`tests/services/orchestrator/test_coding_orchestrator.py::TestReactExecute::test_react_execute_max_steps_exhausted` (lines 578–600) asserts `"max_steps" in result["summary"]`. The termination semantics intentionally change: exhaustion now returns `"budget exhausted"` AFTER a grace turn, so the model is called `cap + 1` times. Task 4 Step 6 rewrites this test to `test_react_execute_budget_exhausted` — new summary assertion and a third (`r3`) scripted response so the grace turn's `side_effect` does not raise `StopIteration`. This is the only existing test whose semantics change; all other existing tests pass unchanged because the default cap equals `max_steps` and finish-before-exhaustion paths are byte-for-byte identical.

**Two-call-budget edge note (verified against the loop):** with `cap=1`, the loop runs: turn 1 `consume()` → True (used=1); turn 2 `consume()` → False → `grace()` → True (runs the grace turn); turn 3 `consume()` → False → `grace()` → False → return `"budget exhausted"`. So a cap-1 goal gets exactly 2 model calls before stopping (matches `test_grace_call_that_finishes_succeeds` and `test_env_var_overrides_max_steps`).
