# Lightweight Orchestrator Spike (Collapse LangGraph) + Comparison Harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a flag-gated, branch-isolated *spike* of a plain-async-function orchestrator (`run_goal_lite`) that replicates Labmate's core goal lifecycle WITHOUT LangGraph, plus a fault-injection comparison harness — so the team can DECIDE (with data, not vibes) whether to keep the LangGraph `StateGraph` (candidate **A**) or adopt the lite loop (candidate **B**).

**Architecture:** A new module `services/orchestrator/lite_orchestrator.py` reuses the existing `architect()` ambiguity logic, `requires_editing()`/`SEQUENCING_MODE` routing, and — critically — calls the EXISTING `AsyncOrchestrator._run_react_loop` **verbatim** so the lite path inherits all 11 inner-loop harness features and any concurrent e2e fix. The one non-trivial thing LangGraph gives for free — a *durable* human-in-the-loop approval gate — is hand-coded by persisting state to Mongo and awaiting a Redis signal (mirroring the existing `events.py` cancel/steer helpers). A single flag-gated dispatch in `main.py` (`ORCHESTRATOR=graph|lite`, default `graph`) is the ONLY edit to a hot file; the LangGraph path stays byte-identical.

**Tech Stack:** Python 3.11 + asyncio, litellm (OpenAI-compatible llama.cpp seam), `redis.asyncio`, Motor (MongoDB via `StorageManager`), pytest + pytest-asyncio + pytest-bdd, respx (`fake_model`), `fakeredis.aioredis`.

---

## Honesty Note — What This Spike Is and Is Not

This is an **exploratory spike**. Its deliverable is a **decision**, not a shipped feature.

- Candidates **A** (graph + `MongoDBSaver` checkpoint) and **B** (lite loop + hand-coded checkpoint) produce **identical agent behavior** on a happy path — they call the same `_run_react_loop`, the same `architect()` ambiguity assessment, the same routing. Therefore a **behavioral A/B** (`eval/seq_ab` completion/honesty) **WILL TIE** and is explicitly **NOT the decider**. We run it only as a **regression gate** (does `lite` preserve `graph` behavior?).
- The **actual decider** is two things this plan produces:
  1. **(i) A fault-injection resilience A/B** (`eval/orchestrator_ab/`): kill the orchestrator mid-task at randomized points, restart, and measure recovery success / work redone / final-answer correctness. This is where A vs B can actually *differ* (durable graph checkpoint vs hand-rolled checkpoint + Redis-awaited approval).
  2. **(ii) An engineering scorecard** (`eval/orchestrator_ab/SCORECARD.md`): LOC delta, droppable deps (`langgraph`, `langgraph-checkpoint-mongodb`), and a "add one sample feature in both" ergonomics note.

The spike is intentionally **thin**: single-goal happy path + ambiguity gate + durable approval gate + reflect-retry. It does **NOT** reimplement replan, multi-goal trees, or the heavy critique verify-gate — those are rarely-used and not needed to compare resilience or ergonomics.

---

## Global Constraints

> Copied verbatim from CLAUDE.md and the feature brief. Every task's requirements implicitly include this section.

- **Branch isolation:** Implement on a SEPARATE branch (`feat/agentic-fix-loop` per the brief) off the latest `feat/agentic-fix-loop`. **The implementer MUST rebase onto the latest branch before starting** — live e2e may have pushed fixes to `coding_orchestrator.py` and `graph.py`.
- **Flag default `graph` → existing path byte-identical.** The ONLY edit to a hot file is the flag-gated dispatch in `main.py`. All new logic lives in NEW modules. Anchor on STRUCTURE, not line numbers — re-verify before editing.
- **REUSE, do not fork:** `run_goal_lite` MUST call `AsyncOrchestrator._run_react_loop` (via `react_execute`/the existing routing) verbatim. Do NOT reimplement the inner ReAct loop.
- **Read the flag once at startup**, process-wide, exactly like `SEQUENCING_MODE` (`os.getenv` at import / in `OrchestratorProcess.run`).
- **stdout is sacred** in any code reachable from an MCP server context — use `logging` to stderr; never `print()`.
- **asyncio correctness:** never `asyncio.run()` inside an async function; one owning task per `ClientSession` lifetime.
- **Service URLs from env only:** `MONGO_URI`, `REDIS_URL`, `GEMMA_BASE` — never hardcode.
- **llama.cpp calls:** every model call sets `extra_body={"thinking_budget_tokens": N}` and `api_key="not-needed"`. Reuse `orch.architect()` which already does this — do NOT hand-roll `litellm.acompletion`.
- **Gemma tokenizer only** (`transformers.AutoTokenizer`) — never `tiktoken`. (Not exercised here, but do not introduce it.)
- **Redis = Streams for queues** (`XADD`/`XREADGROUP`/`XACK`); the approval gate uses a plain key + poll (mirrors `events.py` cancel/steer), NOT `BRPOP`.
- **Discord connector is DEFERRED** — do NOT wire, import, or reference it.
- **Graph path keeps `AsyncMongoDBSaver`/`MongoDBSaver`** — do not remove or touch the checkpointer in the `graph` path.
- **Testing:** tests in `tests/` mirroring `services/`; `@pytest.mark.asyncio` on async tests via `asyncio_mode = auto`; assert STRUCTURE, not literal LLM text. BDD: feature → `tests/services/orchestrator/features/<slug>.feature` (`@mocked`), step defs → `test_<slug>_bdd.py`, `bdd` marker. **The BDD contract already exists — do NOT recreate `tests/conftest.py` `fake_model`.**
- **Knob defaults (new, this plan):** `ORCHESTRATOR=graph` (default), `LITE_APPROVAL_POLL_SECONDS=0.5`, `LITE_APPROVAL_TIMEOUT_SECONDS=600`, `MAX_GOAL_ATTEMPTS=2` (reused from graph), `AMBIGUITY_THRESHOLD=0.6` (reused).

---

## File Map

**New modules (all logic lives here):**

| File | Responsibility |
|---|---|
| `services/orchestrator/lite_state.py` | Pure helpers: build the initial lite state dict; a `LiteResult`-shaped finalize. No I/O. Keeps `lite_orchestrator.py` focused on flow. |
| `services/orchestrator/lite_approval.py` | Durable approval gate: `write_approval`/`read_approval`/`await_approval` Redis helpers (mirror `events.py` cancel/steer) + `requires_approval(goal)` heuristic. fakeredis-unit-testable. |
| `services/orchestrator/lite_persistence.py` | Hand-coded per-goal state persistence via `StorageManager` (`save_lite_state`/`load_lite_state`). Fake-store-unit-testable. |
| `services/orchestrator/lite_orchestrator.py` | `run_goal_lite(...)` — the plain async lifecycle: assess ambiguity → (halt on ambiguity) → route → (approval gate) → execute via `_run_react_loop` → reflect/retry → finalize. |

**Comparison harness (the decider):**

| File | Responsibility |
|---|---|
| `eval/orchestrator_ab/run_fault_ab.py` | Fault-injection runner: push a fixed task set through each `ORCHESTRATOR` mode, kill+restart the orchestrator at randomized points, record recovery metrics → `results-<mode>.json`. |
| `eval/orchestrator_ab/run_mode.sh` | Restart the orchestrator under one `ORCHESTRATOR` mode, then invoke `run_fault_ab.py`. RunPod-only path note in header. |
| `eval/orchestrator_ab/SCORECARD.md` | Generated engineering scorecard: LOC delta, droppable deps, "one sample feature in both" ergonomics note. |
| `eval/orchestrator_ab/regression_gate.md` | One-pager: how to run `eval/seq_ab` against `graph` vs `lite` as a REGRESSION gate (not the decider). |

**Hot-file edit (the ONLY one):**

| File | Edit |
|---|---|
| `services/orchestrator/main.py` | Add `ORCHESTRATOR = os.getenv("ORCHESTRATOR", "graph")` near the other module-level knobs; in `_handle`, dispatch `run_goal_lite(...)` instead of `orch.run_task(...)` when `ORCHESTRATOR == "lite"`. Default `graph` → unchanged code path. |

**Tests:**

| File | Covers |
|---|---|
| `tests/services/orchestrator/test_lite_approval.py` | Unit: approval helpers with `fakeredis.aioredis`. |
| `tests/services/orchestrator/test_lite_persistence.py` | Unit: save/load with a fake store. |
| `tests/services/orchestrator/test_lite_orchestrator.py` | Unit: `run_goal_lite` happy path / ambiguity halt / approval resume / reflect-retry (mocked orch). |
| `tests/services/orchestrator/test_main_lite_flag.py` | Unit: flag dispatch — default `graph` calls `run_task`, `lite` calls `run_goal_lite`. |
| `tests/services/orchestrator/features/lite_orchestrator.feature` | BDD (`@mocked`) Gherkin. |
| `tests/services/orchestrator/test_lite_orchestrator_bdd.py` | BDD step defs. |

---

## Behavior (BDD) — Gherkin

Full content for `tests/services/orchestrator/features/lite_orchestrator.feature`:

```gherkin
@mocked
Feature: Lightweight orchestrator runs a goal lifecycle without LangGraph
  As a maintainer evaluating whether to drop LangGraph
  I want a plain-async run_goal_lite that matches graph behavior
  So that I can compare the two on resilience and engineering cost.

  Scenario: Happy path runs a goal end-to-end and finalizes an answer
    Given an unambiguous goal "Write a python function that reverses a string"
    And the react loop will return ok "True" with summary "def reverse(s): return s[::-1]"
    When run_goal_lite executes the goal
    Then the lite result is ok
    And the final answer contains "def reverse"
    And the react loop was invoked exactly once

  Scenario: Ambiguity gate halts with a clarification and does not execute
    Given an ambiguous goal "make it better"
    And the ambiguity assessment scores "0.85" with question "What should I improve?"
    When run_goal_lite executes the goal
    Then the lite result is awaiting clarification
    And the clarification question is "What should I improve?"
    And the react loop was never invoked

  Scenario: Approval gate suspends until a Redis approve signal arrives
    Given a goal "delete the production database" requiring approval
    And the react loop will return ok "True" with summary "done"
    When run_goal_lite executes the goal and an approval signal "approve" is written
    Then the lite result is ok
    And the react loop was invoked exactly once

  Scenario: Approval gate finalizes as blocked on a deny signal
    Given a goal "delete the production database" requiring approval
    When run_goal_lite executes the goal and an approval signal "deny" is written
    Then the lite result is not ok
    And the final answer mentions the action was not approved
    And the react loop was never invoked

  Scenario: A failed goal is reflect-retried up to the attempt cap
    Given an unambiguous goal "fix the failing test in utils.py"
    And the react loop fails once then succeeds with summary "fixed"
    When run_goal_lite executes the goal
    Then the lite result is ok
    And the react loop was invoked exactly twice
    And a reflection diagnosis was produced between attempts

  Scenario: Default ORCHESTRATOR flag leaves the existing graph path untouched
    Given the ORCHESTRATOR flag is unset
    When the orchestrator chooses a goal runner
    Then it selects the graph run_task path
    And it does not call run_goal_lite
```

---

## Task 1: Lite state helpers (pure)

**Files:**
- Create: `services/orchestrator/lite_state.py`
- Test: `tests/services/orchestrator/test_lite_orchestrator.py` (state-helper tests live here too)

**Interfaces:**
- Produces:
  - `build_initial_lite_state(task: str, session_id: str, *, user_id: str = "", workspace_id: str = "") -> dict`
  - `finalize_lite(*, answer: str, ok: bool, awaiting_clarification: bool = False, clarification_question: str = "", error: str | None = None) -> dict`
  - Both return plain JSON-safe dicts shaped like the graph's `final_state` so `main._handle` post-processing (reads `final_answer`, `awaiting_clarification`, `clarification_question`, `direct_answer`, `error`) works UNCHANGED.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_lite_orchestrator.py
import pytest
from services.orchestrator.lite_state import build_initial_lite_state, finalize_lite

pytestmark = pytest.mark.mocked


def test_initial_state_is_json_safe_and_shaped_like_graph():
    s = build_initial_lite_state("do x", "sess-1", user_id="u", workspace_id="w")
    assert s["session_id"] == "sess-1"
    assert s["root_goal"] == "do x"
    assert s["current_goal_id"] == "root"
    assert s["goal_tree"]["root"]["description"] == "do x"
    assert s["final_answer"] == ""
    assert s["error"] is None
    assert s["messages"] == []


def test_finalize_ok_sets_answer_and_clears_error():
    out = finalize_lite(answer="42", ok=True)
    assert out["final_answer"] == "42"
    assert out["error"] is None
    assert out["awaiting_clarification"] is False


def test_finalize_clarification_carries_question():
    out = finalize_lite(
        answer="", ok=False, awaiting_clarification=True,
        clarification_question="What should I improve?",
    )
    assert out["awaiting_clarification"] is True
    assert out["clarification_question"] == "What should I improve?"


def test_finalize_failure_sets_error():
    out = finalize_lite(answer="partial", ok=False, error="boom")
    assert out["error"] == "boom"
    assert out["final_answer"] == "partial"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_orchestrator.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.lite_state'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/orchestrator/lite_state.py
"""Pure state helpers for the lite (non-LangGraph) orchestrator spike.

No I/O. Returns plain JSON-safe dicts shaped like the LangGraph final_state so
main._handle's post-processing (final_answer / awaiting_clarification /
clarification_question / direct_answer / error) works without changes.
"""
from __future__ import annotations

from .types import create_goal


def build_initial_lite_state(
    task: str,
    session_id: str,
    *,
    user_id: str = "",
    workspace_id: str = "",
) -> dict:
    """Mirror CodingOrchestrator.run_task's initial State, minus graph-only keys."""
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
        "awaiting_clarification": False,
        "clarification_question": "",
        "direct_answer": False,
    }


def finalize_lite(
    *,
    answer: str,
    ok: bool,
    awaiting_clarification: bool = False,
    clarification_question: str = "",
    error: str | None = None,
) -> dict:
    """Build the final_state dict main._handle consumes."""
    return {
        "final_answer": answer,
        "error": None if ok else (error or (answer or "failed")),
        "awaiting_clarification": awaiting_clarification,
        "clarification_question": clarification_question,
        "direct_answer": False,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_orchestrator.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/lite_state.py tests/services/orchestrator/test_lite_orchestrator.py
git commit -m "feat(lite): pure state helpers for the lite orchestrator spike"
```

---

## Task 2: Durable approval gate — Redis helpers + heuristic

**Files:**
- Create: `services/orchestrator/lite_approval.py`
- Test: `tests/services/orchestrator/test_lite_approval.py`

**Interfaces:**
- Consumes: `redis.asyncio.Redis` (or `fakeredis.aioredis.FakeRedis`) instance.
- Produces:
  - `APPROVAL_PREFIX = "labmate:approval:"`
  - `async def write_approval(redis, task_id: str, decision: str) -> None` — `decision` is `"approve"` or `"deny"`. Mirrors `events.write_steer` (SET with TTL).
  - `async def read_approval(redis, task_id: str) -> str | None` — GETDEL (consume-once), mirrors `events.read_and_clear_steer`.
  - `async def await_approval(redis, task_id: str, *, poll_seconds: float = 0.5, timeout_seconds: float = 600.0, sleep=asyncio.sleep) -> str` — poll until a decision appears or timeout; returns `"approve"` / `"deny"` / `"timeout"`. `sleep` injectable for tests.
  - `def requires_approval(goal: str) -> bool` — word-boundary heuristic for irreversible verbs (delete/drop/destroy/wipe/rm -rf/force-push/deploy to production). Mirrors `edit_intent.requires_editing` style.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_lite_approval.py
import asyncio
import pytest
import fakeredis.aioredis

from services.orchestrator.lite_approval import (
    write_approval, read_approval, await_approval, requires_approval, APPROVAL_PREFIX,
)

pytestmark = pytest.mark.mocked


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def test_write_then_read_is_consume_once(redis):
    await write_approval(redis, "t1", "approve")
    assert await read_approval(redis, "t1") == "approve"
    assert await read_approval(redis, "t1") is None  # GETDEL consumed it


async def test_await_returns_when_signal_appears(redis):
    async def writer():
        await asyncio.sleep(0)  # let the awaiter start polling
        await write_approval(redis, "t2", "deny")

    asyncio.create_task(writer())
    decision = await await_approval(redis, "t2", poll_seconds=0.001, timeout_seconds=5.0)
    assert decision == "deny"


async def test_await_times_out_without_signal(redis):
    ticks = {"n": 0}

    async def fast_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 50:  # safety: never hang the test
            raise RuntimeError("polled too long")

    decision = await await_approval(
        redis, "t3", poll_seconds=0.0, timeout_seconds=0.0, sleep=fast_sleep
    )
    assert decision == "timeout"


@pytest.mark.parametrize("goal,expected", [
    ("delete the production database", True),
    ("drop the users table", True),
    ("rm -rf /workspace/build", True),
    ("write a function that reverses a string", False),
    ("review utils.py for bugs", False),
])
def test_requires_approval_heuristic(goal, expected):
    assert requires_approval(goal) is expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_approval.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.lite_approval'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/orchestrator/lite_approval.py
"""Durable human-in-the-loop approval gate for the lite orchestrator.

LangGraph's interrupt() checkpoints state and suspends until the thread is
resumed. Without LangGraph we hand-roll the equivalent: persist state (see
lite_persistence) and AWAIT a Redis signal. The Redis helpers mirror the
cancel/steer pattern in events.py (SET-with-TTL + GETDEL consume-once) so the
signalling shape is consistent across the codebase. The await is a bounded poll
(NOT BRPOP — CLAUDE.md rule 5 reserves blocking ops; a key + poll is the
established cancel/steer idiom).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

import redis.asyncio as aioredis

_log = logging.getLogger("lite_approval")

APPROVAL_PREFIX = "labmate:approval:"
APPROVAL_TTL = 3600  # a stale, undrained approval self-expires


async def write_approval(redis: aioredis.Redis, task_id: str, decision: str) -> None:
    """Queue an approval decision ('approve'|'deny') for a suspended task."""
    try:
        await redis.set(f"{APPROVAL_PREFIX}{task_id}", decision, ex=APPROVAL_TTL)
    except Exception as exc:  # never let signalling break the caller
        _log.warning("write_approval failed for %s: %s", task_id, exc)


async def read_approval(redis: aioredis.Redis, task_id: str) -> str | None:
    """Atomically read AND delete the pending decision (consume-once via GETDEL)."""
    try:
        return await redis.getdel(f"{APPROVAL_PREFIX}{task_id}")
    except Exception as exc:
        _log.warning("read_approval failed for %s: %s", task_id, exc)
        return None


async def await_approval(
    redis: aioredis.Redis,
    task_id: str,
    *,
    poll_seconds: float = 0.5,
    timeout_seconds: float = 600.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Block (by polling) until an approval decision is written, or time out.

    Returns 'approve', 'deny', or 'timeout'. `sleep` is injectable so unit tests
    drive the loop deterministically without real wall-clock waits.
    """
    elapsed = 0.0
    while True:
        decision = await read_approval(redis, task_id)
        if decision in ("approve", "deny"):
            return decision
        if elapsed >= timeout_seconds:
            return "timeout"
        await sleep(poll_seconds)
        elapsed += poll_seconds if poll_seconds > 0 else timeout_seconds + 1.0


# Word-boundary irreversible-action verbs. Mirrors edit_intent.py's style.
_APPROVAL_RE = re.compile(
    r"\b(?:delete|deletes|deleting|drop|drops|dropping|destroy|destroys|"
    r"wipe|wipes|truncate|truncates|force[- ]?push|deploy(?:s|ed|ing)?)\b"
    r"|rm\s+-rf",
    re.IGNORECASE,
)


def requires_approval(goal: str) -> bool:
    """True when the goal implies an irreversible/destructive action."""
    return _APPROVAL_RE.search(goal or "") is not None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_approval.py -q`
Expected: PASS (8 passed — 3 helper tests + 5 parametrized)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/lite_approval.py tests/services/orchestrator/test_lite_approval.py
git commit -m "feat(lite): durable approval gate (Redis signal helpers + heuristic)"
```

---

## Task 3: Hand-coded state persistence via StorageManager

**Files:**
- Create: `services/orchestrator/lite_persistence.py`
- Test: `tests/services/orchestrator/test_lite_persistence.py`

**Interfaces:**
- Consumes: a store object exposing `store._db["lite_states"]` Motor-style collection with async `replace_one(filter, doc, upsert=True)` and `find_one(filter)`. In production this is `StorageManager` (`_sm._db`); tests pass a fake.
- Produces:
  - `LITE_STATE_COLLECTION = "lite_states"`
  - `async def save_lite_state(store, task_id: str, state: dict) -> None` — upsert `{ "_id": task_id, "state": <json-safe state> }`.
  - `async def load_lite_state(store, task_id: str) -> dict | None` — return the saved `state` dict or `None`.

This is the lite analogue of the graph's `MongoDBSaver` checkpoint — coarse-grained (one doc per goal) rather than per-super-step, which is exactly the durability granularity the resilience A/B will measure against the graph.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_lite_persistence.py
import pytest
from services.orchestrator.lite_persistence import (
    save_lite_state, load_lite_state, LITE_STATE_COLLECTION,
)

pytestmark = pytest.mark.mocked


class _FakeCollection:
    def __init__(self):
        self.docs = {}

    async def replace_one(self, flt, doc, upsert=False):
        self.docs[flt["_id"]] = doc

    async def find_one(self, flt):
        return self.docs.get(flt["_id"])


class _FakeStore:
    def __init__(self):
        self._db = {LITE_STATE_COLLECTION: _FakeCollection()}


async def test_save_then_load_roundtrips_state():
    store = _FakeStore()
    state = {"root_goal": "x", "current_goal_id": "root", "messages": []}
    await save_lite_state(store, "task-1", state)
    loaded = await load_lite_state(store, "task-1")
    assert loaded == state


async def test_load_missing_returns_none():
    store = _FakeStore()
    assert await load_lite_state(store, "nope") is None


async def test_save_is_idempotent_upsert():
    store = _FakeStore()
    await save_lite_state(store, "task-2", {"v": 1})
    await save_lite_state(store, "task-2", {"v": 2})
    assert (await load_lite_state(store, "task-2")) == {"v": 2}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_persistence.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.lite_persistence'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/orchestrator/lite_persistence.py
"""Hand-coded per-goal state persistence for the lite orchestrator.

The lite analogue of LangGraph's MongoDBSaver checkpoint: one durable doc per
goal in the `lite_states` collection. Coarser than per-super-step graph
checkpointing — that granularity difference is precisely what the
fault-injection A/B (eval/orchestrator_ab) measures A vs B on.

`store` is duck-typed: anything with `store._db[COLLECTION]` exposing async
replace_one / find_one. In production it is StorageManager (_sm); tests pass a
fake. Best-effort: a persistence failure logs and continues (the goal still
runs; only crash-recovery is degraded), mirroring the rest of the orchestrator's
best-effort I/O.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("lite_persistence")

LITE_STATE_COLLECTION = "lite_states"


async def save_lite_state(store, task_id: str, state: dict) -> None:
    try:
        await store._db[LITE_STATE_COLLECTION].replace_one(
            {"_id": task_id}, {"_id": task_id, "state": state}, upsert=True,
        )
    except Exception as exc:
        _log.warning("save_lite_state failed for %s: %s", task_id, exc)


async def load_lite_state(store, task_id: str) -> dict | None:
    try:
        doc = await store._db[LITE_STATE_COLLECTION].find_one({"_id": task_id})
    except Exception as exc:
        _log.warning("load_lite_state failed for %s: %s", task_id, exc)
        return None
    if not doc:
        return None
    return doc.get("state")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_persistence.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/lite_persistence.py tests/services/orchestrator/test_lite_persistence.py
git commit -m "feat(lite): hand-coded per-goal state persistence via StorageManager"
```

---

## Task 4: `run_goal_lite` — assess ambiguity + halt

**Files:**
- Create: `services/orchestrator/lite_orchestrator.py`
- Test: `tests/services/orchestrator/test_lite_orchestrator.py` (append)

**Interfaces:**
- Consumes: `lite_state.build_initial_lite_state`, `lite_state.finalize_lite`; `orch.architect` (the EXISTING ambiguity prompt logic — reuse, do not duplicate the prompt text); `AMBIGUITY_THRESHOLD` from `graph`.
- Produces (final signature, fully built by Task 6 — earlier tasks return partial behavior):

```python
async def run_goal_lite(
    *,
    orch: "CodingOrchestrator",
    async_orch: "AsyncOrchestrator",
    task: str,
    task_id: str,
    session_id: str,
    user_id: str = "",
    workspace_id: str = "",
    redis=None,
    store=None,
    max_attempts: int | None = None,
) -> dict:
```

This task implements ONLY the assess-ambiguity-and-halt branch; routing/execute/approval/retry land in Tasks 5–6. Reuse the graph's ambiguity prompt by importing a small extracted helper rather than copy-pasting (see Step 3 — we add `assess_ambiguity_lite` that calls `orch.architect` with the SAME prompt builder the graph node uses).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_lite_orchestrator.py  (append)
from unittest.mock import AsyncMock, MagicMock
import json as _json

from services.orchestrator.lite_orchestrator import run_goal_lite
from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator


def _mock_orch(architect_return):
    orch = MagicMock(spec=CodingOrchestrator)
    orch.architect = AsyncMock(return_value=architect_return)
    return orch


async def test_ambiguous_goal_halts_with_clarification():
    orch = _mock_orch(_json.dumps(
        {"assumptions": [], "ambiguity": 0.85, "blocking_question": "What should I improve?"}
    ))
    async_orch = MagicMock(spec=AsyncOrchestrator)
    async_orch.react_execute = AsyncMock()

    out = await run_goal_lite(
        orch=orch, async_orch=async_orch,
        task="make it better", task_id="t", session_id="s",
    )
    assert out["awaiting_clarification"] is True
    assert out["clarification_question"] == "What should I improve?"
    async_orch.react_execute.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_orchestrator.py::test_ambiguous_goal_halts_with_clarification -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.lite_orchestrator'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/orchestrator/lite_orchestrator.py
"""Lightweight (non-LangGraph) orchestrator — SPIKE.

A plain async function replicating the LOAD-BEARING goal lifecycle from graph.py
WITHOUT a StateGraph: assess ambiguity -> (halt) -> route -> (durable approval
gate) -> EXECUTE via the EXISTING AsyncOrchestrator._run_react_loop (reused
verbatim through react_execute, so it inherits all inner-loop harness features)
-> reflect/retry -> finalize.

DELIBERATELY THIN: single-goal happy path + ambiguity gate + approval gate +
reflect-retry only. Replan, multi-goal trees, and the heavy critique verify-gate
are out of scope — the spike exists to COMPARE A (graph) vs B (lite) on
resilience + engineering cost, not to ship a full replacement.

stdout is sacred: log to stderr only.
"""
from __future__ import annotations

import json
import logging

from . import events
from .graph import AMBIGUITY_THRESHOLD, ASSESS_THINKING_BUDGET, MAX_GOAL_ATTEMPTS
from .lite_state import build_initial_lite_state, finalize_lite

_log = logging.getLogger("lite_orchestrator")


def _build_ambiguity_prompt(goal: str) -> str:
    """Same triage prompt the graph's assess_ambiguity node uses.

    Kept as a thin builder so the lite path reuses the SAME wording (and thus the
    same model behavior) as the graph path — the two must be behaviorally
    identical for the regression gate to be meaningful.
    """
    return (
        "You are triaging a task before an autonomous agent executes it.\n"
        f"TASK: {goal}\n\n"
        "List the assumptions an agent must make to act on this as written. "
        "Then rate overall ambiguity from 0.0 (fully specified) to 1.0 "
        "(critically underspecified). The score measures ONE thing: is the CORE "
        "objective underspecified (undefined referent, no concrete deliverable, "
        "undefined success criteria, or essential info missing)? Unstated MINOR "
        "parameters (format, count, library, styling) are NOT ambiguity.\n"
        "When ambiguity is high, set \"blocking_question\" to the single most "
        "useful question; otherwise leave it empty.\n"
        'Respond as JSON: {"assumptions": ["..."], "ambiguity": 0.0, '
        '"blocking_question": "" }'
    )


def _parse_ambiguity(raw: str) -> tuple[float, str]:
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    try:
        out = json.loads(text.strip())
        if not isinstance(out, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return 0.0, ""
    try:
        amb = float(out.get("ambiguity", 0.0))
    except (TypeError, ValueError):
        amb = 0.0
    return amb, str(out.get("blocking_question", "") or "")


async def run_goal_lite(
    *,
    orch,
    async_orch,
    task: str,
    task_id: str,
    session_id: str,
    user_id: str = "",
    workspace_id: str = "",
    redis=None,
    store=None,
    max_attempts: int | None = None,
) -> dict:
    state = build_initial_lite_state(
        task, session_id, user_id=user_id, workspace_id=workspace_id,
    )

    # 1. Ambiguity gate (reuses orch.architect + the graph's triage prompt).
    raw = await orch.architect(
        _build_ambiguity_prompt(task), thinking_budget=ASSESS_THINKING_BUDGET,
    )
    ambiguity, question = _parse_ambiguity(raw)
    if ambiguity >= AMBIGUITY_THRESHOLD:
        question = question or "Could you clarify what you'd like me to do?"
        await events.emit(
            "clarification_request", question=question, task=task, session_id=session_id,
        )
        return finalize_lite(
            answer="", ok=False, awaiting_clarification=True,
            clarification_question=question,
        )

    # Routing / execute / approval / retry land in Tasks 5-6.
    raise NotImplementedError("execute path implemented in Tasks 5-6")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_orchestrator.py::test_ambiguous_goal_halts_with_clarification -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/lite_orchestrator.py tests/services/orchestrator/test_lite_orchestrator.py
git commit -m "feat(lite): run_goal_lite ambiguity gate (halts with clarification)"
```

---

## Task 5: `run_goal_lite` — route + execute via reused `_run_react_loop`

**Files:**
- Modify: `services/orchestrator/lite_orchestrator.py` (replace the `NotImplementedError` block)
- Test: `tests/services/orchestrator/test_lite_orchestrator.py` (append)

**Interfaces:**
- Consumes: `async_orch.react_execute(goal)` — the EXISTING dispatcher that routes to `_run_skill_first` / `_run_react_loop` per `SEQUENCING_MODE` and `requires_editing`. **The lite path calls `react_execute` so it inherits the inner loop VERBATIM** (do not call `_run_react_loop` directly — `react_execute` owns the activation-budget reset and the routing).
- Produces: on a non-ambiguous, non-approval goal, `run_goal_lite` executes once via `react_execute` and finalizes `ok`/answer from the returned `{"ok", "summary"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_lite_orchestrator.py  (append)
async def test_happy_path_executes_once_and_finalizes():
    orch = _mock_orch(_json.dumps({"ambiguity": 0.1, "blocking_question": ""}))
    async_orch = MagicMock(spec=AsyncOrchestrator)
    async_orch.react_execute = AsyncMock(
        return_value={"ok": True, "summary": "def reverse(s): return s[::-1]"}
    )

    out = await run_goal_lite(
        orch=orch, async_orch=async_orch,
        task="write a python function that reverses a string",
        task_id="t", session_id="s",
    )
    assert out["error"] is None
    assert "def reverse" in out["final_answer"]
    assert async_orch.react_execute.await_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_orchestrator.py::test_happy_path_executes_once_and_finalizes -q`
Expected: FAIL — `NotImplementedError: execute path implemented in Tasks 5-6`

- [ ] **Step 3: Write minimal implementation**

Replace the `raise NotImplementedError(...)` line with:

```python
    # 2. Route + execute. react_execute is the EXISTING dispatcher: it resets the
    #    per-goal activation budget and routes to skill-first / ReAct per
    #    SEQUENCING_MODE + requires_editing. Calling it here means the lite path
    #    REUSES _run_react_loop verbatim (all 11 inner-loop features + any e2e fix).
    result = await async_orch.react_execute(task)
    ok = bool(result.get("ok"))
    answer = str(result.get("summary", "") or "")
    return finalize_lite(answer=answer, ok=ok)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_orchestrator.py -q`
Expected: PASS (all tests in file)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/lite_orchestrator.py tests/services/orchestrator/test_lite_orchestrator.py
git commit -m "feat(lite): route+execute via reused react_execute/_run_react_loop"
```

---

## Task 6: `run_goal_lite` — durable approval gate + reflect-retry

**Files:**
- Modify: `services/orchestrator/lite_orchestrator.py`
- Test: `tests/services/orchestrator/test_lite_orchestrator.py` (append)

**Interfaces:**
- Consumes: `lite_approval.requires_approval`, `lite_approval.await_approval`; `lite_persistence.save_lite_state`; `orch.architect` (for the reflect diagnosis, reusing `REFLECT_THINKING_BUDGET`).
- Produces: the FINAL `run_goal_lite` behavior:
  1. ambiguity halt (Task 4)
  2. if `requires_approval(task)` AND `redis` present → `save_lite_state` (durable suspend) → `await_approval`; `deny`/`timeout` → finalize blocked (not ok), never execute; `approve` → continue.
  3. execute via `react_execute` (Task 5)
  4. on failure, reflect via `orch.architect` + retry up to `max_attempts` (default `MAX_GOAL_ATTEMPTS`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_lite_orchestrator.py  (append)
import asyncio
import fakeredis.aioredis
from services.orchestrator.lite_approval import write_approval


async def test_approval_deny_finalizes_blocked_without_executing():
    orch = _mock_orch(_json.dumps({"ambiguity": 0.1}))
    async_orch = MagicMock(spec=AsyncOrchestrator)
    async_orch.react_execute = AsyncMock(return_value={"ok": True, "summary": "done"})
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_approval(redis, "t", "deny")

    out = await run_goal_lite(
        orch=orch, async_orch=async_orch,
        task="delete the production database", task_id="t", session_id="s",
        redis=redis,
    )
    assert out["error"] is not None
    assert "approv" in out["final_answer"].lower()
    async_orch.react_execute.assert_not_called()


async def test_approval_approve_resumes_and_executes():
    orch = _mock_orch(_json.dumps({"ambiguity": 0.1}))
    async_orch = MagicMock(spec=AsyncOrchestrator)
    async_orch.react_execute = AsyncMock(return_value={"ok": True, "summary": "done"})
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def approve_later():
        await asyncio.sleep(0)
        await write_approval(redis, "t", "approve")

    asyncio.create_task(approve_later())
    out = await run_goal_lite(
        orch=orch, async_orch=async_orch,
        task="delete the production database", task_id="t", session_id="s",
        redis=redis,
    )
    assert out["error"] is None
    assert async_orch.react_execute.await_count == 1


async def test_failed_goal_is_reflect_retried_to_cap():
    orch = _mock_orch(_json.dumps({"ambiguity": 0.1}))
    orch.architect = AsyncMock(side_effect=[
        _json.dumps({"ambiguity": 0.1}),  # ambiguity call
        "DIAGNOSIS: try converting to int",  # reflect call between attempts
    ])
    async_orch = MagicMock(spec=AsyncOrchestrator)
    async_orch.react_execute = AsyncMock(side_effect=[
        {"ok": False, "summary": "AssertionError"},
        {"ok": True, "summary": "fixed"},
    ])

    out = await run_goal_lite(
        orch=orch, async_orch=async_orch,
        task="fix the failing test in utils.py", task_id="t", session_id="s",
        max_attempts=2,
    )
    assert out["error"] is None
    assert out["final_answer"] == "fixed"
    assert async_orch.react_execute.await_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_orchestrator.py -q -k "approval or reflect"`
Expected: FAIL — approval never gates; failed goal not retried (`react_execute` called once).

- [ ] **Step 3: Write minimal implementation**

Add imports at the top of `lite_orchestrator.py`:

```python
from .graph import REFLECT_THINKING_BUDGET
from .lite_approval import requires_approval, await_approval
from .lite_persistence import save_lite_state
```

Replace the Task-5 block (`# 2. Route + execute. ...` through `return finalize_lite(answer=answer, ok=ok)`) with:

```python
    # 2. Durable approval gate (the one non-trivial thing LangGraph gives free).
    #    Persist state (durable suspend) then AWAIT a Redis signal — no interrupt().
    if requires_approval(task) and redis is not None:
        if store is not None:
            await save_lite_state(store, task_id, state)
        await events.emit(
            "reasoning", node="approval",
            summary="awaiting approval for an irreversible action", text=task[:200],
        )
        decision = await await_approval(redis, task_id)
        if decision != "approve":
            return finalize_lite(
                answer="Action was not approved — halting before the irreversible step.",
                ok=False, error=f"approval_{decision}",
            )

    # 3. Route + execute via the EXISTING dispatcher, with reflect-retry.
    cap = MAX_GOAL_ATTEMPTS if max_attempts is None else max_attempts
    attempt = 0
    last = {"ok": False, "summary": ""}
    while attempt < cap:
        attempt += 1
        last = await async_orch.react_execute(task)
        if bool(last.get("ok")):
            return finalize_lite(answer=str(last.get("summary", "") or ""), ok=True)
        if attempt >= cap:
            break
        # 4. Reflect between attempts (reuse orch.architect + graph's budget).
        diagnosis = await orch.architect(
            "The following task failed (attempt "
            f"{attempt}):\nTask: {task}\nError: {last.get('summary', '')}\n"
            "Write a concise diagnosis and what to do differently next attempt.",
            thinking_budget=REFLECT_THINKING_BUDGET,
        )
        state.setdefault("messages", []).append(
            {"role": "reflection", "goal_id": "root", "content": diagnosis}
        )
        await events.emit(
            "reasoning", node="reflect", summary="diagnosing failed goal",
            text=(diagnosis or "")[:500],
        )

    return finalize_lite(
        answer=str(last.get("summary", "") or "failed"),
        ok=False, error=str(last.get("summary", "") or "failed"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_orchestrator.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/lite_orchestrator.py tests/services/orchestrator/test_lite_orchestrator.py
git commit -m "feat(lite): durable approval gate + reflect-retry in run_goal_lite"
```

---

## Task 7: Flag-gated dispatch in `main.py` (the ONLY hot-file edit)

**Files:**
- Modify: `services/orchestrator/main.py` (add module-level `ORCHESTRATOR`; branch in `_handle`)
- Test: `tests/services/orchestrator/test_main_lite_flag.py`

**Interfaces:**
- Consumes: `lite_orchestrator.run_goal_lite`; the existing `orch`, `async_orch`, `self._redis`, `storage` already in `_handle`'s scope.
- Produces: `ORCHESTRATOR = os.getenv("ORCHESTRATOR", "graph")` at module level; a `_run_goal(...)` helper on `OrchestratorProcess` that dispatches on the flag, so the test can assert dispatch without spinning the full loop.

> **Anchor on STRUCTURE, re-verify before editing.** Insert `ORCHESTRATOR` next to `GOALS_STREAM`/`RESULT_PREFIX` constants (top of `main.py`). The `_handle` call site is `final_state = await orch.run_task(task_text, session_id, ...)` — keep its post-processing UNCHANGED; only swap which coroutine produces `final_state`. `async_orch` is constructed in `OrchestratorProcess.run`; store it as `self._async_orch` there so `_run_goal` can reach it (one extra assignment, no behavior change for `graph`).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_main_lite_flag.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from services.orchestrator.main import OrchestratorProcess

pytestmark = pytest.mark.mocked


async def _dispatch(monkeypatch, mode):
    monkeypatch.setattr("services.orchestrator.main.ORCHESTRATOR", mode, raising=False)
    proc = OrchestratorProcess()
    proc._async_orch = MagicMock()
    proc._redis = MagicMock()
    orch = MagicMock()
    orch.run_task = AsyncMock(return_value={"final_answer": "graph", "error": None})
    storage = MagicMock()
    with patch(
        "services.orchestrator.main.run_goal_lite",
        new=AsyncMock(return_value={"final_answer": "lite", "error": None}),
    ) as lite:
        out = await proc._run_goal(
            orch=orch, storage=storage, task_text="do x",
            task_id="t", session_id="s", user_id="", workspace_id="",
            agent_instructions="",
        )
    return orch.run_task, lite, out


async def test_default_uses_graph_run_task(monkeypatch):
    run_task, lite, out = await _dispatch(monkeypatch, "graph")
    run_task.assert_awaited_once()
    lite.assert_not_called()
    assert out["final_answer"] == "graph"


async def test_lite_flag_uses_run_goal_lite(monkeypatch):
    run_task, lite, out = await _dispatch(monkeypatch, "lite")
    lite.assert_awaited_once()
    run_task.assert_not_called()
    assert out["final_answer"] == "lite"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_main_lite_flag.py -q`
Expected: FAIL — `AttributeError: 'OrchestratorProcess' object has no attribute '_run_goal'`

- [ ] **Step 3: Write minimal implementation**

In `main.py`, add the import near the other orchestrator imports:

```python
from services.orchestrator.lite_orchestrator import run_goal_lite
```

Add the constant beside `GOALS_STREAM` / `RESULT_PREFIX`:

```python
# Orchestrator engine selector (read ONCE at import, like SEQUENCING_MODE):
#   "graph" (DEFAULT) -> the LangGraph StateGraph path (orch.run_task), UNCHANGED.
#   "lite"            -> the plain-async spike (run_goal_lite). For the A vs B
#                        comparison harness (eval/orchestrator_ab). See
#                        docs/superpowers/plans/2026-06-26-lite-orchestrator-spike.md
ORCHESTRATOR = os.getenv("ORCHESTRATOR", "graph")
```

In `OrchestratorProcess.run`, right after `async_orch = AsyncOrchestrator(...)` is constructed, add:

```python
            self._async_orch = async_orch
```

Add the `_run_goal` dispatcher method to `OrchestratorProcess` (e.g. just above `_handle`):

```python
    async def _run_goal(
        self,
        *,
        orch: "CodingOrchestrator",
        storage: "StorageManager",
        task_text: str,
        task_id: str,
        session_id: str,
        user_id: str,
        workspace_id: str,
        agent_instructions: str,
    ) -> dict:
        """Dispatch the goal to the selected engine. Default 'graph' is the
        existing LangGraph path (byte-identical). 'lite' runs the spike."""
        if ORCHESTRATOR == "lite":
            return await run_goal_lite(
                orch=orch,
                async_orch=self._async_orch,
                task=task_text,
                task_id=task_id,
                session_id=session_id,
                user_id=user_id,
                workspace_id=workspace_id,
                redis=self._redis,
                store=storage,
            )
        return await orch.run_task(
            task_text, session_id, user_id=user_id,
            workspace_id=workspace_id, agent_instructions=agent_instructions,
        )
```

In `_handle`, replace ONLY this call:

```python
            final_state = await orch.run_task(
                task_text, session_id, user_id=user_id, workspace_id=workspace_id,
                agent_instructions=agent_instructions,
            )
```

with:

```python
            final_state = await self._run_goal(
                orch=orch, storage=storage, task_text=task_text, task_id=task_id,
                session_id=session_id, user_id=user_id, workspace_id=workspace_id,
                agent_instructions=agent_instructions,
            )
```

Also add `self._async_orch = None` to `OrchestratorProcess.__init__` next to the other `self._...: ... = None` attributes (so the attribute always exists).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_main_lite_flag.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the existing main/graph regression tests (default path unchanged)**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_main.py tests/services/orchestrator/test_graph.py -q`
Expected: PASS (no regressions — default `ORCHESTRATOR=graph` is byte-identical at the call site)

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/main.py tests/services/orchestrator/test_main_lite_flag.py
git commit -m "feat(lite): flag-gated ORCHESTRATOR dispatch in main (default graph)"
```

---

## Task 8: BDD feature + step defs for `run_goal_lite`

**Files:**
- Create: `tests/services/orchestrator/features/lite_orchestrator.feature` (content in the "Behavior (BDD)" section above)
- Create: `tests/services/orchestrator/test_lite_orchestrator_bdd.py`

**Interfaces:**
- Consumes: `run_goal_lite`, `lite_approval.write_approval`, `fake_model` is NOT needed here (we mock `orch`/`async_orch` directly, matching the unit style of `test_stateful_reflection_bdd.py`); `run_async` from `tests.conftest`; `fakeredis.aioredis`.
- Produces: passing `@mocked @bdd` scenarios mapping 1:1 to the feature file.

- [ ] **Step 1: Write the feature file**

Create `tests/services/orchestrator/features/lite_orchestrator.feature` with the EXACT content from the "Behavior (BDD) — Gherkin" section above.

- [ ] **Step 2: Write the step defs**

```python
# tests/services/orchestrator/test_lite_orchestrator_bdd.py
"""pytest-bdd step defs for lite_orchestrator.feature."""
from __future__ import annotations

import asyncio
import json
import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
from services.orchestrator.lite_orchestrator import run_goal_lite
from services.orchestrator.lite_approval import write_approval
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/lite_orchestrator.feature")


@pytest.fixture
def ctx():
    return {
        "task": "",
        "amb_json": json.dumps({"ambiguity": 0.1, "blocking_question": ""}),
        "react_results": [{"ok": True, "summary": "ok"}],
        "redis": fakeredis.aioredis.FakeRedis(decode_responses=True),
        "approval": None,
        "out": None,
        "react_mock": None,
        "architect_mock": None,
        "selected_runner": None,
        "flag_unset": False,
    }


def _run(ctx):
    orch = MagicMock(spec=CodingOrchestrator)
    # First architect() call is the ambiguity assessment; later calls are reflect.
    orch.architect = AsyncMock(side_effect=[ctx["amb_json"], "DIAGNOSIS"])
    ctx["architect_mock"] = orch.architect
    async_orch = MagicMock(spec=AsyncOrchestrator)
    async_orch.react_execute = AsyncMock(side_effect=list(ctx["react_results"]))
    ctx["react_mock"] = async_orch.react_execute

    async def _go():
        if ctx["approval"] is not None:
            await write_approval(ctx["redis"], "t", ctx["approval"])
        return await run_goal_lite(
            orch=orch, async_orch=async_orch, task=ctx["task"],
            task_id="t", session_id="s", redis=ctx["redis"], max_attempts=2,
        )

    ctx["out"] = run_async(_go())


# ---- Given ----------------------------------------------------------------- #

@given(parsers.parse('an unambiguous goal "{task}"'))
def unambiguous_goal(ctx, task):
    ctx["task"] = task
    ctx["amb_json"] = json.dumps({"ambiguity": 0.1, "blocking_question": ""})


@given(parsers.parse('an ambiguous goal "{task}"'))
def ambiguous_goal(ctx, task):
    ctx["task"] = task


@given(parsers.parse('the ambiguity assessment scores "{score}" with question "{q}"'))
def ambiguity_scores(ctx, score, q):
    ctx["amb_json"] = json.dumps({"ambiguity": float(score), "blocking_question": q})


@given(parsers.parse('a goal "{task}" requiring approval'))
def goal_requiring_approval(ctx, task):
    ctx["task"] = task


@given(parsers.parse('the react loop will return ok "{ok}" with summary "{summary}"'))
def react_returns(ctx, ok, summary):
    ctx["react_results"] = [{"ok": ok == "True", "summary": summary}]


@given(parsers.parse('the react loop fails once then succeeds with summary "{summary}"'))
def react_fails_then_succeeds(ctx, summary):
    ctx["react_results"] = [
        {"ok": False, "summary": "AssertionError"},
        {"ok": True, "summary": summary},
    ]


@given("the ORCHESTRATOR flag is unset")
def flag_unset(ctx):
    ctx["flag_unset"] = True


# ---- When ------------------------------------------------------------------ #

@when("run_goal_lite executes the goal")
def execute_goal(ctx):
    _run(ctx)


@when(parsers.parse('run_goal_lite executes the goal and an approval signal "{decision}" is written'))
def execute_with_approval(ctx, decision):
    ctx["approval"] = decision
    _run(ctx)


@when("the orchestrator chooses a goal runner")
def choose_runner(ctx):
    import services.orchestrator.main as m
    ctx["selected_runner"] = "graph" if m.ORCHESTRATOR == "graph" else "lite"


# ---- Then ------------------------------------------------------------------ #

@then("the lite result is ok")
def result_ok(ctx):
    assert ctx["out"]["error"] is None


@then("the lite result is not ok")
def result_not_ok(ctx):
    assert ctx["out"]["error"] is not None


@then("the lite result is awaiting clarification")
def result_awaiting(ctx):
    assert ctx["out"]["awaiting_clarification"] is True


@then(parsers.parse('the clarification question is "{q}"'))
def clarification_is(ctx, q):
    assert ctx["out"]["clarification_question"] == q


@then(parsers.parse('the final answer contains "{text}"'))
def answer_contains(ctx, text):
    assert text in ctx["out"]["final_answer"]


@then("the final answer mentions the action was not approved")
def answer_not_approved(ctx):
    assert "approv" in ctx["out"]["final_answer"].lower()


@then("the react loop was invoked exactly once")
def react_once(ctx):
    assert ctx["react_mock"].await_count == 1


@then("the react loop was invoked exactly twice")
def react_twice(ctx):
    assert ctx["react_mock"].await_count == 2


@then("the react loop was never invoked")
def react_never(ctx):
    ctx["react_mock"].assert_not_called()


@then("a reflection diagnosis was produced between attempts")
def reflection_produced(ctx):
    # architect() called twice: ambiguity assessment + one reflect diagnosis.
    assert ctx["architect_mock"].await_count == 2


@then("it selects the graph run_task path")
def selects_graph(ctx):
    assert ctx["selected_runner"] == "graph"


@then("it does not call run_goal_lite")
def not_lite(ctx):
    assert ctx["selected_runner"] != "lite"
```

- [ ] **Step 3: Run the BDD scenarios to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/services/orchestrator/test_lite_orchestrator_bdd.py -q`
Expected: PASS (6 scenarios passed)

> Note: the "Default ORCHESTRATOR flag" scenario asserts `main.ORCHESTRATOR == "graph"` (the import-time default). It does not set the env var, so it validates the shipped default. Run this file with `ORCHESTRATOR` UNSET in the environment.

- [ ] **Step 4: Commit**

```bash
git add tests/services/orchestrator/features/lite_orchestrator.feature tests/services/orchestrator/test_lite_orchestrator_bdd.py
git commit -m "test(lite): BDD scenarios for run_goal_lite lifecycle"
```

---

## Task 9: Fault-injection comparison harness (the decider, part i)

**Files:**
- Create: `eval/orchestrator_ab/run_fault_ab.py`
- Create: `eval/orchestrator_ab/run_mode.sh`
- Test: `tests/eval/test_run_fault_ab.py`

**Interfaces:**
- Produces:
  - `FAULT_TASKS: list[dict]` — a fixed task set (each `{"id", "task", "expects_substr"}`): a happy compute, an edit/fix goal, an approval-gated goal.
  - `def choose_kill_point(rng, *, min_s: float, max_s: float) -> float` — randomized kill delay (pure; seedable).
  - `async def run_one(redis, task: dict, *, kill_after_s: float, restart, push, poll) -> dict` — push the task, wait `kill_after_s`, invoke `restart()` (kills + restarts the orchestrator process), then poll for the result; returns `{"id", "recovered": bool, "redone_turns": int, "correct": bool, "wall_s": float}`.
  - `async def run_suite(mode: str, ...) -> dict` — run all tasks, aggregate `{"mode", "recovery_rate", "avg_redone_turns", "correctness_rate", "cases": [...]}` → write `results-<mode>.json`.

The harness is the ONLY place A vs B can differ: under a mid-task kill, the `graph` path resumes from the `MongoDBSaver` checkpoint; the `lite` path resumes from `lite_persistence` + re-awaits the Redis approval. Unit tests cover the pure/seam logic; the live kill+restart is driven by `run_mode.sh` on RunPod.

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/test_run_fault_ab.py
import random
import pytest
from eval.orchestrator_ab.run_fault_ab import (
    choose_kill_point, run_one, FAULT_TASKS,
)

pytestmark = pytest.mark.mocked


def test_kill_point_is_within_bounds_and_seedable():
    rng = random.Random(42)
    p1 = choose_kill_point(rng, min_s=1.0, max_s=5.0)
    assert 1.0 <= p1 <= 5.0
    rng2 = random.Random(42)
    assert choose_kill_point(rng2, min_s=1.0, max_s=5.0) == p1  # deterministic


def test_fault_tasks_cover_happy_edit_and_approval():
    kinds = {t["id"] for t in FAULT_TASKS}
    assert {"happy", "edit", "approval"} <= kinds


async def test_run_one_records_recovery_when_result_appears():
    pushed = {}
    restarts = {"n": 0}

    async def push(task):
        pushed["task"] = task

    async def restart():
        restarts["n"] += 1

    async def poll(task_id, timeout):
        return {"ok": True, "state": {"final_answer": "def reverse(s)"}}

    async def fast_sleep(_s):
        return None

    out = await run_one(
        redis=None,
        task={"id": "happy", "task": "reverse a string", "expects_substr": "reverse"},
        kill_after_s=0.0, restart=restart, push=push, poll=poll, sleep=fast_sleep,
    )
    assert out["recovered"] is True
    assert out["correct"] is True
    assert restarts["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/eval/test_run_fault_ab.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.orchestrator_ab.run_fault_ab'`

- [ ] **Step 3: Write minimal implementation**

```python
# eval/orchestrator_ab/run_fault_ab.py
"""Fault-injection resilience A/B for the orchestrator engine (graph vs lite).

THE DECIDER (part i). A behavioral A/B (eval/seq_ab) TIES because both engines
call the same _run_react_loop. This harness instead KILLS the orchestrator
mid-task at randomized points, RESTARTS it, and measures whether the engine
RECOVERS — which is exactly where candidate A (graph + MongoDBSaver checkpoint)
and candidate B (lite + hand-coded checkpoint + Redis-awaited approval) can
DIFFER. Run once per ORCHESTRATOR mode via run_mode.sh.

stdout-sacred does NOT apply here (this is a standalone eval script, not an MCP
server) — but we still log to stderr for consistency.
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Awaitable, Callable

FAULT_TASKS: list[dict] = [
    {"id": "happy", "task": "Write a python function that reverses a string.",
     "expects_substr": "reverse"},
    {"id": "edit", "task": "Fix the failing test in /workspace/ab_buggy.py.",
     "expects_substr": ""},
    {"id": "approval", "task": "Delete the temporary file /workspace/ab_scratch.txt.",
     "expects_substr": ""},
]


def choose_kill_point(rng: random.Random, *, min_s: float, max_s: float) -> float:
    """Deterministic (seedable) randomized kill delay in [min_s, max_s]."""
    return min_s + (max_s - min_s) * rng.random()


async def run_one(
    *,
    redis,
    task: dict,
    kill_after_s: float,
    restart: Callable[[], Awaitable[None]],
    push: Callable[[dict], Awaitable[None]],
    poll: Callable[[str, float], Awaitable[dict | None]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_timeout_s: float = 300.0,
) -> dict:
    """Push a task, kill+restart mid-flight, then poll for recovery."""
    import time

    t0 = time.monotonic()
    await push(task)
    await sleep(kill_after_s)
    await restart()
    result = await poll(task["id"], poll_timeout_s)
    wall_s = time.monotonic() - t0

    recovered = bool(result and result.get("ok"))
    answer = ""
    if isinstance(result, dict):
        answer = (result.get("state") or {}).get("final_answer", "") or ""
    expects = task.get("expects_substr", "")
    correct = recovered and (expects == "" or expects.lower() in answer.lower())

    return {
        "id": task["id"],
        "recovered": recovered,
        "redone_turns": int((result or {}).get("redone_turns", 0)),
        "correct": correct,
        "wall_s": round(wall_s, 2),
    }


async def run_suite(
    *,
    mode: str,
    redis,
    restart: Callable[[], Awaitable[None]],
    push: Callable[[dict], Awaitable[None]],
    poll: Callable[[str, float], Awaitable[dict | None]],
    seed: int = 7,
    out_dir: str | Path = "eval/orchestrator_ab",
) -> dict:
    rng = random.Random(seed)
    cases = []
    for task in FAULT_TASKS:
        kill_after = choose_kill_point(rng, min_s=2.0, max_s=8.0)
        cases.append(await run_one(
            redis=redis, task=task, kill_after_s=kill_after,
            restart=restart, push=push, poll=poll,
        ))
    n = len(cases) or 1
    summary = {
        "mode": mode,
        "recovery_rate": round(sum(c["recovered"] for c in cases) / n, 3),
        "avg_redone_turns": round(sum(c["redone_turns"] for c in cases) / n, 3),
        "correctness_rate": round(sum(c["correct"] for c in cases) / n, 3),
        "cases": cases,
    }
    out_path = Path(out_dir) / f"results-{mode}.json"
    out_path.write_text(json.dumps(summary, indent=2))
    return summary
```

Create `eval/orchestrator_ab/run_mode.sh`:

```bash
#!/usr/bin/env bash
# Fault-injection A/B for the orchestrator engine. RunPod-only: hardcodes
# /workspace/Labmate and writes fixtures under /workspace/. Adjust paths or call
# run_fault_ab.run_suite directly on another host.
#
# Usage: bash eval/orchestrator_ab/run_mode.sh <graph|lite>
set -euo pipefail
MODE="${1:?usage: run_mode.sh <graph|lite>}"
cd /workspace/Labmate

# Reset fixtures the fault tasks touch.
printf 'def add(a, b):\n    return a - b  # bug\n' > /workspace/ab_buggy.py
echo "scratch" > /workspace/ab_scratch.txt

# Restart the orchestrator under the chosen engine (process-wide flag).
infrastructure/local/stop.sh || true
ORCHESTRATOR="$MODE" infrastructure/local/start.sh
infrastructure/local/status.sh

# Drive the suite (push tasks via Redis, kill+restart mid-task, poll results).
ORCHESTRATOR="$MODE" PYTHONPATH=. python -m eval.orchestrator_ab.run_fault_ab --mode "$MODE"
echo "wrote eval/orchestrator_ab/results-${MODE}.json"
```

Add `eval/orchestrator_ab/__init__.py` (empty) and `tests/eval/__init__.py` (empty) if the eval package lacks them, so the test import resolves.

> The `run_fault_ab.py` `__main__` block (wiring real Redis push/poll + a `restart()` that shells `infrastructure/local/stop.sh`+`start.sh`) is a thin live-only glue layer; it is NOT unit-tested (the pure logic and seams above are). Implement it as a small `if __name__ == "__main__":` that builds `push`/`poll`/`restart` closures over `redis.asyncio` and `asyncio.create_subprocess_exec`, then `asyncio.run(run_suite(...))`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -m pytest tests/eval/test_run_fault_ab.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
chmod +x eval/orchestrator_ab/run_mode.sh
git add eval/orchestrator_ab/ tests/eval/test_run_fault_ab.py tests/eval/__init__.py
git commit -m "feat(eval): fault-injection A/B harness for graph vs lite orchestrator"
```

---

## Task 10: Engineering scorecard + regression-gate docs (the decider, part ii)

**Files:**
- Create: `eval/orchestrator_ab/SCORECARD.md`
- Create: `eval/orchestrator_ab/regression_gate.md`

These are the human-judged half of the decision. No code/tests — documentation deliverables that the implementer fills with REAL measured numbers after Tasks 1–9 land.

- [ ] **Step 1: Write the scorecard skeleton with real measurement commands**

Create `eval/orchestrator_ab/SCORECARD.md`:

```markdown
# Orchestrator Engineering Scorecard — A (graph) vs B (lite)

Decision input (ii). Pair with the fault-injection results (results-graph.json /
results-lite.json) for the full A-vs-B decision. The behavioral A/B (eval/seq_ab)
is a TIE and is used only as a regression gate (see regression_gate.md).

## 1. LOC delta

Measure with:

    # Lines the lite engine ADDS (its own modules):
    wc -l services/orchestrator/lite_orchestrator.py \
          services/orchestrator/lite_state.py \
          services/orchestrator/lite_approval.py \
          services/orchestrator/lite_persistence.py

    # Lines the graph engine costs today (for reference, NOT removed by the spike):
    wc -l services/orchestrator/graph.py

| Engine | Core LOC | Notes |
|---|---|---|
| A (graph) | <fill: graph.py> | + LangGraph node/router boilerplate |
| B (lite)  | <fill: 4 lite_*.py> | plain async; no graph framework |

## 2. Droppable dependencies (only if B is adopted)

If the lite engine REPLACES graph (not in this spike — a follow-up), these become
removable:

- `langgraph`
- `langgraph-checkpoint-mongodb`

Confirm nothing else imports them:

    grep -rn "langgraph" services/ tests/ | grep -v "graph.py\|test_graph"

| Dependency | Other importers? | Droppable if B adopted |
|---|---|---|
| langgraph | <fill> | <yes/no> |
| langgraph-checkpoint-mongodb | <fill> | <yes/no> |

## 3. "Add one sample feature in both" ergonomics

Pick ONE small feature (suggestion: a per-goal wall-clock deadline that
finalizes early). Implement it in BOTH engines and record:

| | A (graph) | B (lite) |
|---|---|---|
| Files touched | <fill> | <fill> |
| New node/router vs inline branch | <fill> | <fill> |
| Checkpoint-compat considerations | <fill> | <fill> |
| Subjective friction (1-5) | <fill> | <fill> |

## 4. Recommendation

<fill after fault-injection results: KEEP graph / ADOPT lite / KEEP graph but
adopt lite's approach for the inner durability> — justified by §1-3 + the
resilience A/B recovery_rate / correctness_rate.
```

- [ ] **Step 2: Write the regression-gate one-pager**

Create `eval/orchestrator_ab/regression_gate.md`:

```markdown
# Regression Gate — does `lite` preserve `graph` behavior?

The behavioral A/B (eval/seq_ab) is NOT the decider: both engines call the same
_run_react_loop, so completion/honesty TIE. We run it only to confirm the lite
engine did not REGRESS behavior versus graph.

## Run both engines through the existing behavioral A/B

    # Graph (baseline):
    ORCHESTRATOR=graph SEQUENCING_MODE=skill_first bash eval/seq_ab/run_mode.sh skill_first
    cp eval/seq_ab/results-skill_first.json eval/orchestrator_ab/seq-graph.json

    # Lite (spike):
    ORCHESTRATOR=lite  SEQUENCING_MODE=skill_first bash eval/seq_ab/run_mode.sh skill_first
    cp eval/seq_ab/results-skill_first.json eval/orchestrator_ab/seq-lite.json

> NOTE: eval/seq_ab/run_mode.sh restarts the orchestrator; ensure ORCHESTRATOR is
> exported so the restart picks up the engine. The 5 seq_ab cases (3 compound + 2
> controls) should produce equivalent ok/skill-sequence outcomes across engines.

## Pass criterion

For each of the 5 cases, `lite` matches `graph` on `ok` and on whether the
expected work was performed. A divergence is a lite BUG, not an A/B signal — fix
the lite engine until the behavioral gate ties, THEN judge on the
fault-injection A/B + scorecard.
```

- [ ] **Step 3: Verify the docs reference real, existing paths**

Run: `cd /Users/zachstallbohm/Work/Labmate && ls eval/seq_ab/run_mode.sh services/orchestrator/graph.py && echo "paths OK"`
Expected: prints both paths then `paths OK`

- [ ] **Step 4: Commit**

```bash
git add eval/orchestrator_ab/SCORECARD.md eval/orchestrator_ab/regression_gate.md
git commit -m "docs(eval): engineering scorecard + regression-gate for orchestrator A/B"
```

---

## Task 11: Full lite-suite green + branch-isolation verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full lite + main + graph + bdd suite**

Run:
```bash
cd /Users/zachstallbohm/Work/Labmate && python -m pytest \
  tests/services/orchestrator/test_lite_orchestrator.py \
  tests/services/orchestrator/test_lite_approval.py \
  tests/services/orchestrator/test_lite_persistence.py \
  tests/services/orchestrator/test_main_lite_flag.py \
  tests/services/orchestrator/test_lite_orchestrator_bdd.py \
  tests/eval/test_run_fault_ab.py \
  tests/services/orchestrator/test_main.py \
  tests/services/orchestrator/test_graph.py -q
```
Expected: all PASS — the new suite green AND `test_main.py`/`test_graph.py` unchanged (default `graph` path untouched).

- [ ] **Step 2: Confirm the ONLY hot-file edit is the flag dispatch**

Run: `cd /Users/zachstallbohm/Work/Labmate && git diff --stat origin/feat/agentic-fix-loop -- services/orchestrator/graph.py services/orchestrator/coding_orchestrator.py`
Expected: NO changes to `graph.py` or `coding_orchestrator.py` (empty diff). Only `main.py` changed among hot files.

- [ ] **Step 3: Confirm default flag is `graph`**

Run: `cd /Users/zachstallbohm/Work/Labmate && python -c "import os; os.environ.pop('ORCHESTRATOR', None); import importlib, services.orchestrator.main as m; importlib.reload(m); print(m.ORCHESTRATOR)"`
Expected: prints `graph`

- [ ] **Step 4: Commit (if any verification fix was needed; otherwise skip)**

```bash
git commit --allow-empty -m "chore(lite): verify branch isolation + flag default graph"
```

---

## Self-Review

**1. Spec coverage:**

| Brief requirement | Task |
|---|---|
| `run_goal_lite` plain async, no LangGraph | 4–6 |
| Ambiguity assessment reuses architect logic; halt on ambiguous | 4 |
| Route via `requires_editing`/`SEQUENCING_MODE` | 5 (via `react_execute`, which owns that routing) |
| Execute by calling EXISTING `_run_react_loop` UNCHANGED (reuse, not reimplement) | 5 (calls `react_execute` → `_run_react_loop` verbatim) |
| reflect→retry up to `MAX_GOAL_ATTEMPTS` | 6 |
| Durable approval gate: persist + AWAIT Redis signal (mirror cancel/steer) | 2 (helpers) + 6 (wired) |
| Hand-coded state persistence via `StorageManager` | 3 + 6 (save before approval suspend) |
| Spike scope: happy + ambiguity + approval + reflect; NO replan/multi-goal/critique | stated in Honesty Note + Task 4 docstring |
| Flag-gated dispatch in `main.py`, `ORCHESTRATOR` default `graph`, read once | 7 |
| ONLY hot-file edit is `main.py` | 7 + verified Task 11 Step 2 |
| Fault-injection resilience harness (the decider, part i) | 9 |
| Engineering scorecard (LOC, droppable deps, ergonomics) | 10 |
| eval/seq_ab as REGRESSION gate only, not decider | 10 (regression_gate.md) + Honesty Note |
| Decider = resilience A/B + scorecard, NOT a behavioral tie | Honesty Note + 9 + 10 |
| BDD: feature + step defs (contract exists; don't recreate fake_model) | 8 |
| approval-await + persistence unit-testable with fakeredis / fake store | 2 + 3 |
| asyncio-correct, stdout-sacred, no Discord | Global Constraints + module docstrings |

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". The SCORECARD.md `<fill>` markers are INTENTIONAL data-entry slots for measured numbers (the deliverable is the filled doc) with exact measurement commands provided — not code placeholders. Every code step shows complete code.

**3. Type consistency:** `run_goal_lite(*, orch, async_orch, task, task_id, session_id, user_id, workspace_id, redis, store, max_attempts)` is identical across Tasks 4/5/6/7/8. `finalize_lite(...)` keyword args match between `lite_state.py` and all call sites. `await_approval` returns `"approve"/"deny"/"timeout"` consistently in Tasks 2 and 6. `react_execute(goal)` (existing) is the reuse seam in Task 5 — confirmed it returns `{"ok", "summary", "tools_used"}`. `run_one(...)`/`run_suite(...)` signatures match between `run_fault_ab.py` and `test_run_fault_ab.py`.

**Branch-isolation note:** This is an exploratory SPIKE on a SEPARATE branch. Rebase onto latest `feat/agentic-fix-loop` BEFORE starting (live e2e may have moved `graph.py`/`coding_orchestrator.py`). All new logic is in NEW modules; the only hot-file edit is the flag-gated `main.py` dispatch with default `graph`, so the existing LangGraph path is byte-identical and fully regression-safe. The deliverable is a DECISION (keep graph vs adopt lite), and the decider is the fault-injection resilience A/B (Task 9) + engineering scorecard (Task 10) — NOT a behavioral A/B, which ties.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-26-lite-orchestrator-spike.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
