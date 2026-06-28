# Wall-clock Deadline + No-progress Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two layered safety guards to the ReAct loop in `_run_react_loop` — a per-goal wall-clock deadline and a pure no-progress (idle) breaker — so a stalled or runaway loop hard-stops with a clear message instead of only relying on step counting.

**Architecture:** Mirror the openclaw mechanism (run/idle-timeout-breaker.ts + attempt.ts wall-clock). Add (1) a wall-clock deadline checked at the top of each turn inside `_run_react_loop`, using an injectable clock so tests are deterministic; and (2) a new PURE module `services/orchestrator/progress_breaker.py` — a small thread-safe dataclass-backed counter that increments on a turn with no completed progress, resets on real progress, and trips at a cap. Both guards are additive and OFF-safe (env-tunable; a normal short loop hits neither). They sit alongside the existing `IterationBudget` step counter — they do not replace it.

**Tech Stack:** Python 3.11, asyncio, `litellm` (HTTP seam), `pytest` + `pytest-asyncio`, `pytest-bdd` (BDD layer with `fake_model`/scripted `side_effect` lists), `respx` (HTTP mock, already wired in `tests/conftest.py`).

## Global Constraints

- **stdout is sacred:** never `print()` / `console.log()`; use `logging` to stderr only. (CLAUDE.md Rule 1)
- **Pure modules stay pure:** `progress_breaker.py` has no async, no I/O, no orchestrator imports — exactly like `iteration_budget.py`. Thread-safe via `threading.Lock()`.
- **Python file naming:** `snake_case.py`; classes `PascalCase`; functions `snake_case`. (CLAUDE.md File Naming)
- **Service URLs / config from env, never hardcoded.** New env knobs: `LABMATE_GOAL_DEADLINE_S` (default `600`; `0` disables), `LABMATE_NOPROGRESS_LIMIT` (default `5`; `0` disables).
- **Tests:** live in `tests/` mirroring `services/`; `@pytest.mark.asyncio` on async tests; assert structure not literal LLM text; pure-module tests use `@pytest.mark.mocked`.
- **BDD contract (ALREADY EXISTS — do NOT recreate):** pytest-bdd; `fake_model` + `run_async` in `tests/conftest.py`; feature → `tests/services/orchestrator/features/<slug>.feature` tagged `@mocked`; step defs → `tests/services/orchestrator/test_<slug>_bdd.py`; `*_bdd.py` patch the model call with `side_effect` lists. Follow that pattern verbatim.
- **Additive + regression-safe:** no signature removals; existing `IterationBudget`, `LoopDetector`, `PromptAssembler`, failover behavior unchanged. A normal productive short loop must hit neither new guard. Suite (`tests/services/orchestrator/` + memory) must stay green.

---

## Structural Anchors (re-verify before editing — a concurrent workflow is editing `coding_orchestrator.py`)

`services/orchestrator/coding_orchestrator.py` `_run_react_loop(self, goal, max_steps)` is the only edit site. **Anchor on STRUCTURE, not line numbers.** As of this writing the relevant structure is:

1. `cap = int(os.getenv("LABMATE_MAX_ITERATIONS", str(self.max_steps)))` then `budget = IterationBudget(max_total=cap)`.
2. `try:` then **`while True:`** — the loop body.
3. Top of loop: `if not budget.record_turn(): return {"ok": False, "summary": "absolute turn limit exceeded"}` followed by the `budget.consume()` / `budget.grace()` block.
4. `_turn_tools: list[str] = []`, then `r = await acompletion_with_failover(...)`, then `msg = r.choices[0].message`.
5. `tool_calls = getattr(msg, "tool_calls", None)`; the `if not tool_calls:` branch `return {"ok": True, "summary": (msg.content or "")[:2000]}`; the per-`tc` loop; `finish` returns; the cheap-tool `budget.refund()` at the bottom.
6. `time.monotonic()` is used at the `tool.start`/`tool.done` emit sites (`_t0 = time.monotonic()` and `duration_ms=int((time.monotonic() - _t0) * 1000)`) — **logging only, never a break condition.** Leave those untouched; we add our OWN `start`/`now` clock.

The implementer **must** re-open the file and re-locate the `while True:` header and the `budget.record_turn()` / `budget.consume()` / `budget.grace()` block before applying edits — surrounding line numbers will have drifted.

---

## File Map

| File | Create/Modify | Responsibility |
|---|---|---|
| `services/orchestrator/progress_breaker.py` | **Create** | Pure, thread-safe no-progress breaker: `ProgressBreaker` state + `step(made_progress, *, cap) -> ProgressStep(consecutive, tripped)`. No async, no I/O. Mirrors `iteration_budget.py` style. |
| `services/orchestrator/coding_orchestrator.py` | **Modify** (`_run_react_loop` only) | (A) capture `start = self._now()` once before the loop; at top of each turn, if `LABMATE_GOAL_DEADLINE_S > 0` and `self._now() - start > deadline`, return the deadline result. (B) instantiate `ProgressBreaker`; after each turn compute `made_progress`; call `breaker.step(...)`; on `tripped` return the breaker result. Add an injectable `self._now` clock (defaults to `time.monotonic`). |
| `tests/services/orchestrator/test_progress_breaker.py` | **Create** | Exhaustive unit tests for the pure breaker (increment / reset / trip-at-cap / cap-0-disables / decision table). |
| `tests/services/orchestrator/test_wall_clock_idle_breaker.py` | **Create** | Unit tests for the wire-in: deadline trips with injected clock; breaker trips on N idle turns; productive loop untouched. |
| `tests/services/orchestrator/features/wall_clock_idle_breaker.feature` | **Create** | `@mocked` Gherkin: deadline-exceeded loop stops; N consecutive no-progress turns trip the breaker; productive loop finishes normally. |
| `tests/services/orchestrator/test_wall_clock_idle_breaker_bdd.py` | **Create** | pytest-bdd step defs binding the `.feature` (patches `litellm.acompletion` with a `side_effect` list, injects a fake clock). |

---

## Behavior (BDD) — Gherkin

Full `.feature` content for Task 5 (`tests/services/orchestrator/features/wall_clock_idle_breaker.feature`):

```gherkin
@mocked
Feature: Wall-clock deadline and no-progress breaker for the ReAct loop
  As the ReAct loop orchestrator
  I want a per-goal wall-clock deadline and a no-progress idle breaker
  beyond plain step counting
  So that a stalled or runaway loop hard-stops with a clear message
  And a normal productive loop is never affected

  Scenario: a loop that exceeds the wall-clock deadline stops
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 10
    And the wall-clock deadline is 5 seconds
    And the no-progress limit is 0
    And the fake clock advances 4 seconds per turn
    And the model calls run_bash with command "echo 1" on turn 1
    And the model calls run_bash with command "echo 2" on turn 2
    And the model calls run_bash with command "echo 3" on turn 3
    When react_execute runs the goal "spin past the deadline"
    Then the result ok is False
    And the result summary contains "wall-clock deadline exceeded"
    And the model was called exactly 2 times

  Scenario: N consecutive no-progress turns trip the breaker
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 20
    And the wall-clock deadline is 0 seconds
    And the no-progress limit is 3
    And the fake clock advances 0 seconds per turn
    And the model returns an empty no-progress turn every turn
    When react_execute runs the goal "make no progress forever"
    Then the result ok is False
    And the result summary contains "no-progress breaker tripped"
    And the model was called exactly 3 times

  Scenario: a productive loop finishes normally untouched
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 10
    And the wall-clock deadline is 600 seconds
    And the no-progress limit is 5
    And the fake clock advances 1 seconds per turn
    And the model calls run_bash with command "echo work" on turn 1
    And the model calls finish with summary "all done" on turn 2
    When react_execute runs the goal "do real work then finish"
    Then the result ok is True
    And the result summary contains "all done"
    And the model was called exactly 2 times
```

Notes on the three scenarios:
- **Deadline scenario:** clock advances 4s/turn, deadline 5s. Turn 1 starts at elapsed 0 (`start` captured, then clock ticks to 4 at the post-turn read). Turn 2 top-of-loop reads elapsed 4 (≤5, proceeds), runs, clock → 8. Turn 3 top-of-loop reads elapsed 8 (>5) → return deadline result. So the model is called exactly twice. The no-progress limit is 0 (disabled) so only the deadline can fire.
- **No-progress scenario:** deadline disabled (0). Each turn returns an assistant message with empty content and NO tool calls — but the loop's `if not tool_calls:` returns early with `ok: True`. To keep the loop spinning for the breaker test we instead script turns that DO make a tool call whose result is treated as no-progress; see Task 4/6 for the exact `made_progress` definition and how the step def scripts an "empty no-progress turn" (a tool call that yields no new assistant content and is not `finish`). The breaker trips at cap 3.
- **Productive scenario:** deadline 600s, limit 5, clock 1s/turn — neither guard fires; the loop finishes via `finish` on turn 2.

---

## Task 1: Pure `ProgressBreaker` module

**Files:**
- Create: `services/orchestrator/progress_breaker.py`
- Test: `tests/services/orchestrator/test_progress_breaker.py`

**Interfaces:**
- Consumes: nothing (pure, stdlib only).
- Produces (later tasks rely on these exact names/types):
  - `ProgressStep` — frozen dataclass with fields `consecutive: int`, `tripped: bool`.
  - `ProgressBreaker(default_cap: int = 5)` — thread-safe; method `step(self, made_progress: bool, *, cap: int | None = None) -> ProgressStep`. Increments the internal consecutive counter when `made_progress` is False; resets it to 0 when True. Trips (`tripped=True`) when `consecutive >= cap` and `cap > 0`. `cap == 0` disables (never trips). When `cap is None`, uses `default_cap`. Read-only properties: `consecutive: int`, `tripped: bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_progress_breaker.py`:

```python
from __future__ import annotations

import pytest

from services.orchestrator.progress_breaker import ProgressBreaker, ProgressStep


@pytest.mark.mocked
class TestProgressBreakerCore:
    def test_starts_at_zero_not_tripped(self):
        b = ProgressBreaker(default_cap=5)
        assert b.consecutive == 0
        assert b.tripped is False

    def test_no_progress_increments_consecutive(self):
        b = ProgressBreaker(default_cap=5)
        s = b.step(False, cap=5)
        assert isinstance(s, ProgressStep)
        assert s.consecutive == 1
        assert s.tripped is False
        s2 = b.step(False, cap=5)
        assert s2.consecutive == 2
        assert s2.tripped is False

    def test_progress_resets_consecutive_to_zero(self):
        b = ProgressBreaker(default_cap=5)
        b.step(False, cap=5)
        b.step(False, cap=5)
        assert b.consecutive == 2
        s = b.step(True, cap=5)
        assert s.consecutive == 0
        assert s.tripped is False
        assert b.consecutive == 0

    def test_trips_exactly_at_cap(self):
        b = ProgressBreaker(default_cap=5)
        # cap is 3: third consecutive no-progress turn trips.
        assert b.step(False, cap=3).tripped is False  # 1
        assert b.step(False, cap=3).tripped is False  # 2
        s = b.step(False, cap=3)                       # 3 == cap
        assert s.consecutive == 3
        assert s.tripped is True
        assert b.tripped is True

    def test_does_not_trip_below_cap(self):
        b = ProgressBreaker(default_cap=5)
        for _ in range(4):
            assert b.step(False, cap=5).tripped is False
        assert b.consecutive == 4

    def test_progress_before_cap_prevents_trip(self):
        b = ProgressBreaker(default_cap=5)
        b.step(False, cap=3)   # 1
        b.step(False, cap=3)   # 2
        b.step(True, cap=3)    # reset -> 0
        assert b.step(False, cap=3).tripped is False  # 1 again
        assert b.step(False, cap=3).tripped is False  # 2 again

    def test_cap_zero_disables_breaker(self):
        b = ProgressBreaker(default_cap=5)
        for _ in range(50):
            s = b.step(False, cap=0)
            assert s.tripped is False
        # Counter still climbs, but it can never trip.
        assert b.consecutive == 50
        assert b.tripped is False

    def test_cap_none_uses_default_cap(self):
        b = ProgressBreaker(default_cap=2)
        assert b.step(False).tripped is False  # 1
        s = b.step(False)                       # 2 == default_cap
        assert s.tripped is True

    def test_decision_table_idle_no_progress_increments(self):
        # (idle & !progress) -> +1
        b = ProgressBreaker(default_cap=5)
        assert b.step(False, cap=5).consecutive == 1

    def test_decision_table_progress_resets(self):
        # progress -> reset 0
        b = ProgressBreaker(default_cap=5)
        b.step(False, cap=5)
        assert b.step(True, cap=5).consecutive == 0

    def test_progress_step_is_frozen(self):
        s = ProgressStep(consecutive=1, tripped=False)
        with pytest.raises(Exception):
            s.consecutive = 2  # type: ignore[misc]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_progress_breaker.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.progress_breaker'`.

- [ ] **Step 3: Write the minimal implementation**

Create `services/orchestrator/progress_breaker.py`:

```python
"""No-progress (idle) breaker for the ReAct loop — pure and thread-safe.

A second guard layered on top of ``IterationBudget``'s step counting. Modeled
on openclaw's ``stepIdleTimeoutBreaker``: a PURE counter that

  * increments on a turn that made NO completed progress,
  * RESETS to 0 the moment a turn makes real progress (new assistant content,
    a tool call/result, or ``finish``),
  * trips (hard stop) once the consecutive no-progress count reaches ``cap``.

Decision table (per turn):
  * made_progress is False, cap > 0  -> consecutive += 1; tripped when >= cap
  * made_progress is True            -> consecutive = 0;  tripped is False
  * cap == 0                         -> never trips (counter still climbs)

Pure module: no async, no I/O, no orchestrator imports. Fully unit-testable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressStep:
    """Result of a single ``ProgressBreaker.step`` call."""

    consecutive: int
    tripped: bool


class ProgressBreaker:
    """Thread-safe consecutive-no-progress counter with a trip cap.

    ``step(made_progress, *, cap=None)`` records one turn and returns a
    :class:`ProgressStep`. A turn that made no completed progress increments
    the consecutive counter; a turn that made progress resets it to 0. The
    breaker trips when the consecutive count reaches ``cap`` (``cap > 0``); a
    ``cap`` of ``0`` disables tripping entirely. When ``cap`` is ``None`` the
    ``default_cap`` supplied at construction is used.
    """

    def __init__(self, default_cap: int = 5):
        self.default_cap = default_cap
        self._consecutive = 0
        self._tripped = False
        self._lock = threading.Lock()

    def step(self, made_progress: bool, *, cap: int | None = None) -> ProgressStep:
        effective_cap = self.default_cap if cap is None else cap
        with self._lock:
            if made_progress:
                self._consecutive = 0
            else:
                self._consecutive += 1
            self._tripped = effective_cap > 0 and self._consecutive >= effective_cap
            return ProgressStep(consecutive=self._consecutive, tripped=self._tripped)

    @property
    def consecutive(self) -> int:
        with self._lock:
            return self._consecutive

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped


__all__ = ["ProgressBreaker", "ProgressStep"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_progress_breaker.py -q`
Expected: PASS — all tests green.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/progress_breaker.py tests/services/orchestrator/test_progress_breaker.py
git commit -m "feat(orchestrator): pure no-progress breaker for ReAct loop"
```

---

## Task 2: Injectable clock on `AsyncOrchestrator`

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (`AsyncOrchestrator.__init__` only)
- Test: `tests/services/orchestrator/test_wall_clock_idle_breaker.py` (first test)

**Interfaces:**
- Consumes: existing `AsyncOrchestrator.__init__(self, skill_router=None, mcp=None, workspace=..., ...)` — **re-verify its current signature before editing** (a concurrent workflow may have changed it). Add the new param with a default at the END so it is backward-compatible.
- Produces: `self._now: Callable[[], float]` — a monotonic clock used by `_run_react_loop`. Defaults to `time.monotonic`. Tests pass a fake clock to make the wall-clock deadline deterministic.

- [ ] **Step 1: Write the failing test**

Create `tests/services/orchestrator/test_wall_clock_idle_breaker.py`:

```python
from __future__ import annotations

import time

import pytest

from services.orchestrator.coding_orchestrator import AsyncOrchestrator


@pytest.mark.mocked
def test_now_defaults_to_time_monotonic():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    assert orch._now is time.monotonic


@pytest.mark.mocked
def test_now_is_injectable():
    calls = {"n": 0}

    def fake_clock() -> float:
        calls["n"] += 1
        return float(calls["n"])

    orch = AsyncOrchestrator(
        skill_router=None, mcp=None, workspace="/tmp", now=fake_clock
    )
    assert orch._now is fake_clock
    assert orch._now() == 1.0
    assert orch._now() == 2.0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_wall_clock_idle_breaker.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'now'`.

- [ ] **Step 3: Add the injectable clock**

Re-open `services/orchestrator/coding_orchestrator.py`, locate `class AsyncOrchestrator` and its `def __init__`. At the top of the file the imports already include `import time` and `from typing import AsyncGenerator` — extend the typing import to also bring in `Callable`:

```python
from typing import AsyncGenerator, Callable
```

Add a `now` keyword-only-friendly parameter at the END of `__init__`'s signature (after the last existing parameter), defaulting to `None`, and store the resolved clock. Example (adapt to the ACTUAL current parameter list — keep every existing parameter unchanged and append `now=None` last):

```python
    def __init__(
        self,
        skill_router=None,
        mcp=None,
        workspace: str = "/workspace",
        # ... KEEP every other existing parameter exactly as-is ...
        now: Callable[[], float] | None = None,
    ):
        # ... existing body unchanged ...
        self._now: Callable[[], float] = now if now is not None else time.monotonic
```

If `__init__` already sets many attributes, add the `self._now = ...` line beside the other attribute assignments. Do not remove or reorder any existing assignment.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_wall_clock_idle_breaker.py -q`
Expected: PASS — both clock tests green.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_wall_clock_idle_breaker.py
git commit -m "feat(orchestrator): injectable monotonic clock on AsyncOrchestrator"
```

---

## Task 3: Wall-clock deadline in `_run_react_loop`

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (`_run_react_loop` — top of the `while True:` loop)
- Test: `tests/services/orchestrator/test_wall_clock_idle_breaker.py` (append deadline tests)

**Interfaces:**
- Consumes: `self._now` (Task 2); `IterationBudget` budget block (unchanged).
- Produces: when `LABMATE_GOAL_DEADLINE_S > 0` and elapsed since loop start exceeds it, `_run_react_loop` returns `{"ok": False, "summary": "wall-clock deadline exceeded"}`. `0` disables the deadline.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_wall_clock_idle_breaker.py`:

```python
import json
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import run_async


def _bash_msg(command: str):
    tc = MagicMock()
    tc.id = f"call-{command}"
    tc.function = MagicMock()
    tc.function.name = "run_bash"
    tc.function.arguments = json.dumps({"command": command})
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


def _orch_with_clock(clock):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp", now=clock)
    mcp = AsyncMock()
    res = MagicMock()
    res.content = [MagicMock(text="output")]
    res.isError = False
    mcp.call_tool.return_value = res
    orch.mcp = mcp
    return orch


@pytest.mark.mocked
def test_wall_clock_deadline_stops_loop(monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", "5")
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", "0")  # isolate the deadline

    ticks = iter([0.0, 4.0, 8.0, 12.0, 16.0])  # start, then per-turn reads
    orch = _orch_with_clock(lambda: next(ticks))
    orch.max_steps = 10

    responses = [_bash_msg("echo 1"), _bash_msg("echo 2"), _bash_msg("echo 3")]
    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock, side_effect=responses,
    ) as mock:
        result = run_async(orch.react_execute("spin past the deadline"))

    assert result["ok"] is False
    assert "wall-clock deadline exceeded" in result["summary"]
    # start@0; turn1 reads 4 (<=5, runs); turn2 reads 8 (>5) -> stop. 1 call? No:
    # start consumes one tick, turn1 reads tick #2 (=4), runs model call #1,
    # turn2 reads tick #3 (=8) -> stop before call #2 => exactly 1 model call.
    assert mock.await_count == 1


@pytest.mark.mocked
def test_deadline_zero_disables(monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", "0")
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", "0")
    # A clock that would blow any positive deadline, but deadline is disabled.
    ticks = iter([0.0] + [10_000.0] * 10)
    orch = _orch_with_clock(lambda: next(ticks))
    orch.max_steps = 2

    # finish on turn 1 so the loop ends cleanly via normal completion.
    fin = MagicMock()
    fin.id = "call-finish"
    fin.function = MagicMock()
    fin.function.name = "finish"
    fin.function.arguments = json.dumps({"summary": "done, deadline off"})
    fmsg = MagicMock()
    fmsg.tool_calls = [fin]
    fmsg.content = ""
    fmsg.reasoning_content = ""
    fmsg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    finish_resp = MagicMock(choices=[MagicMock(message=fmsg)])

    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock, side_effect=[finish_resp],
    ):
        result = run_async(orch.react_execute("disabled deadline"))

    assert result["ok"] is True
    assert "done, deadline off" in result["summary"]
```

> Implementer note: the exact tick→call-count mapping depends on how many times `self._now()` is read per turn. The plan specifies reading the clock **once per turn, at the very top of the loop body** (one read for the deadline check) — plus one read for `start` before the loop. If you add a second per-turn read, update the `ticks` iterator and the `await_count` assertion to match. Re-run and adjust the scripted ticks until the assertion reflects the documented "stop on the turn whose top-of-loop elapsed first exceeds the deadline" behavior.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_wall_clock_idle_breaker.py -q -k deadline`
Expected: FAIL — `test_wall_clock_deadline_stops_loop` does not stop (summary lacks "wall-clock deadline exceeded"); the loop runs all 3 scripted responses.

- [ ] **Step 3: Add the deadline check**

In `_run_react_loop`, re-locate the block that reads `cap = int(os.getenv("LABMATE_MAX_ITERATIONS", ...))` / `budget = IterationBudget(max_total=cap)` / `try:` / `while True:`. Capture `start` from the injectable clock just **before** the `while True:`, read the deadline env once, and add the deadline check as the **first statement inside the loop body** (before `budget.record_turn()`):

```python
        cap = int(os.getenv("LABMATE_MAX_ITERATIONS", str(self.max_steps)))
        budget = IterationBudget(max_total=cap)
        # Wall-clock deadline (guard layered on top of step counting). 0 disables.
        deadline_s = float(os.getenv("LABMATE_GOAL_DEADLINE_S", "600"))
        start = self._now()
        try:
            while True:
                # Wall-clock guard: stop if this goal has run past its deadline.
                if deadline_s > 0 and (self._now() - start) > deadline_s:
                    return {
                        "ok": False,
                        "summary": "wall-clock deadline exceeded",
                    }

                # Hard absolute ceiling (prevents infinite loops of distinct cheap reads).
                if not budget.record_turn():
                    return {"ok": False, "summary": "absolute turn limit exceeded"}
                # ... existing budget.consume()/grace() block and the rest of the loop
                #     body UNCHANGED ...
```

Do not touch the `_t0 = time.monotonic()` / `duration_ms=...` emit sites — those stay as-is (logging only).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_wall_clock_idle_breaker.py -q -k deadline`
Expected: PASS — both deadline tests green. (Adjust the scripted `ticks` per the implementer note if the call count is off by one.)

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_wall_clock_idle_breaker.py
git commit -m "feat(orchestrator): per-goal wall-clock deadline in _run_react_loop"
```

---

## Task 4: Wire the no-progress breaker into `_run_react_loop`

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (`_run_react_loop` — import + per-turn progress accounting)
- Test: `tests/services/orchestrator/test_wall_clock_idle_breaker.py` (append breaker tests)

**Interfaces:**
- Consumes: `ProgressBreaker`, `ProgressStep` from `services.orchestrator.progress_breaker` (Task 1).
- Produces: when `LABMATE_NOPROGRESS_LIMIT > 0` and the breaker trips after `N` consecutive no-progress turns, `_run_react_loop` returns `{"ok": False, "summary": "no-progress breaker tripped (N consecutive idle turns)"}` where `N` is the consecutive count. `0` disables.
- **`made_progress` definition for a ReAct turn** = the turn produced *real* output: it has at least one tool call (`tool_calls` non-empty), OR new non-empty assistant content (`msg.content`), OR it is a `finish`. A turn that returns no tool calls and no content is no-progress. (Because the loop returns early on `not tool_calls`, the breaker only meaningfully accumulates across turns when content is empty AND tool calls keep coming but the model is otherwise stalled; the breaker is the belt-and-suspenders guard for that degenerate case — see the BDD no-progress scenario in Task 5 for how it is exercised.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_wall_clock_idle_breaker.py`:

```python
def _noprogress_msg():
    """A turn that calls a tool but yields no new assistant content — the
    degenerate 'spinning' turn the breaker is meant to catch.

    It is NOT finish and produces empty content; we use run_bash so the loop
    keeps going (a no-tool-call turn would return early). The step def treats
    these as no-progress via the made_progress rule (empty content + this turn
    flagged as not advancing). For the unit test we drive the breaker directly
    by scripting turns that the loop counts as idle.
    """
    return _bash_msg("noop")


@pytest.mark.mocked
def test_no_progress_breaker_trips_at_limit(monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", "0")   # isolate the breaker
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", "3")

    orch = _orch_with_clock(lambda: 0.0)
    orch.max_steps = 20

    # Force every turn to be counted as no-progress by stubbing the progress
    # decision to False (see Step 3 for the seam). Here we script enough turns.
    responses = [_noprogress_msg() for _ in range(10)]
    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock, side_effect=responses,
    ) as mock, patch.object(
        AsyncOrchestrator, "_turn_made_progress", return_value=False
    ):
        result = run_async(orch.react_execute("make no progress forever"))

    assert result["ok"] is False
    assert "no-progress breaker tripped" in result["summary"]
    assert "3" in result["summary"]
    assert mock.await_count == 3


@pytest.mark.mocked
def test_no_progress_limit_zero_disables(monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", "0")
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", "0")

    orch = _orch_with_clock(lambda: 0.0)
    orch.max_steps = 2  # IterationBudget still bounds the loop

    responses = [_noprogress_msg() for _ in range(10)]
    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new_callable=AsyncMock, side_effect=responses,
    ), patch.object(AsyncOrchestrator, "_turn_made_progress", return_value=False):
        result = run_async(orch.react_execute("breaker disabled"))

    # Breaker off -> IterationBudget ends it ("budget exhausted"), not the breaker.
    assert result["ok"] is False
    assert "no-progress breaker tripped" not in result["summary"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_wall_clock_idle_breaker.py -q -k no_progress`
Expected: FAIL — `AttributeError: ... no attribute '_turn_made_progress'` and the breaker summary is absent.

- [ ] **Step 3: Add the breaker wiring + a `_turn_made_progress` seam**

Add the import near the existing budget import in `coding_orchestrator.py`:

```python
from .progress_breaker import ProgressBreaker, ProgressStep
```

Add a small instance method on `AsyncOrchestrator` (a testable seam for the `made_progress` decision). Place it next to `_run_react_loop`:

```python
    @staticmethod
    def _turn_made_progress(*, has_tool_calls: bool, content: str | None, is_finish: bool) -> bool:
        """A ReAct turn made progress if it produced real output: a tool call,
        new non-empty assistant content, or a finish. Used by the no-progress
        breaker to decide whether to increment or reset its idle counter.
        """
        return bool(is_finish or has_tool_calls or (content or "").strip())
```

In `_run_react_loop`, instantiate the breaker beside the budget, and call it at the END of each turn (after the tool-result appends / cheap-refund, before the loop repeats). Read the limit env once before the loop:

```python
        cap = int(os.getenv("LABMATE_MAX_ITERATIONS", str(self.max_steps)))
        budget = IterationBudget(max_total=cap)
        deadline_s = float(os.getenv("LABMATE_GOAL_DEADLINE_S", "600"))
        noprogress_limit = int(os.getenv("LABMATE_NOPROGRESS_LIMIT", "5"))
        breaker = ProgressBreaker(default_cap=noprogress_limit)
        start = self._now()
        try:
            while True:
                # ... deadline check (Task 3) ...
                # ... budget.record_turn() / consume() / grace() ...
                # ... model call, assistant append, tool_calls extraction ...
                # ... the `if not tool_calls:` early return stays as-is ...
                # ... per-tc loop, finish return, cheap-refund stay as-is ...

                # No-progress breaker (after the turn's work). Compute whether
                # this turn advanced; a stalled turn increments the idle count.
                made_progress = self._turn_made_progress(
                    has_tool_calls=bool(tool_calls),
                    content=msg.content,
                    is_finish=False,  # finish already returned above
                )
                pstep: ProgressStep = breaker.step(made_progress, cap=noprogress_limit)
                if pstep.tripped:
                    return {
                        "ok": False,
                        "summary": (
                            f"no-progress breaker tripped "
                            f"({pstep.consecutive} consecutive idle turns)"
                        ),
                    }
```

> Implementer note on placement: the breaker `step` call must execute on EVERY turn that does not already return (the `not tool_calls` branch and the `finish`/loop-detect/budget branches return earlier, which is correct — those are terminal). Because a real tool-call turn counts as progress under `_turn_made_progress`, the breaker only trips when `_turn_made_progress` returns False repeatedly — which the tests force via the patched seam, and the BDD no-progress scenario reproduces. Re-verify the breaker call sits at the bottom of the `while True:` body, after the cheap-tool `budget.refund()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_wall_clock_idle_breaker.py -q -k no_progress`
Expected: PASS — both breaker tests green.

- [ ] **Step 5: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_wall_clock_idle_breaker.py
git commit -m "feat(orchestrator): no-progress idle breaker in _run_react_loop"
```

---

## Task 5: BDD feature file

**Files:**
- Create: `tests/services/orchestrator/features/wall_clock_idle_breaker.feature`

**Interfaces:**
- Consumes: nothing at runtime; bound by the step defs in Task 6.
- Produces: the three scenarios bound by `scenarios("features/wall_clock_idle_breaker.feature")`.

- [ ] **Step 1: Create the feature file**

Write `tests/services/orchestrator/features/wall_clock_idle_breaker.feature` with the EXACT Gherkin from the "Behavior (BDD) — Gherkin" section above (the full block beginning `@mocked` / `Feature: Wall-clock deadline and no-progress breaker for the ReAct loop`). Copy it verbatim.

- [ ] **Step 2: Verify it is discovered but unbound (fails)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_wall_clock_idle_breaker_bdd.py -q`
Expected: FAIL — the bound test file does not exist yet (`file or directory not found`). This is expected; Task 6 creates it. (Do not commit alone; commit together with Task 6.)

---

## Task 6: BDD step definitions

**Files:**
- Create: `tests/services/orchestrator/test_wall_clock_idle_breaker_bdd.py`

**Interfaces:**
- Consumes: `AsyncOrchestrator`; `run_async` from `tests.conftest`; the feature from Task 5; the env knobs `LABMATE_GOAL_DEADLINE_S`, `LABMATE_NOPROGRESS_LIMIT`, `LABMATE_MAX_ITERATIONS`.
- Produces: passing `@mocked` BDD scenarios. Mirrors `test_iteration_budget_bdd.py` (patch `litellm.acompletion` with a `side_effect` list); additionally injects a fake clock and sets the two new env knobs.

- [ ] **Step 1: Create the step defs**

Write `tests/services/orchestrator/test_wall_clock_idle_breaker_bdd.py`:

```python
"""Step definitions for the wall-clock deadline + no-progress breaker feature.

Mirrors test_iteration_budget_bdd.py: patches litellm.acompletion with a
scripted side_effect list, plus injects a deterministic fake clock and sets the
two new env knobs (LABMATE_GOAL_DEADLINE_S, LABMATE_NOPROGRESS_LIMIT).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from tests.conftest import run_async

scenarios("features/wall_clock_idle_breaker.feature")


# ── helpers ────────────────────────────────────────────────────────────────

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


@pytest.fixture
def ctx():
    return {
        "responses": [],
        "result": None,
        "mock": None,
        "advance": 0.0,
        "force_idle": False,
    }


# ── Background ───────────────────────────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and no mcp")
def _orch(ctx):
    # Fake monotonic clock: advances by ctx["advance"] seconds on each read.
    state = {"t": 0.0}

    def clock() -> float:
        v = state["t"]
        state["t"] += ctx["advance"]
        return v

    orch = AsyncOrchestrator(
        skill_router=None, mcp=None, workspace="/tmp", now=clock
    )
    mcp = AsyncMock()
    res = MagicMock()
    res.content = [MagicMock(text="output")]
    res.isError = False
    mcp.call_tool.return_value = res
    orch.mcp = mcp
    ctx["orch"] = orch


# ── Given steps ──────────────────────────────────────────────────────────────

@given(parsers.parse("the iteration budget cap is {cap:d}"))
def _set_cap(ctx, cap):
    ctx["orch"].max_steps = cap


@given(parsers.parse("the wall-clock deadline is {seconds:d} seconds"))
def _set_deadline(ctx, seconds, monkeypatch):
    monkeypatch.setenv("LABMATE_GOAL_DEADLINE_S", str(seconds))


@given(parsers.parse("the no-progress limit is {limit:d}"))
def _set_limit(ctx, limit, monkeypatch):
    monkeypatch.setenv("LABMATE_NOPROGRESS_LIMIT", str(limit))


@given(parsers.parse("the fake clock advances {seconds:d} seconds per turn"))
def _set_advance(ctx, seconds):
    ctx["advance"] = float(seconds)


@given(parsers.parse('the model calls run_bash with command "{command}" on turn {turn:d}'))
def _bash_on_turn(ctx, command, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("run_bash", {"command": command})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_on_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


@given("the model returns an empty no-progress turn every turn")
def _always_idle(ctx):
    # A run_bash turn (keeps the loop alive) flagged as no-progress via the
    # _turn_made_progress seam below.
    ctx["force_idle"] = True
    ctx["responses"] = [
        _tool_call_msg("run_bash", {"command": "noop"}) for _ in range(20)
    ]


def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(
            _tool_call_msg("run_bash", {"command": "echo filler"})
        )


# ── When step ────────────────────────────────────────────────────────────────

@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run(ctx, goal):
    patches = [
        patch(
            "services.orchestrator.coding_orchestrator.litellm.acompletion",
            new_callable=AsyncMock, side_effect=ctx["responses"],
        )
    ]
    if ctx["force_idle"]:
        patches.append(
            patch.object(AsyncOrchestrator, "_turn_made_progress", return_value=False)
        )

    with patches[0] as mock:
        if len(patches) > 1:
            with patches[1]:
                ctx["result"] = run_async(ctx["orch"].react_execute(goal))
        else:
            ctx["result"] = run_async(ctx["orch"].react_execute(goal))
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
    assert needle in ctx["result"]["summary"], (
        f"expected '{needle}' in summary, got: {ctx['result']['summary']}"
    )


@then(parsers.parse("the model was called exactly {n:d} times"))
def _called_n(ctx, n):
    assert ctx["mock"].await_count == n
```

> Implementer note: the deadline scenario's expected call count (`exactly 2 times` in the `.feature`) assumes the clock is read once for `start` and once per turn at the top of the loop, with `advance=4` and deadline `5`. Re-run and, if the loop reads the clock a different number of times, reconcile EITHER by adjusting the `.feature` "advances N seconds per turn" / "exactly N times" values OR the per-turn clock-read count — keep the documented behavior ("stop on the first turn whose top-of-loop elapsed exceeds the deadline") and make the numbers consistent. Do the same reconciliation for the no-progress scenario (limit 3 → `exactly 3 times`).

- [ ] **Step 2: Run the BDD scenarios to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_wall_clock_idle_breaker_bdd.py -q`
Expected: PASS — all three scenarios green. (Adjust feature numbers per the implementer note if a count is off by one, then re-run.)

- [ ] **Step 3: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add tests/services/orchestrator/features/wall_clock_idle_breaker.feature tests/services/orchestrator/test_wall_clock_idle_breaker_bdd.py
git commit -m "test(orchestrator): BDD for wall-clock deadline + no-progress breaker"
```

---

## Task 7: Full regression sweep + docs row

**Files:**
- Modify: `CLAUDE.md` (append one row to the Harness Robustness table — documentation only)

**Interfaces:**
- Consumes: all prior tasks.
- Produces: green orchestrator + memory suite; a documented env-knob row.

- [ ] **Step 1: Run the full orchestrator + memory suite**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ tests/services/memory/ -q 2>&1 | tail -20`
Expected: PASS — all green (the pre-existing 684 plus the new tests). No regressions. If the BDD `iteration_budget` / `tool_loop_detection` scenarios changed behavior, they must still pass untouched — confirm the new guards did not perturb them (a normal short loop must hit neither guard: deadline default 600s and limit default 5 are both well clear of the short scripted loops).

- [ ] **Step 2: Add the documentation row to `CLAUDE.md`**

In `CLAUDE.md`, locate the Harness Robustness feature table (the one with columns `| Feature | New module | Wires into | Env knobs (default) |`). Append one row:

```markdown
| Wall-clock + no-progress breaker | `services/orchestrator/progress_breaker.py` (`ProgressBreaker`, `ProgressStep`) | `coding_orchestrator.py` `_run_react_loop` — per-turn wall-clock deadline (injectable clock) + idle breaker that trips after N no-progress turns; both layered on top of `IterationBudget` step counting | `LABMATE_GOAL_DEADLINE_S=600` (0 disables), `LABMATE_NOPROGRESS_LIMIT=5` (0 disables) |
```

- [ ] **Step 3: Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add CLAUDE.md
git commit -m "docs(CLAUDE.md): document wall-clock deadline + no-progress breaker"
```

---

## Self-Review

**1. Spec coverage:**
- (A) Wall-clock deadline in `_run_react_loop`, `start` captured once, top-of-turn check, env `LABMATE_GOAL_DEADLINE_S` default 600 / 0 disables, returns `{"ok": False, "summary": "wall-clock deadline exceeded"}` → Tasks 2 (clock) + 3. ✓
- (B) Pure module `progress_breaker.py` modeled on openclaw's pure breaker; `ProgressBreaker.step(made_progress, *, cap) -> ProgressStep(consecutive, tripped)`; increments on no-progress, resets on progress, trips at cap, cap 0 disables; wired into loop returning `{"ok": False, "summary": "no-progress breaker tripped (N consecutive idle turns)"}`; env `LABMATE_NOPROGRESS_LIMIT` default 5 / 0 disables → Tasks 1 + 4. ✓
- Breaker is PURE + exhaustively unit-tested (increment / reset / trip-at-cap / cap-0-disables / decision table / frozen result) → Task 1. ✓
- Wall-clock uses an injectable clock for tests → Task 2 (`now=` param, defaults to `time.monotonic`). ✓
- Additive + regression-safe (normal short loop hits neither guard) → Task 7 full sweep + the productive BDD scenario. ✓
- Mirrors `iteration_budget.py`'s pure, thread-safe style (`threading.Lock`, `__all__`, no async/IO) → Task 1. ✓
- Full `.feature` with all three required scenarios (deadline stops; N idle turns trip; productive loop untouched) → "Behavior (BDD)" section + Task 5. ✓
- BDD contract reused, not recreated (pytest-bdd, `fake_model`/scripted `side_effect`, `@mocked`, feature + `test_*_bdd.py`, `run_async`) → Tasks 5–6 mirror `test_iteration_budget_bdd.py`. ✓
- Structural anchoring (not line numbers) + re-verify instruction → "Structural Anchors" section + per-task re-locate notes. ✓

**2. Placeholder scan:** No "TBD"/"TODO"/"handle edge cases"/"similar to Task N". Every code step shows full code. The two implementer notes are reconciliation guidance for an off-by-one call count (a genuine re-verify the spec demanded), not missing content — the documented behavior and the code are fully specified. ✓

**3. Type consistency:** `ProgressStep(consecutive: int, tripped: bool)` and `ProgressBreaker.step(made_progress: bool, *, cap: int | None = None) -> ProgressStep` are used identically in Tasks 1 and 4. `self._now: Callable[[], float]` defined in Task 2, consumed in Task 3. `_turn_made_progress(*, has_tool_calls, content, is_finish) -> bool` defined and called consistently in Task 4 and patched by name in the tests/BDD. Env knob names (`LABMATE_GOAL_DEADLINE_S`, `LABMATE_NOPROGRESS_LIMIT`) and summary strings ("wall-clock deadline exceeded", "no-progress breaker tripped (N consecutive idle turns)") match across loop code, unit tests, BDD feature, and docs. ✓
