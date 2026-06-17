# services/orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `services/orchestrator/` — the Python LangGraph orchestrator for Labmate that runs a Plan-Execute-Monitor + Reflexion loop with Gemma 4 31B as architect brain and Qwen2.5-Coder-32B as editor.

**Architecture:** LangGraph `StateGraph` with `AsyncMongoDBSaver` checkpointer. All agent state lives in a single JSON-serializable `State` TypedDict (Goal Tree + step markers). Parallel sub-task fan-out uses `asyncio.TaskGroup` with `graphlib.TopologicalSorter` for cycle detection. Per-node thinking budget passed via `extra_body={"thinking_budget_tokens": N}` to llama.cpp-served Gemma 4 31B.

**Tech Stack:** Python 3.11+, `langgraph >=0.2`, `langgraph-checkpoint-mongodb`, `litellm >=1.40`, `aiolimiter >=1.1`, `tenacity >=8.2`, `transformers >=4.47`, `graphlib` (stdlib), `asyncio` (stdlib), `pytest >=8.0`, `pytest-asyncio >=0.23`, `unittest.mock` (stdlib)

---

## File Map

```
services/orchestrator/
  __init__.py                     # re-exports CodingOrchestrator, build_graph
  types.py                        # Status, Goal TypedDict, State TypedDict, helpers
  coding_orchestrator.py          # SubTask, Result, TokenBudget, AsyncOrchestrator, CodingOrchestrator
  graph.py                        # make_nodes(), router(), build_graph()
  requirements.txt                # pinned deps for this service

tests/services/orchestrator/
  __init__.py                     # empty
  conftest.py                     # pytest mark registration + shared fixtures
  test_types.py                   # types.py unit tests (all @pytest.mark.mocked)
  test_coding_orchestrator.py     # coding_orchestrator.py unit tests (@pytest.mark.mocked)
  test_graph.py                   # graph.py unit tests (@pytest.mark.mocked)
```

---

## Critical Rules (Do Not Violate)

These are non-negotiable per the project CLAUDE.md and spec:

1. **Never use `tiktoken`** — use `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")` for token counting.
2. **`State` TypedDict must be 100% JSON-serializable** — no Python objects, no DB clients, no coroutines. Every value must survive `json.dumps()` / `json.loads()`.
3. **`asyncio.TaskGroup` for all fan-out** — never bare `asyncio.gather()` or `create_task()` at the fan-out site.
4. **Semaphore acquired inside the leaf worker** — never at the fan-out call site (deadlock risk).
5. **Always re-raise `asyncio.CancelledError`** — never swallow it (breaks TaskGroup + asyncio.timeout).
6. **`step_markers` idempotency guard** — write `"started"` before side effects, `"completed"` after; short-circuit if already `"completed"`.
7. **`thinking_budget_tokens` via `extra_body`** — high budget (2000–4000) for plan/reflect/aggregate nodes; `0` for tool-dispatch nodes.
8. **`graphlib.TopologicalSorter.prepare()`** — call before any worker is spawned to detect cycles early.
9. **Tests mark `@pytest.mark.mocked`** for no-GPU tests; mock `litellm.acompletion` with `unittest.mock.AsyncMock`.
10. **Never `asyncio.run()` inside a coroutine** — raises RuntimeError on a running loop.

---

## Task 1: Package Scaffold and Requirements

**Files:**
- Create: `services/orchestrator/__init__.py`
- Create: `services/orchestrator/requirements.txt`
- Create: `tests/services/orchestrator/__init__.py`
- Create: `tests/services/orchestrator/conftest.py`

No tests needed for scaffolding — these are empty/config files.

- [ ] **Step 1: Create `services/orchestrator/__init__.py`** (empty — exports added in Task 7)

```python
```

- [ ] **Step 2: Create `services/orchestrator/requirements.txt`**

```
langgraph>=0.2
langgraph-checkpoint-mongodb
litellm>=1.40
pydantic>=2.0
aiolimiter>=1.1
tenacity>=8.2
transformers>=4.47
networkx>=3.0
docker>=7.0
redis>=5.0
pymongo>=4.0
chromadb>=0.5
GitPython>=3.1
anyio>=4.0
pytest>=8.0
pytest-asyncio>=0.23
respx>=0.21
```

- [ ] **Step 3: Create `tests/services/orchestrator/__init__.py`** (empty)

```python
```

- [ ] **Step 4: Create `tests/services/orchestrator/conftest.py`**

```python
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "mocked: no GPU required, all external calls mocked")
    config.addinivalue_line("markers", "live: requires running llama.cpp inference server")
```

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/ tests/services/orchestrator/
git commit -m "feat(orchestrator): scaffold package and test directories"
```

---

## Task 2: `types.py` — State, Goal, Status, and Goal Tree Helpers

**Files:**
- Create: `services/orchestrator/types.py`
- Create: `tests/services/orchestrator/test_types.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_types.py
from __future__ import annotations
import json
import pytest

from services.orchestrator.types import (
    Status, Goal, State, create_goal, update_status, get_ready_goals, now_iso,
)


@pytest.mark.mocked
class TestStatus:
    def test_all_values_are_strings(self):
        for s in Status:
            assert isinstance(s.value, str)

    def test_required_values_present(self):
        expected = {"PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "BLOCKED", "AWAITING_APPROVAL"}
        assert {s.value for s in Status} == expected


@pytest.mark.mocked
class TestCreateGoal:
    def test_root_goal_has_correct_fields(self):
        tree: dict = {}
        create_goal(tree, "root", None, "Implement feature X")
        g = tree["root"]
        assert g["id"] == "root"
        assert g["parent_id"] is None
        assert g["children"] == []
        assert g["description"] == "Implement feature X"
        assert g["status"] == Status.PENDING
        assert g["result"] is None
        assert g["error"] is None
        assert g["attempts"] == 0
        assert g["started_at"] is None
        assert g["updated_at"] is None

    def test_child_goal_is_added_to_parent_children(self):
        tree: dict = {}
        create_goal(tree, "root", None, "Root task")
        create_goal(tree, "child1", "root", "Sub-task 1")
        assert "child1" in tree["root"]["children"]

    def test_create_goal_returns_tree(self):
        tree: dict = {}
        result = create_goal(tree, "g1", None, "desc")
        assert result is tree

    def test_create_goal_missing_parent_does_not_raise(self):
        tree: dict = {}
        create_goal(tree, "orphan", "nonexistent", "orphan task")
        assert "orphan" in tree


@pytest.mark.mocked
class TestUpdateStatus:
    def test_updates_status_field(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        update_status(tree, "g1", Status.IN_PROGRESS)
        assert tree["g1"]["status"] == Status.IN_PROGRESS

    def test_sets_updated_at_iso_string(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        update_status(tree, "g1", Status.COMPLETED)
        updated = tree["g1"]["updated_at"]
        assert isinstance(updated, str)
        assert updated.endswith("Z")

    def test_extra_kwargs_are_stored(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        update_status(tree, "g1", Status.COMPLETED, result="output text", error=None)
        assert tree["g1"]["result"] == "output text"
        assert tree["g1"]["error"] is None

    def test_returns_tree(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        result = update_status(tree, "g1", Status.COMPLETED)
        assert result is tree


@pytest.mark.mocked
class TestGetReadyGoals:
    def test_pending_leaf_is_ready(self):
        tree: dict = {}
        create_goal(tree, "root", None, "task")
        ready = get_ready_goals(tree)
        assert len(ready) == 1
        assert ready[0]["id"] == "root"

    def test_goal_with_pending_child_is_not_ready(self):
        tree: dict = {}
        create_goal(tree, "root", None, "parent task")
        create_goal(tree, "child1", "root", "child task")
        ready = get_ready_goals(tree)
        assert all(g["id"] != "root" for g in ready)

    def test_goal_with_all_completed_children_is_ready(self):
        tree: dict = {}
        create_goal(tree, "root", None, "parent task")
        create_goal(tree, "child1", "root", "child task")
        update_status(tree, "child1", Status.COMPLETED)
        ready = get_ready_goals(tree)
        ids = [g["id"] for g in ready]
        assert "root" in ids

    def test_in_progress_goal_is_not_ready(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        update_status(tree, "g1", Status.IN_PROGRESS)
        assert get_ready_goals(tree) == []

    def test_multiple_independent_pending_leaves_all_returned(self):
        tree: dict = {}
        create_goal(tree, "root", None, "parent")
        create_goal(tree, "c1", "root", "child 1")
        create_goal(tree, "c2", "root", "child 2")
        ready = get_ready_goals(tree)
        ids = {g["id"] for g in ready}
        assert ids == {"c1", "c2"}


@pytest.mark.mocked
class TestStateJsonSerializable:
    def test_state_survives_json_round_trip(self):
        tree: dict = {}
        create_goal(tree, "root", None, "task")
        state = {
            "session_id": "test-session-001",
            "goal_tree": tree,
            "current_goal_id": "root",
            "step_markers": {},
            "messages": [],
            "error": None,
        }
        serialized = json.dumps(state)
        restored = json.loads(serialized)
        assert restored["goal_tree"]["root"]["id"] == "root"
        assert restored["session_id"] == "test-session-001"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /path/to/gemma
python -m pytest tests/services/orchestrator/test_types.py -v --no-header 2>&1 | head -40
```

Expected: `ModuleNotFoundError: No module named 'services.orchestrator.types'`

- [ ] **Step 3: Implement `services/orchestrator/types.py`**

```python
# services/orchestrator/types.py
from __future__ import annotations

import datetime
from enum import Enum
from operator import add
from typing import Annotated, Optional, TypedDict


class Status(str, Enum):
    """All valid lifecycle states for a Goal node."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"


class Goal(TypedDict):
    """
    A single node in the Goal Tree. All values must be JSON-serializable —
    no Python objects, no datetimes (use ISO-8601 strings).
    """
    id: str
    parent_id: Optional[str]
    children: list[str]
    description: str
    status: str          # Status enum value; stored as str for JSON safety
    result: Optional[str]
    error: Optional[str]
    attempts: int
    started_at: Optional[str]    # ISO-8601
    updated_at: Optional[str]    # ISO-8601


class State(TypedDict):
    """
    The single JSON-serializable state object persisted by AsyncMongoDBSaver
    at every LangGraph super-step.

    RULE: Never store Python objects, coroutines, DB clients, or file handles
    here. Everything must survive json.dumps() / json.loads() round-trips.
    """
    session_id: str
    goal_tree: dict[str, Goal]        # id -> Goal; the live plan
    current_goal_id: Optional[str]
    step_markers: dict[str, str]      # step_id -> 'started' | 'completed'
    messages: Annotated[list, add]    # reducer-safe; parallel nodes may append
    error: Optional[str]


def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def create_goal(
    tree: dict[str, Goal],
    gid: str,
    parent_id: Optional[str],
    desc: str,
) -> dict[str, Goal]:
    """Insert a new PENDING goal and wire it into its parent's children list."""
    tree[gid] = Goal(
        id=gid,
        parent_id=parent_id,
        children=[],
        description=desc,
        status=Status.PENDING,
        result=None,
        error=None,
        attempts=0,
        started_at=None,
        updated_at=None,
    )
    if parent_id and parent_id in tree:
        tree[parent_id]["children"].append(gid)
    return tree


def update_status(
    tree: dict[str, Goal],
    gid: str,
    status: Status,
    **kwargs,
) -> dict[str, Goal]:
    """Transition a goal to a new status and optionally set result/error/started_at/etc."""
    g = tree[gid]
    g["status"] = status
    g["updated_at"] = now_iso()
    for k, v in kwargs.items():
        g[k] = v  # type: ignore[literal-required]
    return tree


def get_ready_goals(tree: dict[str, Goal]) -> list[Goal]:
    """
    Return all PENDING goals whose children are all COMPLETED (or have none).
    These are eligible for immediate execution and may be parallelised.
    """
    return [
        g for g in tree.values()
        if g["status"] == Status.PENDING
        and all(tree[c]["status"] == Status.COMPLETED for c in g["children"])
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/test_types.py -v --no-header
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/types.py tests/services/orchestrator/test_types.py
git commit -m "feat(orchestrator): add types.py with State, Goal, Status, and Goal Tree helpers"
```

---

## Task 3: `TokenBudget` Class

**Files:**
- Create: `services/orchestrator/coding_orchestrator.py` (partial — TokenBudget only)
- Create: `tests/services/orchestrator/test_coding_orchestrator.py` (partial)

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_coding_orchestrator.py
from __future__ import annotations
import asyncio
import pytest

from services.orchestrator.coding_orchestrator import TokenBudget


@pytest.mark.mocked
class TestTokenBudget:
    def test_init_applies_80_percent_margin(self):
        budget = TokenBudget(total=100_000)
        assert budget.remaining == 80_000

    def test_init_custom_margin(self):
        budget = TokenBudget(total=100_000, margin=0.5)
        assert budget.remaining == 50_000

    @pytest.mark.asyncio
    async def test_reserve_success_decrements_remaining(self):
        budget = TokenBudget(total=100_000)
        result = await budget.reserve(10_000)
        assert result is True
        assert budget.remaining == 70_000

    @pytest.mark.asyncio
    async def test_reserve_fails_when_exhausted(self):
        budget = TokenBudget(total=1_000)
        result = await budget.reserve(900)  # reserves 900 of 800 available (80%)
        assert result is False

    @pytest.mark.asyncio
    async def test_refund_restores_balance(self):
        budget = TokenBudget(total=100_000)
        await budget.reserve(10_000)
        await budget.refund(10_000)
        assert budget.remaining == 80_000

    @pytest.mark.asyncio
    async def test_concurrent_reserves_are_serialized(self):
        budget = TokenBudget(total=10_000)
        # Each reserve = 4000 tokens; only one of three can succeed with 8000 available
        results = await asyncio.gather(
            budget.reserve(4_000),
            budget.reserve(4_000),
            budget.reserve(4_000),
        )
        # Exactly two reserves should succeed (4000 + 4000 = 8000 = full margin)
        assert results.count(True) == 2
        assert budget.remaining == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -v --no-header 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'services.orchestrator.coding_orchestrator'`

- [ ] **Step 3: Implement `TokenBudget` in `services/orchestrator/coding_orchestrator.py`**

```python
# services/orchestrator/coding_orchestrator.py
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from typing import Any

import litellm
from aiolimiter import AsyncLimiter

from .types import Goal, State, Status, get_ready_goals, update_status, now_iso


# ---------------------------------------------------------------------------
# Sub-agent result container
# ---------------------------------------------------------------------------

@dataclass
class SubTask:
    """A parallel work unit derived from a ready Goal."""
    id: str
    prompt: str
    deps: set[str] = field(default_factory=set)
    est_tokens: int = 0


@dataclass
class Result:
    """
    Condensed handback from a parallel sub-agent.
    NEVER return the raw transcript — it would overflow the orchestrator context.
    """
    id: str
    summary: str
    artifacts: dict = field(default_factory=dict)
    ok: bool = True


# ---------------------------------------------------------------------------
# Token budget (shared across concurrent workers)
# ---------------------------------------------------------------------------

class TokenBudget:
    """
    Thread-safe token budget with a configurable safety margin.
    All reserve/refund operations are serialised by an asyncio.Lock.
    """

    def __init__(self, total: int, margin: float = 0.8) -> None:
        self.remaining = int(total * margin)
        self._lock = asyncio.Lock()

    async def reserve(self, n: int) -> bool:
        async with self._lock:
            if n > self.remaining:
                return False
            self.remaining -= n
            return True

    async def refund(self, n: int) -> None:
        async with self._lock:
            self.remaining += n
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -v --no-header
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): add TokenBudget with margin and async lock serialization"
```

---

## Task 4: `AsyncOrchestrator` Class

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (append AsyncOrchestrator)
- Modify: `tests/services/orchestrator/test_coding_orchestrator.py` (append AsyncOrchestrator tests)

- [ ] **Step 1: Write the failing tests** (append to `test_coding_orchestrator.py`)

```python
# Append to tests/services/orchestrator/test_coding_orchestrator.py

import graphlib
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.coding_orchestrator import AsyncOrchestrator, Result, SubTask


def _make_mock_result(task_id: str, summary: str = "done") -> MagicMock:
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = summary
    return r


@pytest.mark.mocked
class TestAsyncOrchestrator:
    @pytest.mark.asyncio
    async def test_raises_cycle_error_before_any_spawn(self):
        orch = AsyncOrchestrator()
        # A -> B -> A forms a cycle; est_tokens=0 so budget is not the blocker
        goals = [
            {"id": "A", "description": "task A", "status": "PENDING", "children": ["B"]},
            {"id": "B", "description": "task B", "status": "PENDING", "children": ["A"]},
        ]
        # Override subtasks to have deps
        with patch.object(orch, "_run_worker", new_callable=AsyncMock) as mock_worker:
            with pytest.raises(graphlib.CycleError):
                # Manually create subtasks with cyclic deps to trigger the check
                subtasks = [
                    SubTask(id="A", prompt="task A", deps={"B"}),
                    SubTask(id="B", prompt="task B", deps={"A"}),
                ]
                dep_graph = {t.id: t.deps for t in subtasks}
                ts = graphlib.TopologicalSorter(dep_graph)
                ts.prepare()  # CycleError raised here
            mock_worker.assert_not_called()

    @pytest.mark.asyncio
    async def test_parallel_dispatch_happy_path(self):
        orch = AsyncOrchestrator()

        async def fake_call_qwen(t: SubTask) -> str:
            return f"result for {t.id}"

        with patch.object(orch, "_call_qwen_worker", side_effect=fake_call_qwen):
            goals = [
                {"id": "g1", "description": "task 1", "children": [], "status": "PENDING"},
                {"id": "g2", "description": "task 2", "children": [], "status": "PENDING"},
            ]
            results = await orch.plan_and_dispatch(goals)
            assert len(results) == 2
            ids = {r.id for r in results}
            assert ids == {"g1", "g2"}
            assert all(r.ok for r in results)

    @pytest.mark.asyncio
    async def test_cancelled_error_is_reraised(self):
        orch = AsyncOrchestrator(budget=10_000)
        t = SubTask(id="x", prompt="test", est_tokens=100)

        async def raise_cancel(_):
            raise asyncio.CancelledError()

        with patch.object(orch, "_call_qwen_worker", side_effect=raise_cancel):
            with pytest.raises(asyncio.CancelledError):
                await orch._run_worker(t)

    @pytest.mark.asyncio
    async def test_aggregate_calls_architect_with_2000_budget(self):
        orch = AsyncOrchestrator()
        results = [
            Result(id="r1", summary="proposal 1", ok=True),
            Result(id="r2", summary="proposal 2", ok=True),
        ]
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "synthesized result"

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
            result = await orch.aggregate("some task", results)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 2000
            assert result.id == "aggregated"
            assert "synthesized result" in result.summary
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/orchestrator/test_coding_orchestrator.py::TestAsyncOrchestrator -v --no-header 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'AsyncOrchestrator'`

- [ ] **Step 3: Implement `AsyncOrchestrator` in `coding_orchestrator.py`** (append after TokenBudget)

```python
# ---------------------------------------------------------------------------
# Async parallel orchestrator (called by the StateGraph execute node)
# ---------------------------------------------------------------------------

class AsyncOrchestrator:
    """
    Executes a set of independent SubTasks in parallel over a validated
    dependency DAG. Used by the LangGraph 'execute' node when
    get_ready_goals() returns multiple PENDING leaves.

    Concurrency is dual-gated:
      - asyncio.Semaphore: bounds in-flight workers to GPU KV-cache capacity.
      - AsyncLimiter (RPM + TPM): leaky-bucket rate limits via aiolimiter.
      - TokenBudget: global remaining-tokens guard (80% safety margin).
    """

    def __init__(
        self,
        max_inflight: int = 4,
        rpm: int = 60,
        tpm: int = 90_000,
        budget: int = 400_000,
        qwen_api_base: str = "http://localhost:8001/v1",
        gemma_api_base: str = "http://localhost:8000/v1",
    ) -> None:
        self.sem = asyncio.Semaphore(max_inflight)
        self.rpm_limiter = AsyncLimiter(rpm, 60)
        self.tpm_limiter = AsyncLimiter(tpm, 60)
        self.budget = TokenBudget(budget)
        self.results: dict[str, Result] = {}
        self._qwen_base = qwen_api_base
        self._gemma_base = gemma_api_base

    async def plan_and_dispatch(self, ready_goals: list[dict]) -> list[Result]:
        """
        Validate the ready set as a dependency DAG, then fan-out workers with
        asyncio.TaskGroup for structured concurrency. Returns condensed Results.
        """
        import graphlib

        subtasks = [
            SubTask(id=g["id"], prompt=g["description"], est_tokens=512)
            for g in ready_goals
        ]
        dep_graph: dict[str, set[str]] = {t.id: t.deps for t in subtasks}

        ts = graphlib.TopologicalSorter(dep_graph)
        ts.prepare()  # raises CycleError immediately — fail fast, no spawn

        index = {t.id: t for t in subtasks}
        self.results = {}

        async with asyncio.TaskGroup() as tg:
            running: dict[str, asyncio.Task] = {}
            while ts.is_active():
                for tid in ts.get_ready():
                    if tid not in running:
                        running[tid] = tg.create_task(self._run_worker(index[tid]))
                await asyncio.sleep(0)
                for tid, task in list(running.items()):
                    if task.done() and not task.cancelled():
                        ts.done(tid)

        return list(self.results.values())

    async def _run_worker(self, t: SubTask) -> str:
        """
        Execute a single sub-task worker.
        1. Reserve token budget (wait in loop, never dispatch on bust).
        2. Acquire Semaphore inside the worker (never at the fan-out site).
        3. Rate-limit then call Qwen2.5-Coder-32B.
        4. Condense and store the result.
        5. Re-raise CancelledError — NEVER swallow it.
        """
        while not await self.budget.reserve(t.est_tokens):
            await asyncio.sleep(0.5)

        try:
            async with self.sem:
                async with self.rpm_limiter:
                    async with self.tpm_limiter:
                        raw = await self._call_qwen_worker(t)
                        self.results[t.id] = self._condense(t.id, raw)
        except asyncio.CancelledError:
            await self.budget.refund(t.est_tokens)
            raise  # never swallow — keeps TaskGroup correct
        except Exception:
            await self.budget.refund(t.est_tokens)
            self.results[t.id] = Result(id=t.id, summary="worker failed", ok=False)
            raise

        return t.id

    async def _call_qwen_worker(self, t: SubTask) -> str:
        """Route to Qwen2.5-Coder-32B (specialist editor) via litellm."""
        r = await litellm.acompletion(
            model="openai/qwen2.5-coder-32b",
            api_base=self._qwen_base,
            messages=[{"role": "user", "content": t.prompt}],
        )
        return r.choices[0].message.content

    def _condense(self, tid: str, raw: str) -> Result:
        """Strip the raw transcript to summary + artifact paths."""
        return Result(id=tid, summary=raw[:2000], ok=True)

    async def aggregate(self, task: str, results: list[Result]) -> Result:
        """MoA-style aggregation: Gemma 4 31B synthesises all candidate results."""
        candidates = "\n\n".join(
            f"[{r.id}] {'OK' if r.ok else 'FAILED'}: {r.summary}" for r in results
        )
        prompt = (
            f"Task: {task}\n\nCandidate results:\n{candidates}\n\n"
            "Synthesize the best unified result."
        )
        r = await litellm.acompletion(
            model="openai/gemma-4-31b",
            api_base=self._gemma_base,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking_budget_tokens": 2000},
        )
        return Result(id="aggregated", summary=r.choices[0].message.content)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -v --no-header
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): add AsyncOrchestrator with DAG fan-out, token budget, and MoA aggregation"
```

---

## Task 5: `CodingOrchestrator` Class

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (append CodingOrchestrator)
- Modify: `tests/services/orchestrator/test_coding_orchestrator.py` (append CodingOrchestrator tests)

- [ ] **Step 1: Write the failing tests** (append to `test_coding_orchestrator.py`)

```python
# Append to tests/services/orchestrator/test_coding_orchestrator.py

from services.orchestrator.coding_orchestrator import CodingOrchestrator


@pytest.mark.mocked
class TestCodingOrchestrator:
    def _make_orch(self) -> CodingOrchestrator:
        return CodingOrchestrator(
            graph=MagicMock(),
            workspace_path="/tmp/workspace",
            docker_container="lm-sandbox",
        )

    @pytest.mark.asyncio
    async def test_architect_passes_thinking_budget_in_extra_body(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "here is the plan"

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
            result = await orch.architect("decompose this", thinking_budget=3000)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 3000
            assert call_kwargs["model"] == "openai/gemma-4-31b"
            assert result == "here is the plan"

    @pytest.mark.asyncio
    async def test_architect_tool_dispatch_uses_zero_budget(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "tool result"

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
            await orch.architect("route this tool call", thinking_budget=0)
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 0

    @pytest.mark.asyncio
    async def test_editor_calls_qwen(self):
        orch = self._make_orch()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "patched code"

        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_complete:
            result = await orch.editor("fix this bug")
            call_kwargs = mock_complete.call_args.kwargs
            assert call_kwargs["model"] == "openai/qwen2.5-coder-32b"
            assert result == "patched code"

    def test_is_stuck_returns_false_when_not_repeated(self):
        orch = self._make_orch()
        assert orch.is_stuck("action_a") is False
        assert orch.is_stuck("action_b") is False
        assert orch.is_stuck("action_a") is False

    def test_is_stuck_returns_true_after_n_identical_actions(self):
        orch = self._make_orch()
        orch.is_stuck("same_action")
        orch.is_stuck("same_action")
        result = orch.is_stuck("same_action")
        assert result is True

    def test_is_stuck_resets_on_different_action(self):
        orch = self._make_orch()
        orch.is_stuck("same_action")
        orch.is_stuck("same_action")
        orch.is_stuck("different_action")  # breaks the streak
        assert orch.is_stuck("same_action") is False

    def test_execute_in_sandbox_success(self):
        orch = self._make_orch()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="test output", stderr="", returncode=0
            )
            result = orch.execute_in_sandbox("echo hello")
            assert result["ok"] is True
            assert result["stdout"] == "test output"
            assert result["exit_code"] == 0
            cmd = mock_run.call_args[0][0]
            assert "docker" in cmd
            assert "exec" in cmd
            assert "lm-sandbox" in cmd

    def test_execute_in_sandbox_failure(self):
        orch = self._make_orch()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="", stderr="command not found", returncode=1
            )
            result = orch.execute_in_sandbox("bad_command")
            assert result["ok"] is False
            assert result["exit_code"] == 1

    def test_git_checkpoint_calls_git_add_and_commit(self):
        orch = self._make_orch()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            orch.git_checkpoint("step 3: fixed tests")
            calls = [c[0][0] for c in mock_run.call_args_list]
            assert any("add" in c for c in calls)
            assert any("commit" in c for c in calls)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/orchestrator/test_coding_orchestrator.py::TestCodingOrchestrator -v --no-header 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'CodingOrchestrator'`

- [ ] **Step 3: Implement `CodingOrchestrator` in `coding_orchestrator.py`** (append at end of file)

```python
# ---------------------------------------------------------------------------
# Main coding orchestrator (wraps the StateGraph entry point)
# ---------------------------------------------------------------------------

class CodingOrchestrator:
    """
    Top-level entry point. Wraps the LangGraph StateGraph with convenience
    methods and the Gemma 4 architect / Qwen2.5-Coder-32B editor routing.

    Lifecycle:
      run_task() -> graph.ainvoke() -> plan -> execute -> check -> [reflect | END]

    Crash recovery: re-invoke run_task() with the same session_id.
    The AsyncMongoDBSaver will load the latest checkpoint and resume.
    """

    def __init__(
        self,
        graph,
        workspace_path: str,
        docker_container: str,
        gemma_api_base: str = "http://localhost:8000/v1",
        qwen_api_base: str = "http://localhost:8001/v1",
        max_iter: int = 10,
        stuck_n: int = 3,
    ) -> None:
        self.graph = graph
        self.workspace = workspace_path
        self.container = docker_container
        self._gemma_base = gemma_api_base
        self._qwen_base = qwen_api_base
        self.max_iter = max_iter
        self.stuck_n = stuck_n
        self._recent_actions: list[str] = []

    async def run_task(self, task: str, session_id: str) -> dict:
        """
        Entry point. Pass the same session_id to resume after a crash.
        Returns the final State dict.
        """
        from .types import create_goal

        initial: State = {
            "session_id": session_id,
            "goal_tree": create_goal({}, "root", None, task),
            "current_goal_id": "root",
            "step_markers": {},
            "messages": [],
            "error": None,
        }
        cfg = {"configurable": {"thread_id": session_id}}
        return await self.graph.ainvoke(initial, cfg)

    async def architect(self, prompt: str, thinking_budget: int = 3000) -> str:
        """
        Planning, self-reflection, aggregation -> Gemma 4 31B dense.

        thinking_budget controls per-request reasoning depth via llama.cpp's
        thinking_budget_tokens field (only honored when server started without
        --reasoning-budget flag). Use a large budget for planning/reflection
        nodes; pass thinking_budget=0 for fast tool-dispatch nodes.
        """
        r = await litellm.acompletion(
            model="openai/gemma-4-31b",
            api_base=self._gemma_base,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking_budget_tokens": thinking_budget},
        )
        return r.choices[0].message.content

    async def editor(self, prompt: str) -> str:
        """Code generation, file edits -> Qwen2.5-Coder-32B."""
        r = await litellm.acompletion(
            model="openai/qwen2.5-coder-32b",
            api_base=self._qwen_base,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.choices[0].message.content

    def is_stuck(self, action_key: str) -> bool:
        """
        Returns True if the last stuck_n actions are all identical.
        Triggers escalation from the inner ReAct loop to a fresh Plan-Execute pass.
        """
        self._recent_actions.append(action_key)
        self._recent_actions = self._recent_actions[-self.stuck_n:]
        return (
            len(self._recent_actions) == self.stuck_n
            and len(set(self._recent_actions)) == 1
        )

    def execute_in_sandbox(self, cmd: str, timeout: int = 60) -> dict:
        """
        Run a shell command inside the Docker container.
        The host filesystem is NEVER mounted writable; only the container
        is affected by any destructive command.
        """
        proc = subprocess.run(
            ["docker", "exec", self.container, "bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "exit_code": proc.returncode,
            "ok": proc.returncode == 0,
        }

    def git_checkpoint(self, message: str) -> None:
        """
        Commit all workspace changes as a rollback-ladder checkpoint.
        Called after every successful file edit.
        """
        subprocess.run(["git", "-C", self.workspace, "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", self.workspace, "commit", "-m", message],
            check=True,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/test_coding_orchestrator.py -v --no-header
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "feat(orchestrator): add CodingOrchestrator with architect/editor routing, stuck detection, and sandbox execution"
```

---

## Task 6: `graph.py` — Nodes and Router

**Files:**
- Create: `services/orchestrator/graph.py` (nodes + router; `build_graph` added in Task 7)
- Create: `tests/services/orchestrator/test_graph.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_graph.py
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.types import (
    Status, State, create_goal, update_status, get_ready_goals,
)


def _make_state(**overrides) -> dict:
    tree: dict = {}
    create_goal(tree, "root", None, "top-level task")
    base = {
        "session_id": "test-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
    }
    base.update(overrides)
    return base


@pytest.mark.mocked
class TestRouter:
    def test_router_returns_end_when_no_goals_present(self):
        from services.orchestrator.graph import router
        from langgraph.graph import END

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.COMPLETED)
        result = router(state)
        assert result == END

    def test_router_returns_end_when_current_goal_id_is_none(self):
        from services.orchestrator.graph import router
        from langgraph.graph import END

        state = _make_state(current_goal_id=None)
        result = router(state)
        assert result == END

    def test_router_returns_reflect_on_failed_goal_with_attempts_lt_3(self):
        from services.orchestrator.graph import router

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.FAILED)
        state["goal_tree"]["root"]["attempts"] = 1
        result = router(state)
        assert result == "reflect"

    def test_router_returns_end_on_failed_goal_at_max_attempts(self):
        from services.orchestrator.graph import router
        from langgraph.graph import END

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.FAILED)
        state["goal_tree"]["root"]["attempts"] = 3
        result = router(state)
        assert result == END

    def test_router_returns_approval_on_awaiting_approval(self):
        from services.orchestrator.graph import router

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.AWAITING_APPROVAL)
        result = router(state)
        assert result == "approval"

    def test_router_returns_execute_when_ready_goals_exist(self):
        from services.orchestrator.graph import router

        state = _make_state()
        # root goal is PENDING with no children — get_ready_goals returns it
        update_status(state["goal_tree"], "root", Status.COMPLETED)
        # add a new pending child
        create_goal(state["goal_tree"], "child1", "root", "pending work")
        result = router(state)
        assert result == "execute"


@pytest.mark.mocked
class TestPlanNode:
    @pytest.mark.asyncio
    async def test_plan_node_creates_child_goals_from_architect_response(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="Subtask A\nSubtask B\nSubtask C")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        plan_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta = await plan_node(state)

        tree = delta["goal_tree"]
        children = tree["root"]["children"]
        assert len(children) == 3
        # Children should have descriptions matching the lines
        descriptions = {tree[c]["description"] for c in children}
        assert "Subtask A" in descriptions
        assert "Subtask B" in descriptions
        assert "Subtask C" in descriptions

    @pytest.mark.asyncio
    async def test_plan_node_skips_empty_lines(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="Task 1\n\nTask 2\n")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        plan_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta = await plan_node(state)
        children = delta["goal_tree"]["root"]["children"]
        assert len(children) == 2


@pytest.mark.mocked
class TestExecuteNode:
    @pytest.mark.asyncio
    async def test_idempotency_guard_skips_completed_goal(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        state["step_markers"]["root"] = "completed"  # already done

        delta = await execute_node(state)
        assert delta == {}  # no state changes
        mock_orch.execute_in_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_node_calls_git_checkpoint_on_success(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.execute_in_sandbox.return_value = {
            "stdout": "Tests passed", "stderr": "", "exit_code": 0, "ok": True
        }
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        await execute_node(state)
        mock_orch.git_checkpoint.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_node_increments_attempts_on_failure(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.execute_in_sandbox.return_value = {
            "stdout": "", "stderr": "error", "exit_code": 1, "ok": False
        }
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        delta = await execute_node(state)
        tree = delta["goal_tree"]
        assert tree["root"]["attempts"] == 1
        assert tree["root"]["status"] == Status.FAILED

    @pytest.mark.asyncio
    async def test_execute_node_git_checkpoint_not_called_on_failure(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.execute_in_sandbox.return_value = {
            "stdout": "", "stderr": "fail", "exit_code": 2, "ok": False
        }
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        await execute_node(state)
        mock_orch.git_checkpoint.assert_not_called()


@pytest.mark.mocked
class TestReflectNode:
    @pytest.mark.asyncio
    async def test_reflect_resets_goal_to_pending_and_appends_message(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="do it differently next time")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        _, _, _, reflect_node, _ = make_nodes(mock_orch, mock_async_orch)

        state = _make_state()
        update_status(state["goal_tree"], "root", Status.FAILED, error="syntax error")
        state["goal_tree"]["root"]["attempts"] = 1

        delta = await reflect_node(state)
        assert delta["goal_tree"]["root"]["status"] == Status.PENDING
        assert len(delta["messages"]) == 1
        assert delta["messages"][0]["role"] == "reflection"
        assert "differently" in delta["messages"][0]["content"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/orchestrator/test_graph.py -v --no-header 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'services.orchestrator.graph'`

- [ ] **Step 3: Implement `services/orchestrator/graph.py`** (nodes + router; `build_graph` in next task)

```python
# services/orchestrator/graph.py
from __future__ import annotations

import asyncio
import os
from typing import Any

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from .types import State, Status, Goal, get_ready_goals, update_status, now_iso, create_goal
from .coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
# llama.cpp serves Gemma 4 31B on port 8000 (CUDA on RunPod, Metal on Mac Mini, Vulkan on AMD).
# On 48 GB (A6000 / Mac Mini): both servers co-reside.
# On 32 GB discrete GPU: run one server at a time; set QWEN_BASE = GEMMA_BASE for model-swap mode.
GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
QWEN_BASE  = os.getenv("QWEN_BASE",  "http://localhost:8001/v1")


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

def make_nodes(orch: CodingOrchestrator, async_orch: AsyncOrchestrator):
    """
    Factory that closes over the orchestrator instances so nodes are plain
    async functions (no class state on the graph itself).
    """

    async def plan(state: State) -> dict:
        """
        Decompose the current root goal into child Goals via a Gemma 4 architect call.
        Writes new Goal entries into goal_tree. Pure return-delta — no in-place mutation.
        """
        root_id = state["current_goal_id"]
        root_desc = state["goal_tree"][root_id]["description"]
        raw_plan = await orch.architect(
            f"Decompose this task into concrete subtasks (one per line):\n{root_desc}"
        )
        tree = dict(state["goal_tree"])
        import uuid
        for i, line in enumerate(raw_plan.strip().splitlines()):
            if line.strip():
                gid = f"{root_id}_sub{i}"
                create_goal(tree, gid, root_id, line.strip())
        return {"goal_tree": tree}

    async def execute_node(state: State) -> dict:
        """
        Execute the current PENDING goal. If multiple goals are ready in parallel,
        delegates to AsyncOrchestrator.plan_and_dispatch().
        Includes an idempotency guard via step_markers.
        """
        ready = get_ready_goals(state["goal_tree"])
        if not ready:
            return {}

        tree = dict(state["goal_tree"])
        markers = dict(state["step_markers"])

        if len(ready) > 1:
            results = await async_orch.plan_and_dispatch(ready)
            for r in results:
                gid = r.id
                markers[gid] = "completed"
                new_status = Status.COMPLETED if r.ok else Status.FAILED
                update_status(tree, gid, new_status, result=r.summary)
            return {"goal_tree": tree, "step_markers": markers}

        goal = ready[0]
        gid = goal["id"]

        # Idempotency guard: skip if already completed (crash-resume safety)
        if markers.get(gid) == "completed":
            return {}

        markers[gid] = "started"
        update_status(tree, gid, Status.IN_PROGRESS, started_at=now_iso())

        obs = orch.execute_in_sandbox(f"# execute: {goal['description']}")
        result_text = obs["stdout"] or obs["stderr"]

        if obs["ok"]:
            markers[gid] = "completed"
            update_status(tree, gid, Status.COMPLETED, result=result_text)
            orch.git_checkpoint(f"goal {gid}: {goal['description'][:60]}")
        else:
            g = tree[gid]
            g["attempts"] = g["attempts"] + 1
            update_status(tree, gid, Status.FAILED, error=result_text)

        return {
            "goal_tree": tree,
            "step_markers": markers,
            "current_goal_id": gid,
        }

    async def check(state: State) -> dict:
        """Validate the current goal's result and set final status if warranted."""
        return {}

    async def reflect(state: State) -> dict:
        """
        Reflexion: write a natural-language diagnosis to episodic memory.
        Conditions the next execute attempt on the stored reflection.
        """
        gid = state["current_goal_id"]
        goal = state["goal_tree"][gid]
        reflection = await orch.architect(
            f"The following subtask failed (attempt {goal['attempts']}):\n"
            f"Goal: {goal['description']}\n"
            f"Error: {goal['error']}\n"
            "Write a concise diagnosis and what to do differently on the next attempt.",
            thinking_budget=3000,
        )
        tree = dict(state["goal_tree"])
        update_status(tree, gid, Status.PENDING)
        return {
            "goal_tree": tree,
            "messages": [{"role": "reflection", "content": reflection}],
        }

    async def approval(state: State) -> dict:
        """
        Human-in-the-loop gate before irreversible actions (git push, prod deploy).
        interrupt() checkpoints state and suspends execution until the thread is resumed.
        """
        gid = state["current_goal_id"]
        decision = interrupt({"action": "irreversible", "goal": gid})
        tree = dict(state["goal_tree"])
        new_status = Status.IN_PROGRESS if decision == "approve" else Status.BLOCKED
        update_status(tree, gid, new_status)
        return {"goal_tree": tree}

    return plan, execute_node, check, reflect, approval


# ---------------------------------------------------------------------------
# Conditional router
# ---------------------------------------------------------------------------

def router(state: State) -> str:
    """
    Route after the 'check' node. Reads only values committed in prior
    super-steps — never intra-super-step values.
    """
    gid = state.get("current_goal_id")
    if gid is None:
        return END

    goal = state["goal_tree"].get(gid)
    if goal is None:
        return END

    if goal["status"] == Status.FAILED and goal["attempts"] < 3:
        return "reflect"
    if goal["status"] == Status.AWAITING_APPROVAL:
        return "approval"
    if get_ready_goals(state["goal_tree"]):
        return "execute"
    return END
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/services/orchestrator/test_graph.py -v --no-header
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph.py
git commit -m "feat(orchestrator): add graph nodes (plan, execute, check, reflect, approval) and router"
```

---

## Task 7: `build_graph`, `__init__.py` Exports, and Smoke Test

**Files:**
- Modify: `services/orchestrator/graph.py` (append `build_graph`)
- Modify: `services/orchestrator/__init__.py` (add exports)
- Modify: `tests/services/orchestrator/test_graph.py` (append build_graph smoke test)

- [ ] **Step 1: Write the failing tests** (append to `test_graph.py`)

```python
# Append to tests/services/orchestrator/test_graph.py

@pytest.mark.mocked
class TestBuildGraph:
    @pytest.mark.asyncio
    async def test_build_graph_compiles_without_error(self):
        from unittest.mock import AsyncMock, MagicMock, patch, AsyncContextManager
        from services.orchestrator.graph import build_graph
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        # Mock AsyncMongoDBSaver.from_conn_string as an async context manager
        mock_cp = AsyncMock()
        mock_cp.setup = AsyncMock()
        mock_cp.__aenter__ = AsyncMock(return_value=mock_cp)
        mock_cp.__aexit__ = AsyncMock(return_value=False)
        mock_saver_cls = MagicMock()
        mock_saver_cls.from_conn_string = MagicMock(return_value=mock_cp)

        with patch(
            "services.orchestrator.graph.AsyncMongoDBSaver",
            mock_saver_cls,
        ):
            graph, cp = await build_graph(mock_orch, mock_async_orch)
            assert graph is not None
            mock_cp.setup.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_graph_wires_correct_edges(self):
        """Graph must have plan -> execute -> check -> [reflect | approval | execute | END]."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from services.orchestrator.graph import build_graph
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        mock_cp = AsyncMock()
        mock_cp.setup = AsyncMock()
        mock_cp.__aenter__ = AsyncMock(return_value=mock_cp)
        mock_cp.__aexit__ = AsyncMock(return_value=False)
        mock_saver_cls = MagicMock()
        mock_saver_cls.from_conn_string = MagicMock(return_value=mock_cp)

        with patch("services.orchestrator.graph.AsyncMongoDBSaver", mock_saver_cls):
            graph, _ = await build_graph(mock_orch, mock_async_orch)
            # The compiled graph has a nodes dict
            node_names = set(graph.nodes.keys())
            assert "plan" in node_names
            assert "execute" in node_names
            assert "check" in node_names
            assert "reflect" in node_names
            assert "approval" in node_names
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/services/orchestrator/test_graph.py::TestBuildGraph -v --no-header 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'build_graph'` or `AttributeError: AsyncMongoDBSaver`

- [ ] **Step 3: Append `build_graph` to `services/orchestrator/graph.py`**

Add the following at the end of `graph.py`, after the `router` function:

```python
# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

async def build_graph(
    orch: CodingOrchestrator,
    async_orch: AsyncOrchestrator,
    mongo_uri: str = MONGO_URI,
    db_name: str = "labmate",
):
    """
    Compile the StateGraph with an AsyncMongoDBSaver checkpointer.
    Returns (compiled_graph, checkpointer). The caller MUST keep checkpointer
    alive (inside async with or by holding the reference) for the graph's lifetime.

    Call cp.setup() once at startup to create MongoDB indexes.
    """
    from langgraph.checkpoint.mongodb.aio import AsyncMongoDBSaver

    plan_node, execute_node, check_node, reflect_node, approval_node = make_nodes(
        orch, async_orch
    )

    b = StateGraph(State)
    b.add_node("plan", plan_node)
    b.add_node("execute", execute_node)
    b.add_node("check", check_node)
    b.add_node("reflect", reflect_node)
    b.add_node("approval", approval_node)

    b.add_edge(START, "plan")
    b.add_edge("plan", "execute")
    b.add_edge("execute", "check")
    b.add_conditional_edges("check", router, ["execute", "reflect", "approval", END])
    b.add_edge("reflect", "execute")
    b.add_edge("approval", "execute")

    async with AsyncMongoDBSaver.from_conn_string(mongo_uri, db_name=db_name) as cp:
        await cp.setup()
        graph = b.compile(checkpointer=cp)
        return graph, cp
```

- [ ] **Step 4: Update `services/orchestrator/__init__.py`**

```python
# services/orchestrator/__init__.py
from .coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
from .graph import build_graph
from .types import State, Goal, Status

__all__ = [
    "CodingOrchestrator",
    "AsyncOrchestrator",
    "build_graph",
    "State",
    "Goal",
    "Status",
]
```

- [ ] **Step 5: Run all tests**

```bash
python -m pytest tests/services/orchestrator/ -v --no-header -m mocked
```

Expected: All tests PASS. No failures.

- [ ] **Step 6: Verify the module import chain works**

```bash
python -c "from services.orchestrator import CodingOrchestrator, AsyncOrchestrator, build_graph, State; print('import OK')"
```

Expected: `import OK`

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/graph.py services/orchestrator/__init__.py tests/services/orchestrator/test_graph.py
git commit -m "feat(orchestrator): add build_graph with AsyncMongoDBSaver checkpointer and package exports"
```

---

## Self-Review Checklist

After implementing all tasks, verify spec coverage:

**Spec Section 3 (Design Decisions) → covered by:**
- `AsyncOrchestrator.plan_and_dispatch()` — asyncio.TaskGroup + TopologicalSorter (rule 3, 8)
- `CodingOrchestrator.architect()` — `thinking_budget_tokens` extra_body (rule 7)
- `State` TypedDict — JSON-serializable only (rule 2)
- `step_markers` in `execute_node` — idempotency guard (rule 6)

**Spec Section 4.1 (Core Classes) → all implemented:**
- `Status`, `Goal`, `State`, `now_iso()`, `create_goal()`, `update_status()`, `get_ready_goals()` ✓
- `SubTask`, `Result`, `TokenBudget` ✓
- `AsyncOrchestrator.plan_and_dispatch()`, `_run_worker()`, `aggregate()` ✓
- `CodingOrchestrator.run_task()`, `architect()`, `editor()`, `is_stuck()`, `execute_in_sandbox()`, `git_checkpoint()` ✓

**Spec Section 4.2 (LangGraph) → covered:**
- `make_nodes()` factory: `plan`, `execute`, `check`, `reflect`, `approval` ✓
- `router()` conditional edges: reflect < 3 attempts, approval on AWAITING, execute on ready, END otherwise ✓
- `build_graph()` with `AsyncMongoDBSaver` checkpointer ✓

**Spec Section 5 (BDD Scenarios) → covered by mocked tests:**
- Reflexion recovery (`TestReflectNode`) ✓
- Stuck detection (`TestCodingOrchestrator.test_is_stuck_*`) ✓
- Idempotency on crash resume (`TestExecuteNode.test_idempotency_guard_*`) ✓
- DAG cycle rejection (`TestAsyncOrchestrator.test_raises_cycle_error_*`) ✓
- MoA aggregation (`TestAsyncOrchestrator.test_aggregate_*`) ✓
- CancelledError re-raise (`TestAsyncOrchestrator.test_cancelled_error_is_reraised`) ✓

**Critical rules check:**
- No tiktoken anywhere ✓ (never imported)
- No `chromadb.PersistentClient` ✓ (not in orchestrator; StorageManager handles memory)
- No `asyncio.run()` inside coroutines ✓
- `asyncio.TaskGroup` for fan-out ✓
- Semaphore acquired inside `_run_worker` ✓
- `thinking_budget_tokens` via `extra_body` ✓
- `AsyncMongoDBSaver` (not MemorySaver) ✓
