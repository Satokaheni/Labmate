# Async / Background Subagent Delegation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a capacity-gated `delegate_task` tool to the orchestrator's ReAct executor that spawns a bounded background sub-task, returns a `delegation_id` immediately, and re-enters that sub-task's result into the parent's `finish` aggregation when ready.

**Architecture:** A new focused module `services/orchestrator/delegation.py` exposes a `DelegationManager` — a pure-asyncio coordinator (no threads, no LLM dependency) that issues delegation ids, gates concurrency under a single `asyncio.Lock`, **rejects (does not queue)** dispatches once `running >= max_concurrent`, runs each delegated goal as an `asyncio.Task` over an injected async `runner`, and collects condensed results (capturing crashes as error results so the parent never hangs). The ReAct executor in `coding_orchestrator.py` gains a `delegate_task` tool that calls `DelegationManager.dispatch(...)` and a `finish`-time step that awaits and folds in any outstanding delegation results.

**Single-GPU reality (read this first):** Labmate runs ONE `llama-server` on one GPU, so inference is *serialized* — two delegated goals cannot think at the same time. This feature is therefore **NOT compute parallelism**. Its value is twofold and bounded: (1) **overlapping I/O-bound work** — a delegated goal can be waiting on a Redis-Streams skill dispatch / network skill (web-search, citation, dataset APIs) while the parent does other things; (2) **capacity-bounded fan-out** — a clean, accounted way to spin off at most `LABMATE_MAX_DELEGATIONS` (default `2`) sub-goals and re-collect them, with a hard rejection past the cap so the single GPU is never oversubscribed. The cap is intentionally small precisely *because* the GPU is the bottleneck. Do not frame or document this as true parallelism anywhere in code or comments.

**Tech Stack:** Python 3.11+ asyncio (`asyncio.Lock`, `asyncio.Task`, `asyncio.gather`, `asyncio.shield`), litellm (existing, untouched), pytest + pytest-asyncio, pytest-bdd (added by the foundation plan).

## Global Constraints

- **No `asyncio.run()` inside any async function** — the orchestrator already owns a running event loop (CLAUDE.md "What NOT to Do"). Use `await`, `asyncio.create_task`, `asyncio.gather`.
- **anyio / cancel-scope rule** — any async context manager must be entered AND exited in the same task. `DelegationManager` does NOT hand a live task/context out to a different task; it owns every `asyncio.Task` it creates for that task's full lifetime (CLAUDE.md Critical Rule 2).
- **No `print()` / `console.log` anywhere** — orchestrator code logs to stderr via `logging.getLogger(...)` (CLAUDE.md Critical Rule 1).
- **Additive only / regression-safe** — when the model never calls `delegate_task`, `react_execute` behavior is byte-for-byte identical to today. The `delegate_task` tool is only appended to the tool list; no existing branch changes semantics.
- **No threads** — capacity accounting and dispatch use asyncio primitives only (the orchestrator is asyncio; threads would need a different lock and break cancellation).
- **Token budget for LLM calls** is N/A here — `DelegationManager` makes NO LLM calls. The *runner* it's given does (it's `AsyncOrchestrator.react_execute`), and that already sets `thinking_budget_tokens` (CLAUDE.md Rule 6). Do not add a second LLM call in this module.
- **Env knob:** `LABMATE_MAX_DELEGATIONS` (default `2`) — max concurrently-running delegations. Read via `int(os.getenv("LABMATE_MAX_DELEGATIONS", "2"))`.
- **Result truncation:** condensed delegation summaries are capped at 2000 chars, matching the existing `Result.summary` convention in `coding_orchestrator.py`.
- **Test markers:** every new test is `@pytest.mark.mocked` (no GPU, no live server). The `.feature` file is tagged `@mocked`.

---

## Behavior (BDD) — Gherkin

This is the full contents of `tests/services/orchestrator/features/async_delegation.feature`. Copy verbatim in Task 6.

```gherkin
@mocked
Feature: Async / background subagent delegation
  The ReAct executor can spin off a bounded number of background sub-goals via a
  DelegationManager. Dispatch returns immediately with a delegation_id; results are
  collected later and folded into the parent's final answer. Because the single GPU
  serializes inference, the manager REJECTS (never queues) any dispatch over capacity,
  and a crashed sub-goal is captured as an error result so the parent never hangs.

  Background:
    Given a DelegationManager with max_concurrent 2 and a fake async runner

  Scenario: Dispatch returns a delegation id immediately
    When I dispatch a goal "summarize the readme"
    Then the dispatch status is "accepted"
    And the dispatch returns a non-empty delegation_id

  Scenario: A dispatched result can be collected
    Given the fake runner returns summary "the readme is about X" for "summarize the readme"
    When I dispatch a goal "summarize the readme"
    And I collect all delegation results
    Then exactly 1 result is collected
    And the collected result for that delegation has ok true
    And the collected result summary contains "the readme is about X"

  Scenario: Dispatching over capacity is rejected, not queued
    Given the fake runner blocks until released
    When I dispatch 2 goals that block
    And I dispatch one more goal "third goal"
    Then the third dispatch status is "rejected"
    And the third dispatch returns an empty delegation_id
    And the number of running delegations is 2

  Scenario: Capacity frees after a delegation completes
    Given the fake runner blocks until released
    When I dispatch 2 goals that block
    And I release the blocked runner
    And I collect all delegation results
    And I dispatch a goal "now there is room"
    Then the last dispatch status is "accepted"

  Scenario: A crashed delegation yields an error result and never hangs
    Given the fake runner raises RuntimeError "boom" for "explode"
    When I dispatch a goal "explode"
    And I collect all delegation results
    Then exactly 1 result is collected
    And the collected result for that delegation has ok false
    And the collected result summary contains "boom"
```

---

## File Map

| Path | Create/Modify | Responsibility |
|------|---------------|----------------|
| `services/orchestrator/delegation.py` | **Create** | `DelegationManager`, `Delegation` / `DelegationResult` dataclasses, `DelegationStatus` enum, `MAX_DELEGATIONS` env constant. Capacity gating, id issuance, status tracking, background-task lifecycle, result collection. No LLM, no Redis, no threads. |
| `services/orchestrator/coding_orchestrator.py` | **Modify** | (1) Construct a `DelegationManager` lazily inside `react_execute` bound to `self.react_execute` as the runner. (2) Append the `delegate_task` tool to the ReAct tool list. (3) Add the `delegate_task` dispatch branch in the tool-call switch. (4) At `finish`, await + fold outstanding delegation results into the summary. |
| `tests/services/orchestrator/test_delegation.py` | **Create** | Unit TDD tests for `DelegationManager` with a fake async runner fixture (no LLM). |
| `tests/services/orchestrator/features/async_delegation.feature` | **Create** | Gherkin feature (above). |
| `tests/services/orchestrator/test_async_delegation_bdd.py` | **Create** | pytest-bdd step definitions binding the feature to `DelegationManager` + the fake runner. |
| `tests/services/orchestrator/test_coding_orchestrator.py` | **Modify** | Add ReAct wire-in tests: `delegate_task` tool exists; the dispatch branch returns an id to the model; `finish` folds delegation results; regression — no `delegate_task` call ⇒ unchanged. |

---

## Public Interfaces (locked — later tasks rely on these exact names/types)

```python
# services/orchestrator/delegation.py

class DelegationStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"

@dataclass
class DelegationResult:
    delegation_id: str
    goal: str
    ok: bool
    summary: str          # condensed, <= 2000 chars

@dataclass
class Delegation:
    delegation_id: str
    goal: str
    status: str           # DelegationStatus value

class DelegationManager:
    def __init__(self, runner: Callable[[str], Awaitable[dict]], max_concurrent: int = MAX_DELEGATIONS) -> None: ...
    async def dispatch(self, goal: str) -> dict:          # -> {"status": str, "delegation_id": str}
    async def running_count(self) -> int                  # number of in-flight delegations
    async def collect(self) -> list[DelegationResult]     # await all in-flight + drain finished; returns condensed results
    def pending(self) -> bool                             # True if any delegation is still in-flight or uncollected
```

- `runner(goal: str) -> Awaitable[dict]`: the injected async callable. Must return `{"ok": bool, "summary": str}` (the exact shape `AsyncOrchestrator.react_execute` already returns). A raised exception is captured as `DelegationResult(ok=False, ...)`.
- `dispatch` issues an id and starts an `asyncio.Task` only when `running < max_concurrent`; otherwise returns `{"status": "rejected", "delegation_id": ""}` WITHOUT starting anything.
- `collect` awaits every in-flight task, gathers their `DelegationResult`s (including any already finished), and clears them so a second `collect()` returns `[]`.

---

### Task 1: `DelegationManager` skeleton — dataclasses, enum, env constant, constructor

**Files:**
- Create: `services/orchestrator/delegation.py`
- Test: `tests/services/orchestrator/test_delegation.py`

**Interfaces:**
- Produces: `DelegationStatus`, `DelegationResult`, `Delegation`, `MAX_DELEGATIONS`, `DelegationManager.__init__(runner, max_concurrent=MAX_DELEGATIONS)`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/orchestrator/test_delegation.py`:

```python
# tests/services/orchestrator/test_delegation.py
from __future__ import annotations

import asyncio
import pytest

from services.orchestrator.delegation import (
    DelegationManager,
    DelegationResult,
    DelegationStatus,
    MAX_DELEGATIONS,
)


@pytest.mark.mocked
def test_env_default_max_delegations_is_two():
    # Single-GPU reality: the default cap is intentionally small.
    assert MAX_DELEGATIONS == 2


@pytest.mark.mocked
def test_status_enum_values():
    assert DelegationStatus.ACCEPTED.value == "accepted"
    assert DelegationStatus.REJECTED.value == "rejected"


@pytest.mark.mocked
def test_manager_constructs_with_runner_and_default_cap():
    async def runner(goal: str) -> dict:
        return {"ok": True, "summary": goal}

    mgr = DelegationManager(runner=runner)
    assert mgr.max_concurrent == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_delegation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.delegation'`

- [ ] **Step 3: Write minimal implementation**

Create `services/orchestrator/delegation.py`:

```python
# services/orchestrator/delegation.py
"""
Capacity-gated background sub-goal delegation for the ReAct executor.

SINGLE-GPU REALITY: inference is serialized on one llama-server, so this is NOT
compute parallelism. Its value is (1) overlapping I/O-bound skill calls and
(2) a clean, capacity-bounded way to fan out a SMALL number of sub-goals and
re-collect their results. Over-capacity dispatches are REJECTED (never queued)
so the single GPU is never oversubscribed.

Pure asyncio: no threads, no LLM calls, no Redis. The LLM work happens inside
the injected `runner` (AsyncOrchestrator.react_execute), which already sets
thinking_budget_tokens.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

_log = logging.getLogger("delegation")

# Single-GPU default: keep the fan-out small — the GPU, not concurrency, is the bottleneck.
MAX_DELEGATIONS = int(os.getenv("LABMATE_MAX_DELEGATIONS", "2"))


class DelegationStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass
class DelegationResult:
    """Condensed handback from a delegated sub-goal (never the raw transcript)."""
    delegation_id: str
    goal: str
    ok: bool
    summary: str  # <= 2000 chars


@dataclass
class Delegation:
    delegation_id: str
    goal: str
    status: str  # DelegationStatus value


class DelegationManager:
    """Issues ids, gates concurrency under a single lock, runs sub-goals as
    asyncio.Tasks, and collects condensed results. Rejects over-capacity dispatch."""

    def __init__(
        self,
        runner: Callable[[str], Awaitable[dict]],
        max_concurrent: int = MAX_DELEGATIONS,
    ) -> None:
        self._runner = runner
        self.max_concurrent = max(1, int(max_concurrent))
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}
        self._results: list[DelegationResult] = []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_delegation.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/delegation.py tests/services/orchestrator/test_delegation.py
git commit -m "feat(delegation): DelegationManager skeleton with capacity-gated dataclasses"
```

---

### Task 2: `dispatch` — id issuance, capacity gate (reject over cap), background task start

**Files:**
- Modify: `services/orchestrator/delegation.py`
- Test: `tests/services/orchestrator/test_delegation.py`

**Interfaces:**
- Consumes: `DelegationManager.__init__`, `DelegationStatus`.
- Produces: `async DelegationManager.dispatch(goal) -> {"status": str, "delegation_id": str}`, `async DelegationManager.running_count() -> int`.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_delegation.py`:

```python
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_dispatch_returns_accepted_with_id():
    async def runner(goal: str) -> dict:
        return {"ok": True, "summary": goal}

    mgr = DelegationManager(runner=runner, max_concurrent=2)
    out = await mgr.dispatch("summarize the readme")
    assert out["status"] == "accepted"
    assert out["delegation_id"]  # non-empty
    # Let the task finish so the event loop has no dangling task at teardown.
    await mgr.collect()


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_dispatch_over_capacity_is_rejected_not_queued():
    release = asyncio.Event()

    async def blocking_runner(goal: str) -> dict:
        await release.wait()
        return {"ok": True, "summary": goal}

    mgr = DelegationManager(runner=blocking_runner, max_concurrent=2)
    a = await mgr.dispatch("g1")
    b = await mgr.dispatch("g2")
    # N+1th dispatch must be rejected deterministically (single-GPU: never queue).
    c = await mgr.dispatch("g3")

    assert a["status"] == "accepted"
    assert b["status"] == "accepted"
    assert c["status"] == "rejected"
    assert c["delegation_id"] == ""
    assert await mgr.running_count() == 2

    release.set()
    await mgr.collect()


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_capacity_frees_after_collect():
    release = asyncio.Event()

    async def blocking_runner(goal: str) -> dict:
        await release.wait()
        return {"ok": True, "summary": goal}

    mgr = DelegationManager(runner=blocking_runner, max_concurrent=2)
    await mgr.dispatch("g1")
    await mgr.dispatch("g2")
    assert (await mgr.dispatch("g3"))["status"] == "rejected"

    release.set()
    await mgr.collect()  # drains the two blocked delegations

    assert await mgr.running_count() == 0
    assert (await mgr.dispatch("g4"))["status"] == "accepted"
    await mgr.collect()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_delegation.py -k dispatch -v`
Expected: FAIL — `AttributeError: 'DelegationManager' object has no attribute 'dispatch'`

- [ ] **Step 3: Write minimal implementation**

Add these methods to `DelegationManager` in `services/orchestrator/delegation.py` (below `__init__`):

```python
    async def running_count(self) -> int:
        async with self._lock:
            return sum(1 for t in self._tasks.values() if not t.done())

    async def dispatch(self, goal: str) -> dict:
        """Start a background sub-goal if under capacity; otherwise REJECT.

        Single-GPU policy: we never queue. Returning 'rejected' lets the model
        decide whether to do the work inline instead of oversubscribing the GPU.
        """
        async with self._lock:
            running = sum(1 for t in self._tasks.values() if not t.done())
            if running >= self.max_concurrent:
                _log.info("delegation rejected (at capacity %d): %s", self.max_concurrent, goal[:80])
                return {"status": DelegationStatus.REJECTED.value, "delegation_id": ""}
            delegation_id = "dlg-" + uuid.uuid4().hex[:12]
            task = asyncio.create_task(self._run(delegation_id, goal))
            self._tasks[delegation_id] = task
            _log.info("delegation accepted %s: %s", delegation_id, goal[:80])
            return {"status": DelegationStatus.ACCEPTED.value, "delegation_id": delegation_id}

    async def _run(self, delegation_id: str, goal: str) -> DelegationResult:
        """Owning coroutine for one delegation. Captures crashes as error results
        so a failing sub-goal can never hang the parent. NEVER swallows
        CancelledError (anyio / structured-concurrency correctness)."""
        try:
            ret = await self._runner(goal)
            summary = str((ret or {}).get("summary", ""))[:2000]
            ok = bool((ret or {}).get("ok", False))
            return DelegationResult(delegation_id=delegation_id, goal=goal, ok=ok, summary=summary)
        except asyncio.CancelledError:
            raise  # never swallow — keeps cancellation correct
        except Exception as exc:  # noqa: BLE001 — crash is captured, not propagated
            _log.warning("delegation %s crashed: %s", delegation_id, exc)
            return DelegationResult(
                delegation_id=delegation_id,
                goal=goal,
                ok=False,
                summary=f"delegation error: {str(exc)[:200]}",
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_delegation.py -k dispatch -v`
Expected: FAIL — `test_capacity_frees_after_collect` and the `await mgr.collect()` calls fail with `AttributeError: ... has no attribute 'collect'`. The `dispatch` / `running_count` assertions themselves pass. This is expected; `collect` lands in Task 3.

Run only the non-collect dispatch checks to confirm the gate works now:
Run: `python -m pytest "tests/services/orchestrator/test_delegation.py::test_dispatch_over_capacity_is_rejected_not_queued" -v` — this still references `collect` at the end, so instead verify the gate in isolation:

```bash
python -m pytest tests/services/orchestrator/test_delegation.py -k "dispatch and not frees and not collect" -v 2>&1 | tail -5
```
Expected: the rejection/running_count assertions are reached and pass before any `collect` call. If a test errors only on the trailing `await mgr.collect()`, that is the Task 3 gap.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/delegation.py tests/services/orchestrator/test_delegation.py
git commit -m "feat(delegation): capacity-gated dispatch rejects over cap, never queues"
```

---

### Task 3: `collect` and `pending` — await in-flight tasks, gather condensed results, drain

**Files:**
- Modify: `services/orchestrator/delegation.py`
- Test: `tests/services/orchestrator/test_delegation.py`

**Interfaces:**
- Consumes: `DelegationManager.dispatch`, `_run`, `DelegationResult`.
- Produces: `async DelegationManager.collect() -> list[DelegationResult]`, `DelegationManager.pending() -> bool`.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_delegation.py`:

```python
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_collect_returns_condensed_result():
    async def runner(goal: str) -> dict:
        return {"ok": True, "summary": f"did: {goal}"}

    mgr = DelegationManager(runner=runner, max_concurrent=2)
    out = await mgr.dispatch("summarize the readme")
    results = await mgr.collect()

    assert len(results) == 1
    r = results[0]
    assert r.delegation_id == out["delegation_id"]
    assert r.ok is True
    assert "did: summarize the readme" in r.summary


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_collect_is_idempotent_and_drains():
    async def runner(goal: str) -> dict:
        return {"ok": True, "summary": goal}

    mgr = DelegationManager(runner=runner, max_concurrent=2)
    await mgr.dispatch("g1")
    first = await mgr.collect()
    second = await mgr.collect()  # already drained

    assert len(first) == 1
    assert second == []
    assert mgr.pending() is False


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_pending_true_while_in_flight():
    release = asyncio.Event()

    async def blocking_runner(goal: str) -> dict:
        await release.wait()
        return {"ok": True, "summary": goal}

    mgr = DelegationManager(runner=blocking_runner, max_concurrent=2)
    await mgr.dispatch("g1")
    assert mgr.pending() is True
    release.set()
    await mgr.collect()
    assert mgr.pending() is False


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_crashed_delegation_yields_error_result_not_hang():
    async def crashing_runner(goal: str) -> dict:
        if goal == "explode":
            raise RuntimeError("boom")
        return {"ok": True, "summary": goal}

    mgr = DelegationManager(runner=crashing_runner, max_concurrent=2)
    out = await mgr.dispatch("explode")
    # Must complete quickly — a crash is captured, never a hang.
    results = await asyncio.wait_for(mgr.collect(), timeout=2.0)

    assert len(results) == 1
    r = results[0]
    assert r.delegation_id == out["delegation_id"]
    assert r.ok is False
    assert "boom" in r.summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_delegation.py -k "collect or pending or crashed" -v`
Expected: FAIL — `AttributeError: 'DelegationManager' object has no attribute 'collect'`

- [ ] **Step 3: Write minimal implementation**

Add to `DelegationManager` in `services/orchestrator/delegation.py`:

```python
    def pending(self) -> bool:
        """True if any delegation is still in-flight or has an uncollected result."""
        if self._results:
            return True
        return any(not t.done() for t in self._tasks.values())

    async def collect(self) -> list[DelegationResult]:
        """Await every in-flight delegation, gather all condensed results
        (including any that already finished), then DRAIN — a subsequent
        collect() returns []. asyncio.shield is NOT used: collect() is the
        owner of these tasks and awaits them to completion in this same task,
        satisfying the cancel-scope rule.
        """
        async with self._lock:
            tasks = list(self._tasks.items())
            self._tasks = {}
        for delegation_id, task in tasks:
            try:
                result = await task
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — defensive; _run already captures
                result = DelegationResult(
                    delegation_id=delegation_id,
                    goal="",
                    ok=False,
                    summary=f"delegation error: {str(exc)[:200]}",
                )
            self._results.append(result)
        drained = self._results
        self._results = []
        return drained
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_delegation.py -v`
Expected: PASS (all unit tests, including the Task 2 `collect`-trailing ones, now pass)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/delegation.py tests/services/orchestrator/test_delegation.py
git commit -m "feat(delegation): collect awaits and drains, crashes captured as error results"
```

---

### Task 4: ReAct wire-in — append the `delegate_task` tool to the tool list

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (tool list inside `react_execute`, currently the `tools.extend([...])` block ending around line 383)
- Test: `tests/services/orchestrator/test_coding_orchestrator.py`

**Interfaces:**
- Consumes: nothing new (schema is static JSON).
- Produces: a `delegate_task` function tool in the ReAct tool list with schema `{goal: string (required)}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_coding_orchestrator.py`:

```python
@pytest.mark.mocked
def test_react_tool_list_includes_delegate_task():
    """delegate_task must be advertised to the model in react_execute's tool list."""
    import inspect
    from services.orchestrator import coding_orchestrator
    src = inspect.getsource(coding_orchestrator.AsyncOrchestrator.react_execute)
    assert "delegate_task" in src
    # Schema advertises a single required 'goal' string.
    assert '"goal"' in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::test_react_tool_list_includes_delegate_task" -v`
Expected: FAIL — `assert 'delegate_task' in src`

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/coding_orchestrator.py`, inside `react_execute`, in the `tools.extend([...])` block, add this tool entry immediately BEFORE the `finish` tool entry (keep `finish` last):

```python
            {
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "description": (
                        "Spin off a focused sub-goal to run in the BACKGROUND and return "
                        "immediately with a delegation_id. Use ONLY for an independent, "
                        "self-contained piece of work whose result you will need at the end "
                        "(its result is folded into your final answer automatically). "
                        "Capacity is small and bounded: if the system is already at its "
                        "delegation limit the dispatch is REJECTED — when that happens, just "
                        "do the work inline yourself instead of delegating."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "goal": {
                                "type": "string",
                                "description": "Self-contained description of the sub-goal to delegate",
                            },
                        },
                        "required": ["goal"],
                    },
                },
            },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::test_react_tool_list_includes_delegate_task" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): advertise delegate_task tool in ReAct tool list"
```

---

### Task 5: ReAct wire-in — `delegate_task` dispatch branch + `finish`-time result folding

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (`react_execute`: lazy manager init near the top of the method; a new `elif name == "delegate_task"` branch in the tool switch; the `finish` branch to fold results)
- Test: `tests/services/orchestrator/test_coding_orchestrator.py`

**Interfaces:**
- Consumes: `DelegationManager`, `DelegationResult` from `services.orchestrator.delegation`.
- Produces: a per-`react_execute`-call `DelegationManager` bound to `self.react_execute`; `delegate_task` tool returns `{"status","delegation_id"}` JSON to the model; `finish` summary gains a `\n\nDelegated results:` block when delegations were collected.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_coding_orchestrator.py`:

```python
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_react_delegate_task_returns_id_then_finish_folds_result():
    """delegate_task dispatches a background sub-goal; its result is folded into finish."""
    orch = AsyncOrchestrator(skill_router=None, mcp=None, max_steps=4)

    # The delegated sub-goal runs through react_execute itself. To keep the test
    # deterministic, patch the manager's runner indirectly by patching react_execute
    # for the *delegated* goal only via a sentinel goal string is brittle; instead
    # inject a fake runner by patching DelegationManager at construction.
    from services.orchestrator import delegation as delegation_mod

    async def fake_runner(goal: str) -> dict:
        return {"ok": True, "summary": f"SUBRESULT[{goal}]"}

    orig_init = delegation_mod.DelegationManager.__init__

    def patched_init(self, runner, max_concurrent=delegation_mod.MAX_DELEGATIONS):
        orig_init(self, fake_runner, max_concurrent)

    # Turn 1: model calls delegate_task. Turn 2: model calls finish.
    delegate_msg = _msg_with_tool_call("delegate_task", '{"goal": "do the sub thing"}')
    finish_msg = _msg_with_tool_call("finish", '{"summary": "main done"}')
    resp1 = MagicMock(choices=[MagicMock(message=delegate_msg)])
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])

    with patch.object(delegation_mod.DelegationManager, "__init__", patched_init), \
         patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=[resp1, resp2]):
        out = await orch.react_execute("main goal")

    assert out["ok"] is True
    assert "main done" in out["summary"]
    assert "SUBRESULT[do the sub thing]" in out["summary"]


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_react_delegate_task_branch_returns_delegation_id_to_model():
    """The tool response handed back to the model carries status + delegation_id."""
    orch = AsyncOrchestrator(skill_router=None, mcp=None, max_steps=4)
    from services.orchestrator import delegation as delegation_mod

    async def fake_runner(goal: str) -> dict:
        return {"ok": True, "summary": "sub"}

    orig_init = delegation_mod.DelegationManager.__init__

    def patched_init(self, runner, max_concurrent=delegation_mod.MAX_DELEGATIONS):
        orig_init(self, fake_runner, max_concurrent)

    captured_tool_msgs = []

    real_acompletion_side = None

    delegate_msg = _msg_with_tool_call("delegate_task", '{"goal": "sub"}')
    finish_msg = _msg_with_tool_call("finish", '{"summary": "done"}')
    resp1 = MagicMock(choices=[MagicMock(message=delegate_msg)])
    resp2 = MagicMock(choices=[MagicMock(message=finish_msg)])

    async def fake_acompletion(*a, **k):
        # Capture the tool-role messages appended between turns.
        for m in k.get("messages", []):
            if m.get("role") == "tool":
                captured_tool_msgs.append(m["content"])
        return resp2 if any("delegation_id" in c for c in captured_tool_msgs) else resp1

    with patch.object(delegation_mod.DelegationManager, "__init__", patched_init), \
         patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=fake_acompletion):
        out = await orch.react_execute("main")

    assert out["ok"] is True
    assert any('"delegation_id"' in c and '"status"' in c for c in captured_tool_msgs)


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_react_no_delegate_call_is_unchanged_regression():
    """Regression: when delegate_task is never called, finish summary is unchanged
    (no 'Delegated results' block)."""
    orch = AsyncOrchestrator(skill_router=None, mcp=None, max_steps=2)
    finish_msg = _msg_with_tool_call("finish", '{"summary": "plain answer"}')
    resp = MagicMock(choices=[MagicMock(message=finish_msg)])

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=[resp]):
        out = await orch.react_execute("simple")

    assert out["ok"] is True
    assert out["summary"] == "plain answer"
    assert "Delegated results" not in out["summary"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::test_react_delegate_task_returns_id_then_finish_folds_result" "tests/services/orchestrator/test_coding_orchestrator.py::test_react_delegate_task_branch_returns_delegation_id_to_model" -v`
Expected: FAIL — the `delegate_task` tool produces `{"error": "unknown tool: delegate_task"}` (current `else` branch), so the delegated result is never folded; `SUBRESULT[...]` / `delegation_id` assertions fail.
(The regression test `test_react_no_delegate_call_is_unchanged_regression` should already PASS — it documents current behavior we must preserve.)

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/coding_orchestrator.py`, `react_execute`:

**(a)** At the top of the method, after the import and the skill-activation-reset block (right before the `# ── Skill-first deterministic routing ──` section), add a lazy `DelegationManager` bound to this orchestrator's own ReAct executor:

```python
        # Per-call delegation manager. Bound to THIS orchestrator's react_execute so a
        # delegated sub-goal runs the same skill-aware ReAct loop. Single-GPU: the cap
        # (LABMATE_MAX_DELEGATIONS, default 2) keeps fan-out small — inference is serialized.
        from .delegation import DelegationManager
        _delegations = DelegationManager(runner=self.react_execute)
```

**(b)** Add the dispatch branch in the tool-call `if/elif` switch. Place it BEFORE the final `else: content = json.dumps({"error": f"unknown tool: {name}"})`:

```python
                    elif name == "delegate_task":
                        # Capacity-gated background dispatch. Returns immediately with an
                        # id (or 'rejected' at capacity). The result is folded in at finish.
                        out = await _delegations.dispatch(str(args.get("goal", "")))
                        content = json.dumps(out)
```

**(c)** Replace the `finish` branch so it folds outstanding delegation results into the summary. The current branch is:

```python
                    if name == "finish":
                        return {
                            "ok": True,
                            "summary": str(args.get("summary", ""))[:2000],
                        }
```

Replace it with:

```python
                    if name == "finish":
                        summary = str(args.get("summary", ""))
                        if _delegations.pending():
                            collected = await _delegations.collect()
                            if collected:
                                folded = "\n".join(
                                    f"- [{r.delegation_id}] {'ok' if r.ok else 'FAILED'}: {r.summary}"
                                    for r in collected
                                )
                                summary = f"{summary}\n\nDelegated results:\n{folded}"
                        return {"ok": True, "summary": summary[:2000]}
```

**(d)** Also fold pending delegations when the loop exits other ways, so a delegated task is never orphaned. Replace the `# Max steps reached` return:

```python
            # Max steps reached
            return {"ok": False, "summary": "max_steps reached"}
```

with:

```python
            # Max steps reached — still collect any outstanding delegations so they
            # don't hang and their (possibly useful) results are surfaced.
            if _delegations.pending():
                await _delegations.collect()
            return {"ok": False, "summary": "max_steps reached"}
```

And in the no-tool-calls early return (the `if not tool_calls:` block that returns `msg.content`), add a collect before returning. The current block:

```python
                if not tool_calls:
                    # No tool calls — return the content directly
                    return {
                        "ok": True,
                        "summary": (msg.content or "")[:2000],
                    }
```

becomes:

```python
                if not tool_calls:
                    # No tool calls — return the content directly. Collect any
                    # outstanding delegations first so background tasks never hang.
                    if _delegations.pending():
                        await _delegations.collect()
                    return {
                        "ok": True,
                        "summary": (msg.content or "")[:2000],
                    }
```

(The top-level `except Exception` handler already returns; a crashed/cancelled delegation is captured inside `_run`, so no extra collect is needed there. The manager's tasks are GC-safe because `_run` swallows non-cancel exceptions into results; on an outer exception the unawaited tasks are cancelled at loop teardown, and `_run` re-raises `CancelledError` correctly.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest "tests/services/orchestrator/test_coding_orchestrator.py::test_react_delegate_task_returns_id_then_finish_folds_result" "tests/services/orchestrator/test_coding_orchestrator.py::test_react_delegate_task_branch_returns_delegation_id_to_model" "tests/services/orchestrator/test_coding_orchestrator.py::test_react_no_delegate_call_is_unchanged_regression" -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full coding-orchestrator suite (regression gate)**

Run: `python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -v 2>&1 | tail -15`
Expected: all previously-passing tests still pass; the 4 new ones pass. No failures.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): wire delegate_task dispatch + finish-time result folding"
```

---

### Task 6: BDD feature file + pytest-bdd step definitions

**Files:**
- Create: `tests/services/orchestrator/features/async_delegation.feature`
- Create: `tests/services/orchestrator/test_async_delegation_bdd.py`

**Interfaces:**
- Consumes: `DelegationManager`, `DelegationStatus` from `services.orchestrator.delegation`; the `@mocked` marker registered in `tests/services/orchestrator/conftest.py`.
- Produces: executable BDD coverage of the 5 scenarios.

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/async_delegation.feature` with the **exact** Gherkin from the "## Behavior (BDD) — Gherkin" section above (copy it verbatim, including the `@mocked` tag).

- [ ] **Step 2: Write the step definitions (the failing test)**

Create `tests/services/orchestrator/test_async_delegation_bdd.py`:

```python
# tests/services/orchestrator/test_async_delegation_bdd.py
from __future__ import annotations

import asyncio

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.delegation import DelegationManager

scenarios("features/async_delegation.feature")


class _Ctx:
    """Mutable scenario context shared across steps."""
    def __init__(self) -> None:
        self.mgr: DelegationManager | None = None
        self.release: asyncio.Event | None = None
        self.summaries: dict[str, str] = {}     # goal -> summary the runner should return
        self.crashes: dict[str, str] = {}       # goal -> exception message
        self.dispatches: list[dict] = []         # ordered dispatch outcomes
        self.collected: list = []                # last collect() result


@pytest.fixture
def ctx() -> _Ctx:
    return _Ctx()


def _run(coro):
    """Drive an async coroutine to completion from a sync pytest-bdd step."""
    return asyncio.get_event_loop().run_until_complete(coro)


@given(parsers.parse("a DelegationManager with max_concurrent {n:d} and a fake async runner"))
def _make_manager(ctx: _Ctx, n: int):
    async def runner(goal: str) -> dict:
        if goal in ctx.crashes:
            raise RuntimeError(ctx.crashes[goal])
        if ctx.release is not None:
            await ctx.release.wait()
        return {"ok": True, "summary": ctx.summaries.get(goal, goal)}

    ctx.mgr = DelegationManager(runner=runner, max_concurrent=n)


@given(parsers.parse('the fake runner returns summary "{summary}" for "{goal}"'))
def _runner_returns(ctx: _Ctx, summary: str, goal: str):
    ctx.summaries[goal] = summary


@given("the fake runner blocks until released")
def _runner_blocks(ctx: _Ctx):
    ctx.release = asyncio.get_event_loop().run_until_complete(_make_event())


async def _make_event() -> asyncio.Event:
    return asyncio.Event()


@given(parsers.parse('the fake runner raises RuntimeError "{msg}" for "{goal}"'))
def _runner_raises(ctx: _Ctx, msg: str, goal: str):
    ctx.crashes[goal] = msg


@when(parsers.parse('I dispatch a goal "{goal}"'))
def _dispatch_goal(ctx: _Ctx, goal: str):
    ctx.dispatches.append(_run(ctx.mgr.dispatch(goal)))


@when("I dispatch 2 goals that block")
def _dispatch_two_blocking(ctx: _Ctx):
    ctx.dispatches.append(_run(ctx.mgr.dispatch("block-1")))
    ctx.dispatches.append(_run(ctx.mgr.dispatch("block-2")))


@when(parsers.parse('I dispatch one more goal "{goal}"'))
def _dispatch_one_more(ctx: _Ctx, goal: str):
    ctx.dispatches.append(_run(ctx.mgr.dispatch(goal)))


@when("I release the blocked runner")
def _release(ctx: _Ctx):
    ctx.release.set()


@when("I collect all delegation results")
def _collect(ctx: _Ctx):
    ctx.collected = _run(ctx.mgr.collect())


@then(parsers.parse('the dispatch status is "{status}"'))
def _check_status(ctx: _Ctx, status: str):
    assert ctx.dispatches[-1]["status"] == status


@then("the dispatch returns a non-empty delegation_id")
def _check_id_nonempty(ctx: _Ctx):
    assert ctx.dispatches[-1]["delegation_id"]


@then(parsers.parse('the third dispatch status is "{status}"'))
def _check_third_status(ctx: _Ctx, status: str):
    assert ctx.dispatches[2]["status"] == status


@then("the third dispatch returns an empty delegation_id")
def _check_third_id_empty(ctx: _Ctx):
    assert ctx.dispatches[2]["delegation_id"] == ""


@then(parsers.parse("the number of running delegations is {n:d}"))
def _check_running(ctx: _Ctx, n: int):
    assert _run(ctx.mgr.running_count()) == n


@then(parsers.parse('the last dispatch status is "{status}"'))
def _check_last_status(ctx: _Ctx, status: str):
    assert ctx.dispatches[-1]["status"] == status


@then(parsers.parse("exactly {n:d} result is collected"))
@then(parsers.parse("exactly {n:d} results are collected"))
def _check_collected_count(ctx: _Ctx, n: int):
    assert len(ctx.collected) == n


@then("the collected result for that delegation has ok true")
def _check_ok_true(ctx: _Ctx):
    assert ctx.collected[0].ok is True


@then("the collected result for that delegation has ok false")
def _check_ok_false(ctx: _Ctx):
    assert ctx.collected[0].ok is False


@then(parsers.parse('the collected result summary contains "{needle}"'))
def _check_summary_contains(ctx: _Ctx, needle: str):
    assert any(needle in r.summary for r in ctx.collected)
```

Note: `_run` uses the session event loop so blocking-runner `asyncio.Event`s created in one step are awaitable in another. If the foundation plan's `conftest.py` already provides an `event_loop` fixture or an `anyio`/`asyncio` BDD harness, prefer that; this `_run` helper is the self-contained fallback that needs no extra fixtures.

- [ ] **Step 3: Run to verify it fails (then passes)**

Run: `python -m pytest tests/services/orchestrator/test_async_delegation_bdd.py -v`
Expected on first run BEFORE the module exists: collection error / `ModuleNotFoundError`. After creating both files with `DelegationManager` already implemented (Tasks 1-3): PASS — 5 scenarios pass.

If pytest-bdd is not yet installed (foundation plan not landed), this is the failure:
Run: `python -c "import pytest_bdd"`
Expected if missing: `ModuleNotFoundError: No module named 'pytest_bdd'` — install with `pip install pytest-bdd` (the foundation plan pins it; do not pin a different version here).

- [ ] **Step 4: Commit**

```bash
git add tests/services/orchestrator/features/async_delegation.feature tests/services/orchestrator/test_async_delegation_bdd.py
git commit -m "test(delegation): BDD feature + step defs for capacity-gated delegation"
```

---

### Task 7: Full-suite regression gate

**Files:** none (verification only)

- [ ] **Step 1: Run the orchestrator test suite**

Run: `python -m pytest tests/services/orchestrator/ -v 2>&1 | tail -25`
Expected: all tests pass, including the existing 342 orchestrator tests, the new `test_delegation.py` (10 tests), the 4 new `test_coding_orchestrator.py` tests, and the 5 BDD scenarios. Zero failures, zero errors.

- [ ] **Step 2: Confirm no event-loop warnings / dangling tasks**

Run: `python -m pytest tests/services/orchestrator/test_delegation.py tests/services/orchestrator/test_async_delegation_bdd.py -W error::RuntimeWarning -v 2>&1 | tail -15`
Expected: PASS with no `RuntimeWarning: coroutine ... was never awaited` and no "Task was destroyed but it is pending" warnings — every dispatched task is collected or cancelled.

- [ ] **Step 3: Commit (if any test-only tweaks were needed)**

```bash
git add -A
git commit -m "test(delegation): full orchestrator suite green with async delegation"
```

---

## Self-Review

**1. Spec coverage**

| Spec requirement | Task |
|------------------|------|
| New module `services/orchestrator/delegation.py` with `DelegationManager` | Tasks 1-3 |
| `dispatch(goal) -> {status, delegation_id}` | Task 2 |
| REJECTS (not queues) at `running >= max_concurrent` | Task 2 (`test_dispatch_over_capacity_is_rejected_not_queued`) + BDD scenario 3 |
| Env `LABMATE_MAX_DELEGATIONS` default 2 | Task 1 (`MAX_DELEGATIONS`) |
| Await / collect results | Task 3 (`collect`) |
| Capacity accounting under a single lock | Tasks 1-3 (`self._lock` guards `running` count + task dict) |
| asyncio primitives, not threads | All tasks (no `threading` import anywhere) |
| Independently unit-testable with fake async runner, no LLM | Task 1-3 tests inject a plain `async def runner` |
| Deterministic capacity rejection tested (N+1 → "rejected") | Task 2 + BDD scenario 3 |
| Crashed delegation → error result, never hangs | Task 3 (`test_crashed_delegation_yields_error_result_not_hang` with `asyncio.wait_for` 2s) + BDD scenario 5 |
| `delegate_task` tool wired into ReAct loop | Tasks 4-5 |
| Result re-enters at `finish` aggregation | Task 5 (finish-fold) + `test_react_delegate_task_returns_id_then_finish_folds_result` |
| Additive / regression-safe (no call ⇒ identical) | Task 5 (`test_react_no_delegate_call_is_unchanged_regression`) |
| Full `.feature` with all 4 named behaviors | "Behavior (BDD)" section + Task 6 |
| Step defs in `test_async_delegation_bdd.py` | Task 6 |
| Unit TDD in `test_delegation.py` | Tasks 1-3 |
| Single-GPU framing (I/O overlap + bounded fan-out, NOT compute parallelism) | Architecture section + module docstring + tool description |

**2. Placeholder scan** — No `TBD`/`TODO`/"add error handling"/"similar to". Every code step shows full code. Every test step shows the assertion. Every run step shows the exact command and expected output.

**3. Type consistency** — `dispatch` returns `{"status": str, "delegation_id": str}` everywhere (Task 2 impl, Task 5 tool branch, BDD steps). `collect()` returns `list[DelegationResult]` and `DelegationResult` fields (`delegation_id`, `goal`, `ok`, `summary`) are used identically in Task 3 tests, Task 5 finish-fold, and Task 6 steps. `running_count()` is `async` and always `await`-ed. `pending()` is sync and never awaited. `DelegationStatus.ACCEPTED.value == "accepted"` / `REJECTED.value == "rejected"` match the string literals asserted in the feature file. The runner contract `{"ok": bool, "summary": str}` matches `AsyncOrchestrator.react_execute`'s actual return shape, so binding `runner=self.react_execute` in Task 5 is correct.

**Note for the implementer on asyncio correctness:** `DelegationManager` owns every `asyncio.Task` it creates and only awaits them inside `collect()` (same task that will be running the ReAct loop) — it never hands a live task/context to a different task, satisfying CLAUDE.md Rule 2. `_run` re-raises `CancelledError` and captures every other exception, so the cap-accounting lock is never left holding a crashed task as "running". There is no `asyncio.run()` anywhere — all entry points are `await`/`create_task`.
