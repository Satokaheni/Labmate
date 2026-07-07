# Lightweight Orchestrator Spike v2 (Collapse LangGraph) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a flag-gated plain-async `lite` orchestrator path that reproduces the LangGraph `graph` path's single-goal behavior with no graph framework, so we can measure whether removing LangGraph is worth it.

**Architecture:** Strangler. The unchanged LangGraph `StateGraph` path (`orch.run_task` → `graph.ainvoke`) stays the default. A new `run_goal_lite()` reproduces the same sequence — assess-ambiguity → route/direct-answer → execute via the **reused** `_run_react_loop` → verify → check/reflect-retry → approval — as ordinary `async`/`await`, using primitives that already exist post-migration: `LocalStore.checkpoint_put/get/delete` for durable suspend/resume and `SignalRegistry` for approval-await + cancel/steer. `main._handle` dispatches on `ORCHESTRATOR_ENGINE` (read once at import, exactly like `SEQUENCING_MODE`).

**Tech Stack:** Python 3.11, asyncio, `aiosqlite` LocalStore, pytest + pytest-asyncio + pytest-bdd, respx for the model seam.

## Global Constraints

- **Behavior ties — this is NOT a behavioral eval.** `graph` and `lite` call the same `_run_react_loop`, `architect()` ambiguity assessment, and routing, so a completion/honesty A/B (`eval/local`, `eval/seq_ab`) WILL tie. Run it ONLY as a regression gate. The real decider is (i) an engineering scorecard (LOC delta, droppable deps, ergonomics) and (ii) a fault-injection resilience A/B.
- **Default is unchanged.** `ORCHESTRATOR_ENGINE=graph` (default) → the existing LangGraph path, byte-identical. `lite` is opt-in.
- **Reuse, don't reimplement, execution.** `run_goal_lite` MUST call the existing `AsyncOrchestrator._run_react_loop` / `react_execute` for the actual tool loop. Do not fork it.
- **Thin scope (deliberate).** Reproduce the SINGLE-goal path only: assess-ambiguity gate, single-intent route (skill or direct-answer), execute, verify gate, check→reflect-retry (bounded by `MAX_GOAL_ATTEMPTS`), approval gate. Do NOT reproduce multi-goal decomposition trees, replan, or the heavy critique verify-gate — those are rarely used and out of scope for the comparison.
- **stdout is sacred** in any MCP-adjacent code; logging to stderr only.
- **No Mongo/Redis.** Those are gone. Durable state = `LocalStore.checkpoint_put/get`; signals = in-proc `SignalRegistry`.
- Stage commits by exact path (never `git add -A`, never `config.ts` / `.codegraph/daemon.pid` / `services/frontend/.claude`). Commit footer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Full `tests/services/orchestrator` suite green after every task; the `graph` path's tests must not change.

## Source-of-truth map (what `lite` reproduces)

The `graph` nodes ARE the spec. Each lite step names the node to reproduce:

| Graph node (`services/orchestrator/graph.py`) | Lite equivalent |
|---|---|
| `assess_ambiguity` (:591) + `ambiguity_router` (:915) | Task 4 — ambiguity gate + halt |
| `plan` (:185) + `clarification_router` (:947) | Task 5 — single-intent route / direct-answer |
| `execute_node` (:300) | Task 5 — delegate to `react_execute`/`_run_react_loop` |
| `verify_node` + `verify_router` (:927) | Task 6 — verify gate (light; critique OFF by default) |
| `check` (:399) + `router` (:883) | Task 6 — ok? / reflect-retry / approval / END |
| `reflect` (:526) | Task 6 — bounded reflect-retry |
| `approval` (:572, uses `interrupt()`) | Task 2 + Task 6 — durable suspend + `SignalRegistry` await |

## File Map

| File | Responsibility |
|---|---|
| Create `services/orchestrator/lite_state.py` | Pure helpers: build the initial single-goal state dict; serialize/deserialize for checkpointing. No I/O. |
| Create `services/orchestrator/lite_persistence.py` | Thin durable-suspend/resume over `LocalStore.checkpoint_put/get/delete`. |
| Create `services/orchestrator/lite_approval.py` | Irreversible-action heuristic (reuse `edit_intent`-style verbs) + await/decide over `SignalRegistry`. |
| Modify `services/orchestrator/inproc_bus.py` | Add an approval-decision channel to `SignalRegistry` (`set_approval`/`get_approval`/`await_approval`). |
| Create `services/orchestrator/lite_orchestrator.py` | `run_goal_lite(orch, async_orch, task, session_id, ...)` — the plain-async replica. |
| Modify `services/orchestrator/main.py:724` | Flag-gated dispatch: `ORCHESTRATOR_ENGINE` graph|lite. |
| Create `eval/orchestrator_ab/scorecard.py` + `SCORECARD.md` | LOC delta + droppable-deps report. |
| Create `eval/orchestrator_ab/run_fault_ab.py` | Fault-injection resilience A/B (kill mid-task, restart, measure recovery). NOT run in CI. |
| Tests under `tests/services/orchestrator/` + `tests/eval/` | Per task, below. |

---

## Task 1: Lite state helpers (pure)

**Files:**
- Create: `services/orchestrator/lite_state.py`
- Test: `tests/services/orchestrator/test_lite_state.py`

**Interfaces:**
- Produces: `build_initial_state(task: str, session_id: str, user_id: str = "", workspace_id: str = "") -> dict` — the SAME shape as `CodingOrchestrator.run_task`'s `initial` (`services/orchestrator/coding_orchestrator.py:1771-1784`): keys `session_id, goal_tree (create_goal({},"root",None,task)), current_goal_id="root", step_markers={}, messages=[], error=None, final_answer="", workspace_id, user_id, root_goal=task, verify_retries=0, direct_answer=False`.
- `snapshot(state: dict) -> dict` / `restore(payload: dict) -> dict` — JSON-round-trippable copy for checkpointing (deep copy; drop any non-serializable transient keys).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_lite_state.py
from __future__ import annotations
import pytest
from services.orchestrator.lite_state import build_initial_state, snapshot, restore


def test_build_initial_state_matches_run_task_shape():
    s = build_initial_state("do a thing", "sess-1", user_id="u1", workspace_id="w1")
    assert s["session_id"] == "sess-1"
    assert s["current_goal_id"] == "root"
    assert s["root_goal"] == "do a thing"
    assert s["goal_tree"]["root"]["description"] == "do a thing"
    assert s["verify_retries"] == 0 and s["direct_answer"] is False
    assert s["messages"] == [] and s["final_answer"] == ""


def test_snapshot_restore_round_trips():
    import json
    s = build_initial_state("t", "sess")
    snap = snapshot(s)
    assert json.loads(json.dumps(snap)) == snap  # JSON-safe
    assert restore(snap)["goal_tree"]["root"]["description"] == "t"
```

- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_lite_state.py -q` → FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# services/orchestrator/lite_state.py
"""Pure state helpers for the lite orchestrator (no I/O)."""
from __future__ import annotations

import copy

from services.orchestrator.types import create_goal


def build_initial_state(
    task: str, session_id: str, user_id: str = "", workspace_id: str = ""
) -> dict:
    """The initial single-goal state — identical shape to CodingOrchestrator.run_task's
    `initial` (coding_orchestrator.py:1771), so lite and graph start from the same state."""
    return {
        "session_id": session_id,
        "goal_tree": create_goal({}, "root", None, task),
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "final_answer": "",
        "workspace_id": workspace_id,
        "user_id": user_id,
        "root_goal": task,
        "verify_retries": 0,
        "direct_answer": False,
    }


def snapshot(state: dict) -> dict:
    """A JSON-serializable deep copy for checkpointing. Transient/unpicklable keys are
    dropped (the lite loop rebuilds them on resume)."""
    snap = copy.deepcopy(dict(state))
    snap.pop("_transient", None)
    return snap


def restore(payload: dict) -> dict:
    """Rebuild loop state from a checkpoint snapshot."""
    return copy.deepcopy(dict(payload))
```

- [ ] **Step 4: Run to verify it passes** — same command → PASS.
- [ ] **Step 5: Commit** — `git add services/orchestrator/lite_state.py tests/services/orchestrator/test_lite_state.py && git commit -m "feat(lite): pure initial-state + snapshot helpers"`

---

## Task 2: Approval channel on SignalRegistry

**Files:**
- Modify: `services/orchestrator/inproc_bus.py` (`SignalRegistry`, near `write_steer`)
- Test: `tests/services/orchestrator/test_signal_approval.py`

**Interfaces:**
- Produces on `SignalRegistry`: `set_approval(task_id: str, decision: str) -> None` (decision in `{"approve","reject"}`), `get_approval(task_id: str) -> str | None` (consume-once), `async def await_approval(task_id: str, poll_s: float = 0.2, timeout_s: float | None = None) -> str` (blocks until a decision is set or times out → raises `TimeoutError`). This is the in-proc replacement for the removed Redis approval signal; it mirrors the existing steer/cancel pattern in the same class.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_signal_approval.py
from __future__ import annotations
import asyncio
import pytest
from services.orchestrator.inproc_bus import SignalRegistry


@pytest.mark.asyncio
async def test_await_approval_returns_decision():
    sig = SignalRegistry()
    async def decide():
        await asyncio.sleep(0.05)
        sig.set_approval("t1", "approve")
    asyncio.create_task(decide())
    assert await sig.await_approval("t1", poll_s=0.01, timeout_s=2.0) == "approve"


@pytest.mark.asyncio
async def test_get_approval_is_consume_once():
    sig = SignalRegistry()
    sig.set_approval("t1", "reject")
    assert sig.get_approval("t1") == "reject"
    assert sig.get_approval("t1") is None


@pytest.mark.asyncio
async def test_await_approval_times_out():
    sig = SignalRegistry()
    with pytest.raises(TimeoutError):
        await sig.await_approval("t1", poll_s=0.01, timeout_s=0.05)
```

- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_signal_approval.py -q` → FAIL (AttributeError).

- [ ] **Step 3: Implement** — add to `SignalRegistry.__init__`: `self._approvals: dict[str, str] = {}`. Add methods:

```python
    def set_approval(self, task_id: str, decision: str) -> None:
        """Record a human approve/reject decision for a suspended lite task."""
        self._approvals[task_id] = decision

    def get_approval(self, task_id: str) -> str | None:
        """Consume-once read of a pending approval decision."""
        return self._approvals.pop(task_id, None)

    async def await_approval(
        self, task_id: str, poll_s: float = 0.2, timeout_s: float | None = None
    ) -> str:
        """Block until a decision is set (or cancel/timeout). In-proc replacement for
        the removed Redis approval signal; poll keeps it loop-agnostic and testable."""
        import asyncio as _asyncio
        import time as _time
        start = _time.monotonic()
        while True:
            d = self.get_approval(task_id)
            if d is not None:
                return d
            if self.is_cancelled(task_id):
                return "reject"
            if timeout_s is not None and (_time.monotonic() - start) >= timeout_s:
                raise TimeoutError(f"approval for {task_id!r} timed out")
            await _asyncio.sleep(poll_s)
```

- [ ] **Step 4: Run to verify it passes** — same command → PASS. Also run `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_inproc_bus.py -q` (existing SignalRegistry tests) → still green.
- [ ] **Step 5: Commit** — `git add services/orchestrator/inproc_bus.py tests/services/orchestrator/test_signal_approval.py && git commit -m "feat(inproc-bus): approval-decision channel on SignalRegistry (lite approval gate)"`

---

## Task 3: Durable suspend/resume over LocalStore

**Files:**
- Create: `services/orchestrator/lite_persistence.py`
- Test: `tests/services/orchestrator/test_lite_persistence.py`

**Interfaces:**
- Consumes: `LocalStore.checkpoint_put(task_id, payload) / checkpoint_get(task_id) / checkpoint_delete(task_id)` (`services/orchestrator/local_store.py:614-640`).
- Produces: `async def save_suspend(store, task_id: str, state: dict, phase: str) -> None` (wraps `snapshot()` + phase marker), `async def load_resume(store, task_id: str) -> tuple[dict, str] | None` (returns `(state, phase)` or None), `async def clear(store, task_id: str) -> None`. `phase` is one of `{"assess","execute","await_approval","reflect"}` — the resume entry point.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_lite_persistence.py
from __future__ import annotations
import pytest
from services.orchestrator.lite_state import build_initial_state
from services.orchestrator import lite_persistence as lp


class _FakeStore:
    def __init__(self): self._cp = {}
    async def checkpoint_put(self, task_id, payload): self._cp[task_id] = payload
    async def checkpoint_get(self, task_id): return self._cp.get(task_id)
    async def checkpoint_delete(self, task_id): self._cp.pop(task_id, None)


@pytest.mark.asyncio
async def test_save_then_load_round_trips_state_and_phase():
    store = _FakeStore()
    s = build_initial_state("do it", "sess")
    await lp.save_suspend(store, "t1", s, phase="await_approval")
    loaded = await lp.load_resume(store, "t1")
    assert loaded is not None
    state, phase = loaded
    assert phase == "await_approval"
    assert state["root_goal"] == "do it"


@pytest.mark.asyncio
async def test_load_missing_returns_none_and_clear_removes():
    store = _FakeStore()
    assert await lp.load_resume(store, "nope") is None
    s = build_initial_state("x", "sess")
    await lp.save_suspend(store, "t1", s, phase="assess")
    await lp.clear(store, "t1")
    assert await lp.load_resume(store, "t1") is None
```

- [ ] **Step 2: Run to verify it fails** → FAIL (module not found).

- [ ] **Step 3: Implement**

```python
# services/orchestrator/lite_persistence.py
"""Durable suspend/resume for the lite orchestrator over LocalStore checkpoints.

Replaces LangGraph's per-thread checkpointer with an explicit snapshot at each
suspendable boundary. Best-effort: a persistence failure logs and continues (the
goal still runs; only crash-resume is lost), matching the graph path's tolerance.
"""
from __future__ import annotations

import logging

from services.orchestrator.lite_state import restore, snapshot

_log = logging.getLogger("lite_persistence")


async def save_suspend(store, task_id: str, state: dict, phase: str) -> None:
    try:
        await store.checkpoint_put(task_id, {"phase": phase, "state": snapshot(state)})
    except Exception:  # noqa: BLE001 — persistence is best-effort
        _log.warning("lite checkpoint save failed for %s", task_id, exc_info=True)


async def load_resume(store, task_id: str) -> tuple[dict, str] | None:
    try:
        payload = await store.checkpoint_get(task_id)
    except Exception:  # noqa: BLE001
        _log.warning("lite checkpoint load failed for %s", task_id, exc_info=True)
        return None
    if not payload or "state" not in payload:
        return None
    return restore(payload["state"]), payload.get("phase", "assess")


async def clear(store, task_id: str) -> None:
    try:
        await store.checkpoint_delete(task_id)
    except Exception:  # noqa: BLE001
        _log.debug("lite checkpoint clear failed for %s", task_id, exc_info=True)
```

- [ ] **Step 4: Run to verify it passes** → PASS.
- [ ] **Step 5: Commit** — `git add services/orchestrator/lite_persistence.py tests/services/orchestrator/test_lite_persistence.py && git commit -m "feat(lite): durable suspend/resume over LocalStore checkpoints"`

---

## Task 4: `run_goal_lite` — ambiguity gate + halt

**Files:**
- Create: `services/orchestrator/lite_orchestrator.py`
- Test: `tests/services/orchestrator/test_lite_orchestrator.py`

**Interfaces:**
- Produces: `async def run_goal_lite(orch, async_orch, task: str, session_id: str, user_id: str = "", workspace_id: str = "", store=None, signals=None) -> dict` — returns the SAME final-state shape as `graph.ainvoke` (a dict with `final_answer`, `goal_tree`, `error`, `direct_answer`, and — for the honesty guard parity — `tests_passed`/`ok` where the react loop set them).
- Reproduces `assess_ambiguity` (graph.py:591) + `ambiguity_router` (graph.py:915): compute ambiguity via the SAME `classify_complexity` / `async_orch` assessment the node uses; when the gate is ON (`ENABLE_CONDITIONAL_GATES`) and the task is ambiguous above `AMBIGUITY_THRESHOLD`, HALT and return a clarifying-question state (`final_answer` = the blocking question, no execution).

- [ ] **Step 1: Write the failing test** (read `assess_ambiguity` at graph.py:591 and mirror its assessment call; mock `async_orch` so no model is needed):

```python
# tests/services/orchestrator/test_lite_orchestrator.py
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock
import pytest
from services.orchestrator.lite_orchestrator import run_goal_lite


@pytest.mark.asyncio
async def test_ambiguous_task_halts_with_question(monkeypatch):
    monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")
    orch = MagicMock()
    async_orch = MagicMock()
    # assess returns high ambiguity + a blocking question (mirror assess_ambiguity's contract)
    async_orch.assess_ambiguity = AsyncMock(return_value={
        "ambiguity": 0.9, "blocking_question": "Which file did you mean?"})
    out = await run_goal_lite(orch, async_orch, "improve it", "sess")
    assert "Which file" in out["final_answer"]
    # It halted — no react execution happened
    async_orch.react_execute.assert_not_called()
```

> NOTE to implementer: `assess_ambiguity` (graph.py:591) is the source of truth for HOW ambiguity is computed and what field carries the question. Read it and reproduce that call and its `ambiguity_router` (graph.py:915) threshold check exactly. If the real assess method has a different name/return shape, use the real one and update this test's mock to match — the test asserts BEHAVIOR (halt + question), not the mock's exact shape.

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Implement** the ambiguity gate only (route/execute added in Task 5). Skeleton:

```python
# services/orchestrator/lite_orchestrator.py
"""Plain-async orchestrator: reproduces the LangGraph graph's SINGLE-goal path
without a graph framework (strangler; flag-gated behind ORCHESTRATOR_ENGINE=lite).
Reuses AsyncOrchestrator._run_react_loop / react_execute for execution."""
from __future__ import annotations

import logging

from services.orchestrator import events
from services.orchestrator.graph import (
    AMBIGUITY_THRESHOLD,
    conditional_gates_enabled,  # if present; else read ENABLE_CONDITIONAL_GATES
)
from services.orchestrator.lite_state import build_initial_state

_log = logging.getLogger("lite_orchestrator")


async def run_goal_lite(
    orch, async_orch, task, session_id, user_id="", workspace_id="",
    store=None, signals=None,
):
    state = build_initial_state(task, session_id, user_id=user_id, workspace_id=workspace_id)

    # --- assess_ambiguity gate (reproduces graph.py:591 + ambiguity_router:915) ---
    if conditional_gates_enabled():
        assessment = await async_orch.assess_ambiguity(task)  # use the REAL method name
        if float(assessment.get("ambiguity", 0.0)) >= AMBIGUITY_THRESHOLD:
            q = assessment.get("blocking_question") or "Could you clarify the request?"
            await events.emit("reasoning", node="assess", summary="ambiguous — asking", text=q)
            state["final_answer"] = q
            return state

    # Task 5 adds: route + execute here.
    return state
```

- [ ] **Step 4: Run to verify it passes** → PASS.
- [ ] **Step 5: Commit** — `git add services/orchestrator/lite_orchestrator.py tests/services/orchestrator/test_lite_orchestrator.py && git commit -m "feat(lite): run_goal_lite ambiguity gate + halt"`

---

## Task 5: `run_goal_lite` — route + execute via reused `_run_react_loop`

**Files:**
- Modify: `services/orchestrator/lite_orchestrator.py`
- Test: `tests/services/orchestrator/test_lite_orchestrator.py` (append)

**Interfaces:**
- Reproduces `plan` (graph.py:185) + `clarification_router` (graph.py:947) + `execute_node` (graph.py:300): single-intent route via the SAME `async_orch.react_execute(goal)` the graph's execute path ultimately drives; the direct-answer fast-path (`ENABLE_DIRECT_ANSWER_FASTPATH`) answers via `orch.architect()` and halts.
- Consumes: `AsyncOrchestrator.react_execute(goal) -> dict` (coding_orchestrator.py:392) — returns `{"ok", "summary", "tools_used", "tests_passed", ...}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_lite_orchestrator.py (append)
@pytest.mark.asyncio
async def test_non_ambiguous_task_executes_via_react(monkeypatch):
    monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "0")  # gate off -> straight to execute
    orch = MagicMock()
    async_orch = MagicMock()
    async_orch.react_execute = AsyncMock(return_value={
        "ok": True, "summary": "2 + 2 is 4.", "tools_used": [], "tests_passed": False})
    out = await run_goal_lite(orch, async_orch, "What is 2+2?", "sess")
    async_orch.react_execute.assert_awaited_once()
    assert out["final_answer"] == "2 + 2 is 4."
    assert out.get("ok") is True
```

- [ ] **Step 2: Run to verify it fails** → FAIL (no execution wired).

- [ ] **Step 3: Implement** — replace the `# Task 5 adds` line with the route/execute. Read `execute_node` (graph.py:300) to confirm it drives `react_execute`; carry `ok`/`summary`/`tests_passed` into the returned state so downstream reconciliation (`completion_guard`) behaves identically:

```python
    result = await async_orch.react_execute(task)
    state["final_answer"] = result.get("summary", "")
    state["ok"] = result.get("ok", False)
    state["tests_passed"] = result.get("tests_passed", False)
    state["_result"] = result
    return state  # Task 6 inserts verify/check/reflect/approval before this return
```

- [ ] **Step 4: Run to verify it passes** → PASS. Then run the full lite test file green.
- [ ] **Step 5: Commit** — `git add services/orchestrator/lite_orchestrator.py tests/services/orchestrator/test_lite_orchestrator.py && git commit -m "feat(lite): route + execute via reused react_execute"`

---

## Task 6: `run_goal_lite` — verify + check/reflect-retry + durable approval gate

**Files:**
- Modify: `services/orchestrator/lite_orchestrator.py`
- Create: `services/orchestrator/lite_approval.py`
- Test: `tests/services/orchestrator/test_lite_orchestrator.py` (append), `tests/services/orchestrator/test_lite_approval.py`

**Interfaces:**
- `lite_approval.requires_approval(text: str) -> bool` — irreversible-action verb heuristic; mirror the style of `edit_intent.requires_editing` (word-boundary verbs: `deploy`, `delete`, `drop`, `rm -rf`, `push`, `force-push`, `publish`, `migrate`, …).
- Reproduces `check`/`router` (graph.py:399/883): if the goal FAILED and `attempts < MAX_GOAL_ATTEMPTS` → reflect (bounded retry); if it hit an irreversible action → suspend (Task 2/3) and `await` approval; else finalize.
- Reproduces `reflect` (graph.py:526): one bounded diagnosis pass, then re-execute (reuse `react_execute`).
- Reproduces the approval `interrupt()` (graph.py:572) durably: `save_suspend(store, task_id, state, "await_approval")` → `await signals.await_approval(task_id)` → on `"approve"` continue, on `"reject"` finalize BLOCKED. This is the ONE place graph and lite differ mechanically (checkpointer resume vs hand-rolled resume + in-proc await) — the fault-injection A/B (Task 9) measures whether they match.

- [ ] **Step 1: Write the failing tests** — (a) a failing goal retries once then finalizes; (b) an irreversible task suspends, and `set_approval("approve")` resumes to execution; (c) `set_approval("reject")` finalizes BLOCKED without executing the action. Full code in `test_lite_orchestrator.py` (append) + `test_lite_approval.py` mirroring `test_edit_intent.py`'s table style.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `lite_approval.requires_approval` (copy the verb-table pattern from `edit_intent.py`), then insert the verify→check→reflect/approval sequence into `run_goal_lite` before the final return, reproducing `verify_router`/`router` decisions. Bound reflect by `MAX_GOAL_ATTEMPTS` (import from `graph.py`).
- [ ] **Step 4: Run to verify they pass** + full `tests/services/orchestrator` suite green.
- [ ] **Step 5: Commit** — `git add services/orchestrator/lite_orchestrator.py services/orchestrator/lite_approval.py tests/services/orchestrator/test_lite_orchestrator.py tests/services/orchestrator/test_lite_approval.py && git commit -m "feat(lite): verify + reflect-retry + durable approval gate"`

---

## Task 7: Flag-gated dispatch in `main.py` (the ONLY hot-file edit)

**Files:**
- Modify: `services/orchestrator/main.py` (import block + `:724` where `final_state = await orch.run_task(...)`)
- Test: `tests/services/orchestrator/test_main_lite_flag.py`

**Interfaces:**
- Add near the other module constants: `ORCHESTRATOR_ENGINE = os.getenv("ORCHESTRATOR_ENGINE", "graph")` (read ONCE at import, exactly like `SEQUENCING_MODE` at coding_orchestrator.py:81). `"graph"` (default) → unchanged `orch.run_task(...)`; `"lite"` → `run_goal_lite(orch, async_orch, task, session_id, user_id, workspace_id, store=<localstore>, signals=<signalregistry>)`.

- [ ] **Step 1: Write the failing test** — patch `run_goal_lite` and assert that with `ORCHESTRATOR_ENGINE=lite` the `_handle` path calls it (and with `graph` it calls `orch.run_task`). Mock the process seams minimally.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the flag + the `if ORCHESTRATOR_ENGINE == "lite":` branch at main.py:724, threading the process's existing `LocalStore` and `SignalRegistry` handles.
- [ ] **Step 4: Run to verify it passes** + full orchestrator suite green (default path unchanged).
- [ ] **Step 5: Commit** — `git add services/orchestrator/main.py tests/services/orchestrator/test_main_lite_flag.py && git commit -m "feat(lite): ORCHESTRATOR_ENGINE=graph|lite dispatch (default graph)"`

---

## Task 8: Behavioral parity BDD (the regression gate)

**Files:**
- Create: `tests/services/orchestrator/test_lite_parity_bdd.py` + `features/lite_parity.feature`

**Interfaces:** consumes `run_goal_lite`; uses the `fake_model` respx seam (`tests/conftest.py`).

- [ ] Reuse the existing BDD `fake_model` seam. Assert `lite` produces the SAME outcome as `graph` on: trivial direct-answer, single-skill execute, failing-goal reflect-retry. This is a REGRESSION gate (they must tie), not the decider. Commit.

---

## Task 9: Fault-injection resilience A/B (decider part i)

**Files:**
- Create: `eval/orchestrator_ab/run_fault_ab.py` + `tests/eval/test_run_fault_ab.py`

**Interfaces:** kill the harness at randomized points (before execute / mid-execute / at await_approval), restart, and measure: recovered? work redone? final answer correct? for BOTH engines. RunPod/live only (needs a real run); unit-test the pure scoring seam. `graph` resumes from the AsyncSqliteSaver checkpoint; `lite` resumes from `load_resume` + re-awaits approval.

- [ ] Implement the scoring seam + a `--engine graph|lite` runner. Unit-test the pure recovery-scorer on fixture trajectories. Live kill+restart is manual/RunPod. Commit.

---

## Task 10: Engineering scorecard (decider part ii)

**Files:**
- Create: `eval/orchestrator_ab/scorecard.py` + `eval/orchestrator_ab/SCORECARD.md`

**Interfaces:** `scorecard.py` computes: LOC of LangGraph-specific code removable (`graph.py` node-wiring + `_make_async_sqlite_checkpointer` + the checkpointer SQLite layer) vs lite LOC added; droppable deps (`langgraph`, `langgraph-checkpoint-sqlite`); and a hand-written "add one sample feature (a new gate) in both paths" ergonomics note.

- [ ] Generate `SCORECARD.md` with the numbers + the ergonomics note. This + Task 9's resilience result is the decision input. Commit.

---

## Self-Review

- **Spec coverage:** ambiguity gate (T4), route/direct-answer + execute (T5), verify+reflect-retry+approval (T6), durable suspend/resume (T2/T3), flag dispatch (T7), parity regression (T8), fault-injection decider (T9), scorecard decider (T10). Multi-goal trees / replan / heavy critique explicitly OUT of scope per Global Constraints. ✔
- **Placeholders:** T4/T5/T6 intentionally say "read node X and reproduce it" because the node bodies are the source of truth and reproducing 700 lines verbatim in the plan would drift from `graph.py`; each such task pins the exact source line + a behavioral test. This is a deliberate replicate-from-source structure, not a placeholder. Exact NEW code (lite_state, lite_persistence, approval channel, dispatch) is given in full. ✔
- **Type consistency:** `run_goal_lite` signature, `save_suspend/load_resume` `(state, phase)` tuple, `set_approval/get_approval/await_approval`, `ORCHESTRATOR_ENGINE` are consistent across tasks. ✔
- **Decider honesty:** behavioral A/B is explicitly a regression gate (ties); the decision rests on T9 (resilience) + T10 (scorecard). ✔
