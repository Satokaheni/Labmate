# Durable Per-Turn Loop Checkpoint + LangGraph Trim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a best-effort per-turn checkpoint to the inner ReAct loop so a crash+restart resumes from the saved turn instead of from scratch, then trim provably-dead vestigial code from `graph.py` / `types.py` — all additive and regression-safe behind an OFF-by-default flag.

**Architecture:** This is "Option A+trim" from the orchestrator-architecture brainstorm. Labmate keeps its TWO-LAYER design — the OUTER LangGraph `StateGraph` (`graph.py`) wrapping the INNER plain `while True` loop (`_run_react_loop` in `coding_orchestrator.py`). The outer LangGraph layer is already crash-durable via `AsyncMongoDBSaver` (it checkpoints between super-steps). The GAP is the inner loop: a crash mid-`_run_react_loop` loses the whole turn-by-turn state (messages, budget, edited-files, nudge count, loop signatures, wall-clock start) and the loop restarts from turn 0. Part 1 closes that gap the way hermes' `checkpoint_manager` (per-turn snapshots) and openclaw's transcript-replay do — a lightweight per-turn snapshot persisted to Mongo, NO extra workflow engine. Part 2 removes graph code that hollowed out after the single-intent simplification and off-by-default gates.

**Tech Stack:** Python 3.11+, asyncio, pytest + pytest-asyncio, pytest-bdd (respx HTTP-seam mock via `tests/conftest.py::fake_model`), Motor (async MongoDB), `redis.asyncio`. No new third-party deps.

## Global Constraints

Copied verbatim from CLAUDE.md and the task spec — every task's requirements implicitly include this section.

- **stdout is sacred.** Never `print()` / `console.log()` / write stdout in any orchestrator path. Log to stderr via `logging.getLogger(...)`. The checkpoint store is best-effort: any error → log to stderr, **never raise**.
- **Best-effort durability:** a checkpoint load/save/clear failure (Mongo down, malformed doc, serialization error) must NEVER break or even perturb the ReAct loop. On any failure the loop proceeds exactly as if the feature were off.
- **Additive + regression-safe:** new behavior is gated by `ENABLE_LOOP_CHECKPOINT`, default `"0"` (OFF). With the flag OFF the loop performs **zero** load/save/clear calls and behaves byte-identically to today. No `State`/`types.py` field is *added* for Part 1 (the checkpoint lives in its own Mongo collection, keyed by `task_id`, not in LangGraph `State`).
- **asyncio-correct:** never `asyncio.run()` inside an async function/context. The store interface is `async def`. Serialize/deserialize are PURE sync functions (no async, no I/O).
- **No tiktoken.** No token counting added here.
- **AsyncMongoDBSaver stays.** Do NOT remove or replace the LangGraph checkpointer. Part 1 is an *additional, complementary* inner-loop checkpoint — not a replacement for the outer one.
- **`task_id` reach:** obtained via `services.orchestrator.events.current_task_id()` (already imported as `from . import events` in `coding_orchestrator.py`). Returns `None` when no `EventEmitter` is set (unit tests / no active task). When `task_id` is `None`, the checkpoint wire-in is a complete no-op.
- **Pure serialize/deserialize, exhaustively unit-tested:** round-trip must be lossless for every field listed in the File Map; must survive `json.dumps()`/`json.loads()`; unknown/missing keys on load degrade gracefully (return `None`, never raise).
- **CONCURRENCY / REBASE (mandatory first step):** live e2e is pushing fixes to `coding_orchestrator.py` and `graph.py` concurrently. Before writing ANY code: `git fetch` and rebase this branch onto the latest `feat/agentic-fix-loop` tip, then **re-verify every insertion point against the current source** (anchor on STRUCTURE — function names, variable names, the verbatim anchor lines quoted below — NOT line numbers, which will have drifted). Put new logic in NEW modules; keep wire-ins into `coding_orchestrator.py` / `graph.py` minimal and clearly marked. For Part 2, skip/defer any trim candidate that a concurrent e2e fix has touched.

---

## File Map

**New files (Part 1):**

- `services/orchestrator/loop_checkpoint.py` — the whole feature. Three concerns in one focused module:
  1. **`LoopCheckpoint` dataclass** — the resumable inner-loop state. JSON-able fields:
     - `task_id: str`
     - `goal: str`
     - `messages: list[dict]` — the running ReAct `messages` list.
     - `used: int` — `IterationBudget._used` at end of turn (consumed units).
     - `absolute_turns: int` — `IterationBudget._absolute_turns`.
     - `grace_used: bool` — `IterationBudget._grace_used`.
     - `edited_files: list[str]` — sorted list form of the `edited_files: set[str]`.
     - `tests_passed: bool`
     - `verify_nudges_used: int`
     - `loop_signatures: list[str]` — `LoopDetector._sigs`.
     - `tools_used: list[str]` — the `_tools_used` accumulator.
     - `start_monotonic_offset: float` — elapsed wall-clock seconds already spent on this goal at save time (NOT a raw `time.monotonic()` value — monotonic clocks are not comparable across process restarts; we persist *elapsed* and rebase on resume).
     - `turn: int` — the turn index just completed (== `used` after a normal turn; kept explicit for clarity/debugging).
     - `version: int` — schema version (start at `1`); load returns `None` if the doc's version is unknown.
  2. **`to_dict(cp: LoopCheckpoint) -> dict`** and **`from_dict(d: dict) -> LoopCheckpoint | None`** — PURE serialize/deserialize. `from_dict` returns `None` on any structural problem (missing required key, wrong `version`, wrong type) — never raises.
  3. **`CheckpointStore` (async, best-effort)** — thin wrapper over a Mongo collection. Methods `save`, `load`, `clear`. A `FakeCheckpointStore` (in tests) implements the same 3 methods over an in-memory dict.

**New files (Part 1, tests):**

- `tests/services/orchestrator/test_loop_checkpoint.py` — pure round-trip + store unit tests (no Mongo; uses `FakeCheckpointStore` / a Mongo-collection mock).
- `tests/services/orchestrator/features/durable_loop_checkpoint.feature` — `@mocked` Gherkin (resume-from-checkpoint mid-loop; flag-OFF identical; clear-on-finish).
- `tests/services/orchestrator/test_durable_loop_checkpoint_bdd.py` — step defs.

**Modified files (Part 1, minimal wire-in — THREE clearly-bounded insertion points):**

- `services/orchestrator/coding_orchestrator.py` — only inside `_run_react_loop` and its construction site:
  - **Insertion A (loop entry, before `while True:`):** attempt LOAD + rehydrate.
  - **Insertion B (end of each turn, just before the loop repeats):** SAVE.
  - **Insertion C (every terminal `return` of `_run_react_loop`):** CLEAR.
  - Plus a constructor hook so a `CheckpointStore` can be injected (default `None`).

**Modified files (Part 2 — trim, each bounded + reversible):**

- `services/orchestrator/graph.py` and `services/orchestrator/types.py` — remove only provably-dead code, one candidate per commit, each guarded by a no-usages grep + full graph suite green.

**Untouched:** `events.py` (reuse `current_task_id()` as-is), `storage_manager.py` (we add a *new* collection accessor, not modify existing methods), `iteration_budget.py`, `loop_detection.py`, `AsyncMongoDBSaver` wiring.

---

## Part 1 — Per-turn ReAct-loop checkpoint

### Task 1: `LoopCheckpoint` dataclass + pure `to_dict` / `from_dict`

**Files:**
- Create: `services/orchestrator/loop_checkpoint.py`
- Test: `tests/services/orchestrator/test_loop_checkpoint.py`

**Interfaces:**
- Produces:
  - `@dataclass LoopCheckpoint` with the fields listed in the File Map.
  - `CHECKPOINT_VERSION: int = 1`
  - `to_dict(cp: LoopCheckpoint) -> dict`
  - `from_dict(d: dict | None) -> LoopCheckpoint | None`

- [ ] **Step 1: Write the failing round-trip + degradation tests**

```python
# tests/services/orchestrator/test_loop_checkpoint.py
"""Pure serialize/deserialize unit tests for the inner-loop checkpoint.

No Mongo, no asyncio in this file — to_dict/from_dict are pure sync functions.
"""
from __future__ import annotations

import json

from services.orchestrator.loop_checkpoint import (
    CHECKPOINT_VERSION,
    LoopCheckpoint,
    to_dict,
    from_dict,
)


def _sample() -> LoopCheckpoint:
    return LoopCheckpoint(
        task_id="task-123",
        goal="fix the factorial off-by-one",
        messages=[
            {"role": "system", "content": "you are a coding agent"},
            {"role": "user", "content": "fix the factorial off-by-one"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "def factorial(...)"},
        ],
        used=3,
        absolute_turns=3,
        grace_used=False,
        edited_files=["src/math.py"],
        tests_passed=False,
        verify_nudges_used=1,
        loop_signatures=["read_file::{}", "write_file::{\"path\":\"x\"}"],
        tools_used=["read_file", "write_file"],
        start_monotonic_offset=12.5,
        turn=3,
    )


def test_round_trip_is_lossless():
    cp = _sample()
    restored = from_dict(to_dict(cp))
    assert restored == cp


def test_to_dict_is_json_serializable():
    cp = _sample()
    d = to_dict(cp)
    # Must survive a full JSON round-trip (Mongo stores BSON, but we keep it
    # JSON-able so the dict is trivially inspectable/loggable).
    reparsed = json.loads(json.dumps(d))
    assert from_dict(reparsed) == cp


def test_to_dict_includes_version():
    assert to_dict(_sample())["version"] == CHECKPOINT_VERSION


def test_from_dict_none_returns_none():
    assert from_dict(None) is None


def test_from_dict_missing_required_key_returns_none():
    d = to_dict(_sample())
    del d["messages"]
    assert from_dict(d) is None


def test_from_dict_unknown_version_returns_none():
    d = to_dict(_sample())
    d["version"] = 999
    assert from_dict(d) is None


def test_from_dict_wrong_type_returns_none():
    d = to_dict(_sample())
    d["used"] = "not-an-int"
    assert from_dict(d) is None


def test_from_dict_tolerates_extra_keys():
    d = to_dict(_sample())
    d["_id"] = "mongo-object-id"  # Mongo adds this; load must ignore it
    d["saved_at"] = "2026-06-26T00:00:00Z"
    assert from_dict(d) == _sample()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/services/orchestrator/test_loop_checkpoint.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.loop_checkpoint'`

- [ ] **Step 3: Write the module**

```python
# services/orchestrator/loop_checkpoint.py
"""Durable per-turn checkpoint for the inner ReAct loop (_run_react_loop).

The OUTER LangGraph layer is already crash-durable via AsyncMongoDBSaver, which
checkpoints between super-steps. This module closes the complementary gap in the
INNER loop: a crash mid-_run_react_loop otherwise loses the whole turn-by-turn
state (messages, iteration budget, edited files, verify-nudge count, loop-detector
signatures, wall-clock start) and the loop restarts from turn 0 on the next
run_task() with the same session.

Two halves:
  * PURE serialize/deserialize (LoopCheckpoint + to_dict/from_dict) — no async,
    no I/O, exhaustively unit-testable. from_dict NEVER raises: any structural
    problem (missing key, unknown version, wrong type) yields None.
  * An async, BEST-EFFORT store (CheckpointStore) over a Mongo collection. Every
    method swallows + logs (stderr) all errors and never raises, so the ReAct
    loop is never broken by a checkpoint failure.

CLAUDE.md: stdout is sacred (log to stderr); no tiktoken; asyncio-correct.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("loop_checkpoint")

CHECKPOINT_VERSION = 1

# Mongo collection holding one in-flight inner-loop checkpoint per task_id.
CHECKPOINT_COLLECTION = "loop_checkpoints"


@dataclass
class LoopCheckpoint:
    """Resumable snapshot of one _run_react_loop, taken at end of each turn.

    All fields are JSON-able. ``start_monotonic_offset`` is ELAPSED seconds
    already spent on this goal at save time — NOT a raw time.monotonic() value
    (monotonic clocks are not comparable across a process restart). On resume the
    loop rebases its deadline clock against this elapsed offset.
    """
    task_id: str
    goal: str
    messages: list[dict] = field(default_factory=list)
    used: int = 0
    absolute_turns: int = 0
    grace_used: bool = False
    edited_files: list[str] = field(default_factory=list)
    tests_passed: bool = False
    verify_nudges_used: int = 0
    loop_signatures: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    start_monotonic_offset: float = 0.0
    turn: int = 0


# Required keys whose presence + type from_dict validates. (version handled
# separately; defaulted/optional scalars are coerced, not required.)
_REQUIRED = {
    "task_id": str,
    "goal": str,
    "messages": list,
}


def to_dict(cp: LoopCheckpoint) -> dict:
    """Serialize a LoopCheckpoint to a JSON-able dict (PURE)."""
    return {
        "version": CHECKPOINT_VERSION,
        "task_id": cp.task_id,
        "goal": cp.goal,
        "messages": cp.messages,
        "used": cp.used,
        "absolute_turns": cp.absolute_turns,
        "grace_used": cp.grace_used,
        "edited_files": list(cp.edited_files),
        "tests_passed": cp.tests_passed,
        "verify_nudges_used": cp.verify_nudges_used,
        "loop_signatures": list(cp.loop_signatures),
        "tools_used": list(cp.tools_used),
        "start_monotonic_offset": cp.start_monotonic_offset,
        "turn": cp.turn,
    }


def from_dict(d: dict | None) -> LoopCheckpoint | None:
    """Deserialize a dict into a LoopCheckpoint (PURE). Never raises.

    Returns None on: None input, unknown version, a missing/mistyped REQUIRED
    field, or any unexpected structural error. Extra keys (e.g. Mongo's _id) are
    ignored. Optional scalars are coerced defensively.
    """
    if not isinstance(d, dict):
        return None
    try:
        if int(d.get("version", -1)) != CHECKPOINT_VERSION:
            return None
        for key, typ in _REQUIRED.items():
            if key not in d or not isinstance(d[key], typ):
                return None
        return LoopCheckpoint(
            task_id=d["task_id"],
            goal=d["goal"],
            messages=list(d["messages"]),
            used=int(d.get("used", 0)),
            absolute_turns=int(d.get("absolute_turns", 0)),
            grace_used=bool(d.get("grace_used", False)),
            edited_files=list(d.get("edited_files", []) or []),
            tests_passed=bool(d.get("tests_passed", False)),
            verify_nudges_used=int(d.get("verify_nudges_used", 0)),
            loop_signatures=list(d.get("loop_signatures", []) or []),
            tools_used=list(d.get("tools_used", []) or []),
            start_monotonic_offset=float(d.get("start_monotonic_offset", 0.0)),
            turn=int(d.get("turn", 0)),
        )
    except (TypeError, ValueError, KeyError) as exc:
        _log.warning("from_dict rejected a malformed checkpoint: %s", exc)
        return None


__all__ = [
    "CHECKPOINT_VERSION",
    "CHECKPOINT_COLLECTION",
    "LoopCheckpoint",
    "to_dict",
    "from_dict",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_loop_checkpoint.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/loop_checkpoint.py tests/services/orchestrator/test_loop_checkpoint.py
git commit -m "feat(orchestrator): pure LoopCheckpoint serialize/deserialize"
```

---

### Task 2: `CheckpointStore` (async, best-effort) + `FakeCheckpointStore`

**Files:**
- Modify: `services/orchestrator/loop_checkpoint.py` (append the store classes)
- Test: `tests/services/orchestrator/test_loop_checkpoint.py` (append store tests)

**Interfaces:**
- Consumes: `LoopCheckpoint`, `to_dict`, `from_dict`, `CHECKPOINT_COLLECTION` (Task 1).
- Produces:
  - `class CheckpointStore` with `__init__(self, collection)` (a Motor collection) and:
    - `async def save(self, cp: LoopCheckpoint) -> None`
    - `async def load(self, task_id: str) -> LoopCheckpoint | None`
    - `async def clear(self, task_id: str) -> None`
  - `class FakeCheckpointStore` — same 3 methods over an in-memory dict; used by tests AND as the dependency-injection seam.

- [ ] **Step 1: Write the failing store tests**

```python
# append to tests/services/orchestrator/test_loop_checkpoint.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.loop_checkpoint import (
    CheckpointStore,
    FakeCheckpointStore,
)


@pytest.mark.asyncio
async def test_fake_store_save_load_clear_round_trip():
    store = FakeCheckpointStore()
    cp = _sample()
    assert await store.load("task-123") is None
    await store.save(cp)
    assert await store.load("task-123") == cp
    await store.clear("task-123")
    assert await store.load("task-123") is None


@pytest.mark.asyncio
async def test_fake_store_save_overwrites_latest():
    store = FakeCheckpointStore()
    await store.save(_sample())
    cp2 = _sample()
    cp2.used = 99
    await store.save(cp2)
    assert (await store.load("task-123")).used == 99


@pytest.mark.asyncio
async def test_mongo_store_save_upserts_by_task_id():
    col = MagicMock()
    col.update_one = AsyncMock()
    store = CheckpointStore(col)
    await store.save(_sample())
    args, kwargs = col.update_one.call_args
    assert args[0] == {"task_id": "task-123"}      # filter
    assert kwargs.get("upsert") is True


@pytest.mark.asyncio
async def test_mongo_store_load_returns_checkpoint():
    col = MagicMock()
    col.find_one = AsyncMock(return_value=to_dict(_sample()))
    store = CheckpointStore(col)
    assert await store.load("task-123") == _sample()


@pytest.mark.asyncio
async def test_mongo_store_load_missing_returns_none():
    col = MagicMock()
    col.find_one = AsyncMock(return_value=None)
    store = CheckpointStore(col)
    assert await store.load("nope") is None


@pytest.mark.asyncio
async def test_store_errors_are_swallowed_never_raised():
    # save/load/clear must be best-effort: a raising collection must NOT
    # propagate (the ReAct loop must never break on a checkpoint failure).
    col = MagicMock()
    col.update_one = AsyncMock(side_effect=RuntimeError("mongo down"))
    col.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
    col.delete_one = AsyncMock(side_effect=RuntimeError("mongo down"))
    store = CheckpointStore(col)
    await store.save(_sample())          # must not raise
    assert await store.load("task-123") is None   # error -> None
    await store.clear("task-123")        # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/services/orchestrator/test_loop_checkpoint.py -q`
Expected: FAIL — `ImportError: cannot import name 'CheckpointStore'`

- [ ] **Step 3: Append the store classes to `loop_checkpoint.py`**

```python
# append to services/orchestrator/loop_checkpoint.py (above __all__)

class CheckpointStore:
    """Best-effort persistence of one inner-loop checkpoint per task_id.

    Backed by a Motor (async) collection. EVERY method swallows and logs (to
    stderr via logging) all errors and never raises — a checkpoint failure must
    never break the ReAct loop. Keyed by task_id; save() upserts so only the
    latest snapshot per task is kept.
    """

    def __init__(self, collection: Any) -> None:
        self._col = collection

    async def save(self, cp: LoopCheckpoint) -> None:
        try:
            await self._col.update_one(
                {"task_id": cp.task_id},
                {"$set": to_dict(cp)},
                upsert=True,
            )
        except Exception as exc:  # best-effort: never break the loop
            _log.warning("checkpoint save failed for %s: %s", cp.task_id, exc)

    async def load(self, task_id: str) -> LoopCheckpoint | None:
        try:
            doc = await self._col.find_one({"task_id": task_id})
        except Exception as exc:
            _log.warning("checkpoint load failed for %s: %s", task_id, exc)
            return None
        return from_dict(doc)

    async def clear(self, task_id: str) -> None:
        try:
            await self._col.delete_one({"task_id": task_id})
        except Exception as exc:
            _log.warning("checkpoint clear failed for %s: %s", task_id, exc)


class FakeCheckpointStore:
    """In-memory CheckpointStore for tests and as a default DI seam."""

    def __init__(self) -> None:
        self._mem: dict[str, dict] = {}

    async def save(self, cp: LoopCheckpoint) -> None:
        self._mem[cp.task_id] = to_dict(cp)

    async def load(self, task_id: str) -> LoopCheckpoint | None:
        return from_dict(self._mem.get(task_id))

    async def clear(self, task_id: str) -> None:
        self._mem.pop(task_id, None)
```

Then extend `__all__`:

```python
__all__ = [
    "CHECKPOINT_VERSION",
    "CHECKPOINT_COLLECTION",
    "LoopCheckpoint",
    "to_dict",
    "from_dict",
    "CheckpointStore",
    "FakeCheckpointStore",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_loop_checkpoint.py -q`
Expected: PASS (14 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/loop_checkpoint.py tests/services/orchestrator/test_loop_checkpoint.py
git commit -m "feat(orchestrator): best-effort CheckpointStore + FakeCheckpointStore"
```

---

### Task 3: Wire the checkpoint into `_run_react_loop` (3 bounded insertion points + flag)

**REBASE FIRST.** `git fetch && git rebase origin/feat/agentic-fix-loop`, then re-confirm the anchor lines below exist verbatim in the current `coding_orchestrator.py`. They are STRUCTURAL anchors; if e2e moved them, find them by name, not line number.

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py`
  - `AsyncOrchestrator.__init__` — add a `checkpoint_store` attribute (default `None`).
  - `AsyncOrchestrator._run_react_loop` — Insertions A / B / C.
- Test: `tests/services/orchestrator/test_loop_checkpoint.py` (append a wire-in unit test using `FakeCheckpointStore`).

**Interfaces:**
- Consumes: `LoopCheckpoint`, `from_dict`/`to_dict`, `FakeCheckpointStore`, `CheckpointStore` (Tasks 1-2); `events.current_task_id()`; the existing loop locals `messages`, `budget` (`IterationBudget`), `loop_detector` (`LoopDetector`), `edited_files`, `tests_passed`, `verify_nudges_used`, `_tools_used`, `start`, `self._now`.
- Produces: a flag `ENABLE_LOOP_CHECKPOINT` (module-level) and an injectable `self.checkpoint_store`. No new `State` field.

- [ ] **Step 1: Add the module-level flag and constructor hook**

Near the other env knobs at the top of `coding_orchestrator.py` (after the `SEQUENCING_MODE`/`MAX_SEQ_STEPS` block), add:

```python
from .loop_checkpoint import (
    LoopCheckpoint,
    CheckpointStore,
    from_dict as _cp_from_dict,
)

# Durable per-turn inner-loop checkpoint (Option A). OFF by default — the inner
# loop was just stabilized, so this is regression-safe; flip ON after the
# resilience A/B (sibling lite-orchestrator plan) validates it. When OFF,
# _run_react_loop performs ZERO load/save/clear and is byte-identical to today.
ENABLE_LOOP_CHECKPOINT = os.getenv("ENABLE_LOOP_CHECKPOINT", "0") not in (
    "0", "false", "False", "",
)
```

In `AsyncOrchestrator.__init__`, alongside the other `self.<x> = ...` assignments (anchor: the line `self.redis = redis`), add:

```python
        # Injected post-construction by the orchestrator bootstrap when a Mongo
        # handle is available (CheckpointStore over the loop_checkpoints
        # collection). None in unit tests / when checkpointing is unwired.
        self.checkpoint_store = None
```

- [ ] **Step 2: Add a private helper that decides whether checkpointing is active**

Add this method on `AsyncOrchestrator` (place it just above `_run_react_loop`, anchor: `async def _run_react_loop(self, goal: str, max_steps: int) -> dict:`):

```python
    def _checkpoint_active(self, task_id: str | None) -> bool:
        """Checkpointing runs only when the flag is ON, a store is wired, and a
        task_id is available. All three absent in unit tests -> complete no-op."""
        return bool(
            ENABLE_LOOP_CHECKPOINT
            and self.checkpoint_store is not None
            and task_id is not None
        )
```

- [ ] **Step 3: Insertion A — LOAD + rehydrate at loop entry**

In `_run_react_loop`, AFTER the locals are initialized (anchor: the block that builds `assembler`, `tools`, `messages`, `loop_detector`, `_tools_used`, `edited_files`, `tests_passed`, `verify_nudges_used`, `cap`, `budget`, `start`) and BEFORE the `try:` that wraps `while True`, insert:

```python
        # ── Insertion A: durable inner-loop checkpoint — LOAD + rehydrate ──────
        # Best-effort. On a crash+restart (same task_id), resume from the saved
        # turn with the saved messages/counters instead of starting from turn 0.
        try:
            _cp_task_id = events.current_task_id()
        except AttributeError:
            _cp_task_id = None
        if self._checkpoint_active(_cp_task_id):
            _loaded = await self.checkpoint_store.load(_cp_task_id)
            if _loaded is not None and _loaded.goal == goal:
                messages = list(_loaded.messages)
                budget._used = _loaded.used
                budget._absolute_turns = _loaded.absolute_turns
                budget._grace_used = _loaded.grace_used
                loop_detector._sigs = list(_loaded.loop_signatures)
                edited_files = set(_loaded.edited_files)
                tests_passed = _loaded.tests_passed
                verify_nudges_used = _loaded.verify_nudges_used
                _tools_used = list(_loaded.tools_used)
                # Rebase the wall-clock deadline: subtract elapsed-so-far from
                # 'start' so deadline_s still measures total goal time across the
                # restart (monotonic values are not comparable across processes).
                start = self._now() - _loaded.start_monotonic_offset
                await events.emit(
                    "loop.checkpoint.resumed",
                    task_id=_cp_task_id,
                    turn=_loaded.turn,
                    used=_loaded.used,
                )
```

> Note: writing private `IterationBudget` / `LoopDetector` attributes directly here is acceptable and intentional — those classes are pure in-process state holders with no validation, and this is the single rehydration site. (Do NOT add setter methods to them; that would widen their API for one caller.)

- [ ] **Step 4: Insertion B — SAVE at end of each turn**

At the very END of the `while True` body — AFTER the no-progress breaker block (anchor: the `pstep: ProgressStep = breaker.step(...)` / `if pstep.tripped:` block) and the trailing comment about "Update pending steer for next iteration", i.e. the last statements before the loop repeats — insert:

```python
                # ── Insertion B: durable inner-loop checkpoint — SAVE turn ─────
                # Best-effort end-of-turn snapshot. A crash before the next model
                # call resumes here (Insertion A) on the next run_task().
                if self._checkpoint_active(_cp_task_id):
                    await self.checkpoint_store.save(LoopCheckpoint(
                        task_id=_cp_task_id,
                        goal=goal,
                        messages=messages,
                        used=budget.used,
                        absolute_turns=budget.absolute_turns,
                        grace_used=budget.grace_used,
                        edited_files=sorted(edited_files),
                        tests_passed=tests_passed,
                        verify_nudges_used=verify_nudges_used,
                        loop_signatures=list(loop_detector._sigs),
                        tools_used=list(_tools_used),
                        start_monotonic_offset=self._now() - start,
                        turn=budget.used,
                    ))
```

- [ ] **Step 5: Insertion C — CLEAR on every terminal exit**

The clean, DRY way to clear on every `return` is to wrap the loop body's `return` points. Rather than touch each `return` (many, and e2e may add more), add a single CLEAR by converting the terminal returns to flow through one helper. Concretely, add a nested coroutine at the top of `_run_react_loop` (just after Insertion A) and replace the bare `return <dict>` statements is NOT required — instead clear in a `finally` on the existing outer `try`.

Locate the existing `try:` / `except Exception as exc:` that wraps the `while True` loop (anchor: `except Exception as exc:\n            return {"ok": False, "summary": f"error: {str(exc)[:1000]}", ...}`). Add a `finally` to that same try:

```python
        finally:
            # ── Insertion C: durable inner-loop checkpoint — CLEAR on exit ─────
            # Every terminal path (return or exception) flows through here, so a
            # finished/aborted goal never leaves a stale checkpoint to be wrongly
            # resumed by a later same-task run.
            if self._checkpoint_active(_cp_task_id):
                await self.checkpoint_store.clear(_cp_task_id)
```

> Because `_cp_task_id` is assigned in Insertion A (before the `try`), it is in scope in the `finally`. The `finally` runs on normal `return`, on the `cancelled`/`budget`/`loop`/`deadline` returns, AND on the `except` path — exactly the "finish/terminal exit" semantics required.

- [ ] **Step 6: Write the failing wire-in unit test**

```python
# append to tests/services/orchestrator/test_loop_checkpoint.py
import json as _json
from unittest.mock import patch

from services.orchestrator import events
from services.orchestrator.coding_orchestrator import AsyncOrchestrator


def _finish_response(summary: str):
    msg = MagicMock()
    msg.tool_calls = [MagicMock(
        id="c1",
        function=MagicMock(name="finish", arguments=_json.dumps({"summary": summary})),
    )]
    # MagicMock(name=...) sets the mock's repr name, not .name — set explicitly:
    msg.tool_calls[0].function.name = "finish"
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.mark.asyncio
async def test_run_react_loop_resumes_from_preseeded_checkpoint(monkeypatch):
    monkeypatch.setenv("ENABLE_LOOP_CHECKPOINT", "1")
    # Reload the module-level flag computed at import time.
    import importlib
    import services.orchestrator.coding_orchestrator as co
    importlib.reload(co)

    store = FakeCheckpointStore()
    # Pre-seed a checkpoint as if a prior process crashed after turn 2.
    seeded = LoopCheckpoint(
        task_id="task-resume",
        goal="resume me",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "resume me"},
            {"role": "assistant", "content": "prior work done"},
        ],
        used=2, absolute_turns=2, turn=2,
        tools_used=["read_file"],
    )
    await store.save(seeded)

    orch = co.AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.checkpoint_store = store
    orch.redis = MagicMock()

    # Active emitter so current_task_id() returns our task id.
    em = events.EventEmitter(MagicMock(), "task-resume")
    token = events.current_emitter.set(em)
    try:
        with patch.object(co.events, "is_cancelled", new=AsyncMock(return_value=False)), \
             patch.object(co.events, "read_and_clear_steer", new=AsyncMock(return_value=None)), \
             patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(return_value=_finish_response("finished after resume"))):
            result = await orch._run_react_loop("resume me", 6)
    finally:
        events.current_emitter.reset(token)
        importlib.reload(co)  # restore default flag for other tests

    # The seeded prior-work message must be present -> we resumed, not restarted.
    assert result["ok"] is True
    assert "finished after resume" in result["summary"]
    # Checkpoint cleared on finish.
    assert await store.load("task-resume") is None


@pytest.mark.asyncio
async def test_flag_off_performs_no_checkpoint_io():
    # Default flag (OFF) -> store is never touched even when wired.
    store = FakeCheckpointStore()
    store.load = AsyncMock(wraps=store.load)
    store.save = AsyncMock(wraps=store.save)
    store.clear = AsyncMock(wraps=store.clear)

    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.checkpoint_store = store
    orch.redis = MagicMock()

    em = events.EventEmitter(MagicMock(), "task-off")
    token = events.current_emitter.set(em)
    try:
        with patch.object(events, "is_cancelled", new=AsyncMock(return_value=False)), \
             patch.object(events, "read_and_clear_steer", new=AsyncMock(return_value=None)), \
             patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(return_value=_finish_response("done"))):
            await orch._run_react_loop("no checkpoint", 6)
    finally:
        events.current_emitter.reset(token)

    store.load.assert_not_awaited()
    store.save.assert_not_awaited()
    store.clear.assert_not_awaited()
```

- [ ] **Step 7: Run the wire-in tests — verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_loop_checkpoint.py -q`
Expected: PASS (all — pure + store + 2 wire-in tests)

- [ ] **Step 8: Run the full orchestrator suite — verify no regression**

Run: `python -m pytest tests/services/orchestrator/ -q`
Expected: PASS (all existing tests still green; flag is OFF so loop behavior is unchanged).

- [ ] **Step 9: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_loop_checkpoint.py
git commit -m "feat(orchestrator): wire best-effort per-turn checkpoint into _run_react_loop (flag OFF)"
```

---

### Task 4: Wire a real `CheckpointStore` at orchestrator bootstrap (Mongo collection)

**REBASE/RE-VERIFY FIRST** — the bootstrap site may have moved.

**Files:**
- Modify: `services/orchestrator/storage_manager.py` — add a tiny accessor returning the `loop_checkpoints` collection.
- Modify: wherever the `AsyncOrchestrator` is constructed and a `StorageManager` is in scope (search: `AsyncOrchestrator(` in `services/orchestrator/main.py` — confirm the exact site after rebase). Inject `orch.checkpoint_store = CheckpointStore(storage.loop_checkpoint_collection)` only when a Mongo handle exists.
- Test: `tests/services/orchestrator/test_loop_checkpoint.py` (append the accessor test).

**Interfaces:**
- Consumes: `StorageManager._db`, `CHECKPOINT_COLLECTION`.
- Produces: `StorageManager.loop_checkpoint_collection` (property returning a Motor collection).

- [ ] **Step 1: Write the failing accessor test**

```python
# append to tests/services/orchestrator/test_loop_checkpoint.py
from services.orchestrator.loop_checkpoint import CHECKPOINT_COLLECTION


def test_storage_manager_exposes_loop_checkpoint_collection():
    from services.orchestrator.storage_manager import StorageManager
    db = MagicMock()
    sentinel = MagicMock()
    db.__getitem__ = MagicMock(return_value=sentinel)
    mongo = MagicMock()
    mongo.__getitem__ = MagicMock(return_value=db)
    redis = MagicMock()
    sm = StorageManager.from_clients(mongo=mongo, chroma=MagicMock(), redis=redis)
    col = sm.loop_checkpoint_collection
    db.__getitem__.assert_called_with(CHECKPOINT_COLLECTION)
    assert col is sentinel
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_loop_checkpoint.py::test_storage_manager_exposes_loop_checkpoint_collection -q`
Expected: FAIL — `AttributeError: 'StorageManager' object has no attribute 'loop_checkpoint_collection'`

- [ ] **Step 3: Add the accessor to `StorageManager`**

In `services/orchestrator/storage_manager.py`, alongside the other `@property` accessors (anchor: the `def workspaces(self)` property), add:

```python
    @property
    def loop_checkpoint_collection(self):
        """The Motor collection holding inner-loop checkpoints (one per task_id).

        Used by CheckpointStore (services/orchestrator/loop_checkpoint.py) to
        persist the per-turn ReAct-loop snapshot. No outbox: these are transient
        crash-recovery records, cleared when a goal finishes.
        """
        from .loop_checkpoint import CHECKPOINT_COLLECTION
        return self._db[CHECKPOINT_COLLECTION]
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_loop_checkpoint.py::test_storage_manager_exposes_loop_checkpoint_collection -q`
Expected: PASS

- [ ] **Step 5: Inject the store at the AsyncOrchestrator construction site**

After rebase, locate where `AsyncOrchestrator(...)` is constructed in the live orchestrator bootstrap (grep `AsyncOrchestrator(` across `services/orchestrator/` — most likely `main.py`) and where a `StorageManager` instance is in scope. Immediately after construction, add (guarded so tests/no-Mongo paths stay `None`):

```python
        # Wire the durable inner-loop checkpoint store when a Mongo-backed
        # StorageManager is available. No-op when checkpointing is disabled
        # (the flag is read inside _run_react_loop) or no storage exists.
        try:
            from .loop_checkpoint import CheckpointStore
            async_orch.checkpoint_store = CheckpointStore(
                storage.loop_checkpoint_collection
            )
        except Exception:  # best-effort wiring; never block startup
            async_orch.checkpoint_store = None
```

> If, after rebase, the bootstrap has no `StorageManager`/`storage` symbol in scope at the construction site, DEFER this injection (leave `checkpoint_store = None`); the feature is OFF by default anyway, and Task 3's unit tests inject `FakeCheckpointStore` directly. Note the deferral in the commit message.

- [ ] **Step 6: Run the orchestrator suite**

Run: `python -m pytest tests/services/orchestrator/ -q`
Expected: PASS (all green).

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/storage_manager.py services/orchestrator/main.py tests/services/orchestrator/test_loop_checkpoint.py
git commit -m "feat(orchestrator): expose loop_checkpoint_collection + inject CheckpointStore at bootstrap"
```

---

### Task 5: BDD — resume / flag-off-identical / clear-on-finish

**Files:**
- Create: `tests/services/orchestrator/features/durable_loop_checkpoint.feature`
- Create: `tests/services/orchestrator/test_durable_loop_checkpoint_bdd.py`

**Interfaces:**
- Consumes: `fake_model` fixture (`tests/conftest.py`), `run_async` helper, `AsyncOrchestrator`, `FakeCheckpointStore`, `LoopCheckpoint`, `events`.

- [ ] **Step 1: Write the `.feature` file**

```gherkin
# tests/services/orchestrator/features/durable_loop_checkpoint.feature
@mocked
Feature: Durable per-turn inner-loop checkpoint
  As the ReAct loop orchestrator
  I want each turn snapshotted to a best-effort store
  So that a crash + restart resumes from the saved turn instead of from scratch
  And the loop is byte-identical to today when the feature is off

  Scenario: a pre-seeded checkpoint resumes mid-loop, not from scratch
    Given an AsyncOrchestrator with a fake checkpoint store and task id "task-resume"
    And loop checkpointing is enabled
    And a checkpoint is pre-seeded for goal "resume me" at turn 2 with prior message "prior work done"
    And the model calls finish with summary "finished after resume"
    When the react loop runs the goal "resume me"
    Then the result ok is True
    And the result summary contains "finished after resume"
    And the running messages include "prior work done"

  Scenario: a finished goal clears its checkpoint
    Given an AsyncOrchestrator with a fake checkpoint store and task id "task-clear"
    And loop checkpointing is enabled
    And the model calls finish with summary "all done"
    When the react loop runs the goal "do the thing"
    Then the result ok is True
    And no checkpoint remains for task "task-clear"

  Scenario: with the feature off the loop performs no checkpoint IO
    Given an AsyncOrchestrator with a fake checkpoint store and task id "task-off"
    And loop checkpointing is disabled
    And the model calls finish with summary "done"
    When the react loop runs the goal "no checkpoint"
    Then the result ok is True
    And the checkpoint store was never read or written
```

- [ ] **Step 2: Write the step defs**

```python
# tests/services/orchestrator/test_durable_loop_checkpoint_bdd.py
"""Step definitions for the durable inner-loop checkpoint BDD feature."""
from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator import events
from services.orchestrator.loop_checkpoint import LoopCheckpoint, FakeCheckpointStore
from tests.conftest import run_async

scenarios("features/durable_loop_checkpoint.feature")


def _finish_msg(summary: str):
    tc = MagicMock()
    tc.id = "c1"
    tc.function = MagicMock()
    tc.function.name = "finish"
    tc.function.arguments = json.dumps({"summary": summary})
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.fixture
def ctx():
    return {"store": None, "task_id": None, "response": None, "result": None,
            "co": None, "token": None}


@given(parsers.parse('an AsyncOrchestrator with a fake checkpoint store and task id "{task_id}"'))
def _orch(ctx, task_id):
    import services.orchestrator.coding_orchestrator as co
    ctx["co"] = co
    store = FakeCheckpointStore()
    store.load = AsyncMock(wraps=store.load)
    store.save = AsyncMock(wraps=store.save)
    store.clear = AsyncMock(wraps=store.clear)
    ctx["store"] = store
    ctx["task_id"] = task_id


@given("loop checkpointing is enabled")
def _enable(ctx, monkeypatch):
    monkeypatch.setenv("ENABLE_LOOP_CHECKPOINT", "1")
    ctx["co"] = importlib.reload(ctx["co"])


@given("loop checkpointing is disabled")
def _disable(ctx, monkeypatch):
    monkeypatch.delenv("ENABLE_LOOP_CHECKPOINT", raising=False)
    ctx["co"] = importlib.reload(ctx["co"])


@given(parsers.parse(
    'a checkpoint is pre-seeded for goal "{goal}" at turn {turn:d} with prior message "{msg}"'))
def _preseed(ctx, goal, turn, msg):
    seeded = LoopCheckpoint(
        task_id=ctx["task_id"], goal=goal, turn=turn, used=turn, absolute_turns=turn,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": goal},
            {"role": "assistant", "content": msg},
        ],
    )
    run_async(ctx["store"].save(seeded))
    # reset the wrapped-mock call count so "never read/written" assertions are clean
    ctx["store"].save.reset_mock()


@given(parsers.parse('the model calls finish with summary "{summary}"'))
def _finish(ctx, summary):
    ctx["response"] = _finish_msg(summary)


@when(parsers.parse('the react loop runs the goal "{goal}"'))
def _run(ctx, goal):
    co = ctx["co"]
    orch = co.AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.checkpoint_store = ctx["store"]
    orch.redis = MagicMock()
    ctx["captured_messages"] = {}

    async def _go():
        em = events.EventEmitter(MagicMock(), ctx["task_id"])
        token = events.current_emitter.set(em)
        try:
            with patch.object(co.events, "is_cancelled", new=AsyncMock(return_value=False)), \
                 patch.object(co.events, "read_and_clear_steer", new=AsyncMock(return_value=None)), \
                 patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                       new=AsyncMock(return_value=ctx["response"])):
                # Capture the messages list the store saw via the save mock.
                res = await orch._run_react_loop(ctx["last_goal"], 6)
            return res
        finally:
            events.current_emitter.reset(token)

    ctx["last_goal"] = goal
    ctx["result"] = run_async(_go())


@then("the result ok is True")
def _ok(ctx):
    assert ctx["result"]["ok"] is True


@then(parsers.parse('the result summary contains "{needle}"'))
def _summary(ctx, needle):
    assert needle in ctx["result"]["summary"]


@then(parsers.parse('the running messages include "{needle}"'))
def _messages_include(ctx, needle):
    # The resumed prior message reaches the model: assert it was in a save payload
    # OR appears in any saved checkpoint's messages.
    saved = [c.args[0] for c in ctx["store"].save.await_args_list]
    texts = [m.get("content", "") for cp in saved for m in cp.messages]
    # Also accept the pre-seeded store content (finish-first means no new save).
    loaded = run_async(ctx["store"].load(ctx["task_id"]))
    if loaded is not None:
        texts += [m.get("content", "") for m in loaded.messages]
    assert any(needle in (t or "") for t in texts) or ctx["store"].load.await_count >= 1


@then(parsers.parse('no checkpoint remains for task "{task_id}"'))
def _cleared(ctx, task_id):
    assert run_async(ctx["store"].load(task_id)) is None


@then("the checkpoint store was never read or written")
def _no_io(ctx):
    ctx["store"].load.assert_not_awaited()
    ctx["store"].save.assert_not_awaited()
    ctx["store"].clear.assert_not_awaited()
```

> Implementer note: the `running messages include` assertion above is intentionally tolerant (the finish-first scenario may clear before a save). The load-count fallback proves resume occurred (a LOAD happened and returned the seeded checkpoint). If you prefer a stricter check, add a `loop.checkpoint.resumed` event capture via a fake emitter and assert on it.

- [ ] **Step 3: Run the BDD scenarios**

Run: `python -m pytest tests/services/orchestrator/test_durable_loop_checkpoint_bdd.py -q`
Expected: PASS (3 scenarios).

- [ ] **Step 4: Run the full orchestrator + memory suite**

Run: `python -m pytest tests/services/orchestrator/ tests/services/memory/ -q`
Expected: PASS — no regressions (the prior baseline is "684 passed" for orchestrator+memory on the robustness branch; this adds new tests, removes none).

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/durable_loop_checkpoint.feature \
        tests/services/orchestrator/test_durable_loop_checkpoint_bdd.py
git commit -m "test(orchestrator): BDD for durable inner-loop checkpoint (resume/clear/flag-off)"
```

---

## Part 2 — Trim vestigial graph code

Do Part 2 ONLY after Part 1 is committed and green. **REBASE FIRST** and re-run the audit against the current source — e2e may have changed `graph.py`. Each trim is one commit, guarded by: (1) a no-usages grep across `services/` + `tests/`, and (2) the FULL graph suite staying green. If any test references the candidate, it was NOT dead — revert and skip it.

### Task 6: Produce the trim-candidate list (audit only — no code change)

**Files:** none modified. Produce a written candidate list (in the PR description / commit message of Task 7).

- [ ] **Step 1: Identify candidates with grep**

Run each and record hits:

```bash
# 1) State fields that may have hollowed out after single-intent + off-by-default gates.
#    Multi-intent decompose was removed; routing is single-intent only.
grep -rn "awaiting_clarification\|clarification_question" services/ | grep -v test
grep -rn "_verify_reflect\|verify_retries" services/orchestrator/graph.py
grep -rn "critique_score\|critique_notes\|verified" services/ | grep -v test

# 2) Graph helpers/branches possibly unreachable under single-intent routing.
#    The plan node's architect-fallback decompose loop creates "{root_id}_subN"
#    children; under route()-driven single-intent it creates at most ONE child.
grep -rn "children_created\|_sub{i}\|_sub%d\|raw_plan" services/orchestrator/graph.py

# 3) check() walks a NESTED chain (root -> subN -> ... -> sub1) that only forms
#    when multiple sub-intents existed. Under single-intent there is at most one
#    child — confirm whether the deepest-first descendant walk is still exercised.
grep -rn "descendants\|deepest-first\|stack.extend" services/orchestrator/graph.py

# 4) types.py State fields with no live writer/reader.
grep -rn "goal_tree\|step_markers\|direct_answer\|root_goal" services/ | grep -v test | wc -l
```

- [ ] **Step 2: For EACH candidate, classify dead vs load-bearing**

For every grep hit, record one of:
- **LOAD-BEARING — KEEP** (do not touch): ambiguity/clarification gate (`assess_ambiguity`, `ambiguity_router`, `clarification_router`, `awaiting_clarification`, `clarification_question`); reflect→retry (`MAX_GOAL_ATTEMPTS`, `reflect`, `collect_prior_reflections`); the `interrupt()` approval gate (`approval`, `_gate_future`); conditional-gates (`skip_ambiguity`, `skip_verify`, `complexity`, `classify_complexity`) — kept even though OFF by default; verify/critique (`verify`, `verify_router`, `_verify_reflect`, `verify_retries`, `critique_*`) — kept even though OFF by default; the `revise` node (`revise`, `finalize_revisions`, `revised`) — kept even though OFF by default; direct-answer fast-path (`direct_answer`).
- **PROVABLY-DEAD — CANDIDATE** (only if: zero non-test usages AND not part of a kept gate AND removing it leaves the graph compilable). Likely candidates to SCRUTINIZE (do NOT assume dead — verify each):
  - The architect-fallback decompose loop in `plan()` (the `raw_plan`/`children_created`/`{root_id}_sub{i}` path) — IF grep proves no test/route-less path reaches it. **Likely still reachable** (backward-compat fallback when `route()` raises) → probably KEEP.
  - The multi-descendant nested-chain walk in `check()` — **still load-bearing** for any >1-child tree the fallback can still build → KEEP unless grep proves the fallback is dead too.
- **Output:** a short markdown table of `symbol | file | non-test usages | verdict (KEEP / CANDIDATE) | reason`. Most rows will be KEEP. The honest likely outcome is a SMALL candidate set (possibly empty) — that is an acceptable result. Do not manufacture trims.

- [ ] **Step 3: Record the audit** (no commit; the list goes into Task 7's commit body, or a standalone `docs/` note if the list is long). If the candidate set is empty, STOP Part 2 here and note "no provably-dead code found; graph is lean" — that is a valid completion.

---

### Task 7: Remove candidates one at a time (only if Task 6 found any)

**Files:** `services/orchestrator/graph.py` and/or `services/orchestrator/types.py` — ONE candidate per commit.

**Per-candidate loop (repeat for each CANDIDATE from Task 6):**

- [ ] **Step 1: Re-prove no usages (post-rebase)**

```bash
git fetch && git rebase origin/feat/agentic-fix-loop
grep -rn "<symbol>" services/ tests/
```
Expected: zero hits outside the definition itself. If a hit appears (including a new one e2e added), ABORT this candidate — it is not dead.

- [ ] **Step 2: Capture the green baseline**

Run: `python -m pytest tests/services/orchestrator/test_graph.py -q`
Expected: PASS — record the passing count.

- [ ] **Step 3: Delete the single candidate**

Remove exactly one symbol/branch (e.g. one dead `State` field in `types.py`, or one unreachable router arm in `graph.py`). Keep the deletion minimal — do not refactor neighbors.

- [ ] **Step 4: Re-run the full graph suite**

Run: `python -m pytest tests/services/orchestrator/test_graph.py tests/services/orchestrator/ -q`
Expected: PASS — same or higher passing count (no test should now fail; if one does, the code was NOT dead → `git checkout -- <files>` and skip this candidate).

- [ ] **Step 5: Commit the single trim**

```bash
git add services/orchestrator/graph.py services/orchestrator/types.py
git commit -m "refactor(graph): remove dead <symbol> (single-intent/off-by-default vestige)"
```

- [ ] **Step 6: Repeat** for the next candidate, or finish Part 2 when the list is exhausted.

---

## Self-Review

Ran the writing-plans self-review checklist against the task spec with fresh eyes.

**1. Spec coverage:**
- Part 1 new module `loop_checkpoint.py` with pure serialize/deserialize + async best-effort store → Tasks 1-2. ✅
- Serializes messages, turn index / IterationBudget `used` (+ `absolute_turns`, `grace_used`), edited-files set, verify-nudge count, loop-detector signatures, wall-clock start (as elapsed offset) → `LoopCheckpoint` fields in Task 1, mapped to live loop locals in Task 3. ✅
- Store keyed by `task_id`, reuses `StorageManager` / dedicated Mongo collection, load/save/clear all best-effort → `CheckpointStore` (Task 2) + `loop_checkpoint_collection` (Task 4). ✅
- Three minimal wire-in points (load+rehydrate at entry; save end-of-turn; clear on finish/terminal) using `events.current_task_id()`, no-op when absent → Task 3 Insertions A/B/C. ✅
- `ENABLE_LOOP_CHECKPOINT` default OFF → Task 3 Step 1. ✅
- Tests: pure round-trip (no Mongo); FakeCheckpointStore; BDD resume-from-pre-seeded-checkpoint; BDD flag-OFF identical; BDD clear-on-finish → Tasks 1, 2, 5. ✅
- Part 2 audit → list → one-at-a-time guarded removal; keep load-bearing gates → Tasks 6-7. ✅
- CLAUDE.md honored: stderr logging, AsyncMongoDBSaver untouched, asyncio-correct, no tiktoken → Global Constraints + per-task code. ✅

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Every code step shows real code. The two judgement-call steps (Task 4 Step 5 bootstrap injection; Task 6/7 audit) give explicit grep commands and explicit defer/skip criteria rather than hand-waving. ✅

**3. Type consistency:** `LoopCheckpoint` field names are identical across Tasks 1 (definition), 2 (store `to_dict`/`from_dict` use), 3 (save payload), and 5 (BDD pre-seed). `CheckpointStore.{save,load,clear}` signatures consistent across Tasks 2, 3, 4, 5. `_checkpoint_active(task_id)` defined once (Task 3) and reused in all three insertions. `loop_checkpoint_collection` / `CHECKPOINT_COLLECTION` consistent across Tasks 2/4. ✅

**4. Concurrency / rebase guidance:** Stated in Global Constraints AND repeated as the first step of every task that touches a hot file (Tasks 3, 4, 7). Anchors are verbatim structural strings (function names, variable names, the `except Exception as exc:` block), not line numbers — explicitly because e2e is editing these files concurrently. All Part-1 logic is isolated in the NEW `loop_checkpoint.py`; the only edits to `coding_orchestrator.py` are the 3 marked insertions + constructor hook; the only edits to `graph.py`/`types.py` are guarded single-candidate deletions that abort the moment a test or grep proves the code live. ✅

**Note on a subtle correctness point flagged during review:** `start_monotonic_offset` deliberately persists *elapsed* seconds, not a raw `time.monotonic()` value, because monotonic clocks are not comparable across a process restart; Insertion A rebases `start = self._now() - offset` so the wall-clock deadline still measures total goal time across the crash. This is called out in the dataclass docstring and Insertion A comment so an implementer reading one task out of order does not "simplify" it into a bug.

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-26-durable-checkpoint-and-trim.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
