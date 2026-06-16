# Orchestrator Layer Spec

**Project:** Labmate — Local Autonomous Polyglot Coding + Writing Agent
**Hardware target:** RunPod RTX A6000 48 GB VRAM
**Date:** 2026-06-15
**Status:** Draft v1

---

## 1. Overview

The orchestrator layer is the cognitive spine of Labmate. It owns every decision between "user issues a task" and "agent emits a finished result": goal decomposition, loop execution, tool dispatch, error recovery, parallel fan-out, state persistence, and termination.

Three subsystems compose the layer — they are not independent modules but interlocking gears:

1. **ReAct+Plan-Execute-Monitor loop** — the per-task reasoning engine. An upfront planning call decomposes the task into a Goal Tree of subtasks, then a ReAct (Thought→Action→Observation) inner loop executes each subtask, monitored after every action for pass/fail/continue. On failure, Reflexion-style self-reflection writes a diagnosis to episodic memory and conditions the next attempt.

2. **LangGraph StateGraph** — the durable runtime that *runs* the loop. Every node of the graph corresponds to one phase of the loop (plan, execute, check, reflect, approval). The `AsyncMongoDBSaver` checkpointer persists a full state snapshot to MongoDB at every super-step, enabling crash recovery, resume, human-in-the-loop pauses, and time-travel replay. The state machine IS what manages the ReAct loop — the loop's phases are graph nodes and the transitions between phases are conditional edges.

3. **Parallel fan-out / AsyncOrchestrator** — the execution engine for subtasks that are independent. When the Goal Tree reveals a set of siblings with no mutual dependencies, the AsyncOrchestrator fans them out over `asyncio.TaskGroup`, gates concurrency with a `Semaphore` sized to A6000 VRAM capacity, enforces dual RPM+TPM rate limits, and aggregates results Mixture-of-Agents style. The parallel spawner IS what executes fan-out inside the orchestrator — it is called by the `execute` node of the StateGraph whenever the scheduler finds a ready set of width > 1.

**Brain routing:** Gemma 4 MoE 4-bit handles planning, self-reflection, and aggregation (architect role). Qwen2.5-Coder-32B handles code generation and file edits (editor/worker role). Both share the single A6000 and communicate via the TypeScript MCP server / Python MCP client over stdio JSON-RPC.

---

## 2. Architecture

### 2.1 ReAct+Plan-Execute-Monitor Loop

The loop has three nested layers of control, each operating at a different timescale:

**Layer 1 — Plan-Execute-Monitor wrapper (task level)**

Before any ReAct iteration begins, the orchestrator issues a single "architect" call to Gemma 4 to decompose the task into a Goal Tree of subtasks. This plan is stored in `State.goal_tree` and persisted to MongoDB. A Monitor check runs after every action: it classifies the latest `Observation` as `pass | fail | continue`. On `pass`, the subtask is marked `COMPLETED` and a git checkpoint is written. On `fail`, the loop escalates to Reflexion.

*Why Plan-Execute-Monitor and not vanilla ReAct?* Empirically, the hybrid beats vanilla ReAct (83% → 95% on representative benchmarks) and does NOT lose to Tree-of-Thoughts at equal compute budget — ToT costs ~100x and performs no better. Reserve ToT/LATS for bounded isolated sub-decisions only (patch selection, candidate localization), never as the default top-level loop.

**Layer 2 — ReAct inner loop (subtask level)**

For each subtask the `execute` node runs a Thought→Action→Observation step, appending each pair to the `EventLog`. Actions are emitted as executable code (CodeAct), not ad-hoc JSON schemas. A single CodeAct action can compose control flow and manipulate the environment directly with bash/Python — critical for a smaller local brain. Scoped ACI tools (view, edit-with-syntax-check, search\_file, run\_tests) wrap the MCP tool calls; a flat 100-tool list would invite hallucination.

**Layer 3 — Reflexion recovery (failure level)**

When Monitor signals `fail`, the `reflect` node calls the Gemma 4 architect with a reflection prompt over the current `EventLog` and `Observation`. The returned natural-language diagnosis is written to two stores: Redis (hot, retrieved on the next attempt) and MongoDB (durable, available across sessions). The next attempt's execute call retrieves and conditions on the stored reflections. A hard cap `max_attempts=3` prevents infinite retry.

**Context compaction** prevents context rot: the raw EventLog is actively compressed/summarized when it grows past a threshold — full file bodies and stale tool outputs are dropped, re-derivable content is re-read on demand, and a structured note of decisions and TODOs is maintained. Never let the raw EventLog grow unbounded into the prompt.

### 2.2 Agent State Machine (LangGraph StateGraph)

The StateGraph encodes every phase of the Plan-Execute-Monitor loop as a node. The conditional router encodes all branching. Together they replace ad-hoc if/else flow with a durable, serializable, resumable execution graph.

**State object** (JSON-serializable only — no Python objects, no DB clients, no coroutines):

```
State
├── session_id: str
├── goal_tree: dict[str, Goal]        # the live plan; each entry is a Goal TypedDict
├── current_goal_id: str | None
├── step_markers: dict[str, str]      # idempotency: step_id -> 'started' | 'completed'
├── messages: Annotated[list, add]    # reducer-safe; parallel nodes append without races
└── error: str | None
```

**Goal object** (one entry per node of the Goal Tree):

```
Goal
├── id, parent_id, children: list[str]
├── description: str
├── status: Status enum  (PENDING | IN_PROGRESS | COMPLETED | FAILED | BLOCKED | AWAITING_APPROVAL)
├── result: str | None
├── error: str | None
├── attempts: int
└── started_at, updated_at: ISO-8601 str | None
```

**Graph topology:**

```
START -> plan -> execute -> check --+--> reflect -> execute   (on FAILED, attempts < 3)
                                    +--> approval -> execute  (on AWAITING_APPROVAL)
                                    +--> execute              (on COMPLETED child, next sibling ready)
                                    +--> END                  (all goals COMPLETED or budget hit)
```

**Conditional router** reads only values committed in a *prior* super-step (never within the current super-step — stale routing is a known LangGraph pitfall). It branches on `goal.status` and `goal.attempts`.

**Checkpointing** is automatic at every super-step via `AsyncMongoDBSaver`. Resume = re-invoke `graph.ainvoke()` with the same `thread_id`. Human-in-the-loop pauses are implemented with LangGraph's `interrupt()` inside the `approval` node, which checkpoints state and suspends execution until explicitly resumed.

**Idempotency guard** on every side-effecting node: check `step_markers[step_id] == 'completed'` and return early if already done; write `'started'` before the side effect, `'completed'` after. A crash mid-node will re-execute the node on resume; the guard prevents duplicate git commits, double file writes, or repeated API calls.

**Status TTL sweep** (external job): any goal stuck in `IN_PROGRESS` past a configured deadline (e.g., 30 min) is reset to `PENDING` to recover from orchestrator crashes that happen inside a node, after `'started'` but before `'completed'`.

### 2.3 Goal Tree

The Goal Tree is the long-horizon plan representation. It is stored as `State.goal_tree: dict[str, Goal]` inside the checkpointed state — not a separate store — so it travels with the state snapshot and is always consistent with the current execution point.

**Structure:**

- Each `Goal` carries a `parent_id` and a `children` list, forming a rooted tree of arbitrary depth.
- The root goal is the original user task.
- Leaf goals map directly to single `execute` node invocations.
- Interior goals are `COMPLETED` only when all their children are `COMPLETED`.

**Lifecycle:**

1. `plan` node (Gemma 4 architect call) decomposes the root goal and writes child goals into `goal_tree`.
2. `get_ready_goals()` returns `PENDING` leaves whose `children` are all `COMPLETED` (or empty) — these are eligible for immediate execution, potentially in parallel.
3. `execute` node sets the goal `IN_PROGRESS`, runs the ReAct inner loop, and updates status.
4. Completed subtrees are archived out of `goal_tree` (moved to a separate MongoDB collection) to keep the State document small and checkpoint writes fast.

**Depth cap:** Goals decompose to a configurable maximum depth (default: 4). Deeper decomposition bloats the Goal Tree and introduces planning overhead that outweighs gains.

**ReAcTree alignment:** This design mirrors the sequence/fallback/parallel control-flow node types from ReAcTree (Choi et al. 2025). Parallel control-flow maps to the AsyncOrchestrator fan-out in Section 2.4.

### 2.4 Multi-Agent Parallel Spawning

When `get_ready_goals()` returns more than one leaf goal simultaneously, the `execute` node delegates to `AsyncOrchestrator.plan_and_dispatch()` rather than executing sequentially. The parallel spawner is therefore a sub-component *called by* the StateGraph, not a separate top-level system.

**Execution model: dependency DAG fan-out/fan-in**

1. **Plan time:** The ready set of Goals is validated as a dependency DAG using `graphlib.TopologicalSorter`. A cycle raises `CycleError` before any worker is spawned. The sorted order drives deterministic scheduling.

2. **Scheduling:** `ts.get_ready()` returns all currently-unblocked siblings. They are dispatched together as tasks inside a single `asyncio.TaskGroup`. `ts.done(tid)` is called after each completes, releasing its dependents.

3. **Concurrency control — dual gating:**
   - `asyncio.Semaphore(max_inflight)` bounds in-flight workers. Sized to A6000 KV-cache capacity (start at 4; tune empirically). Acquired *inside the leaf worker*, never at the fan-out site (to avoid self-deadlock).
   - `AsyncLimiter(rpm, 60)` + `AsyncLimiter(tpm, 60)` via `aiolimiter` enforce RPM and TPM limits with a leaky-bucket algorithm.
   - `TokenBudget` (global, guarded by `asyncio.Lock`) tracks remaining tokens. `budget.reserve(n)` is called *before* the model call; on failure the worker waits in a tight loop until headroom frees, rather than spawning and hitting a 429 mid-flight.

4. **Workers:** Each spawned worker calls `Qwen2.5-Coder-32B` via the Python MCP client / TypeScript MCP server over stdio JSON-RPC. Workers return a **condensed** `Result` (summary + artifact paths), never their full transcript, to prevent the Gemma orchestrator context from overflowing.

5. **Aggregation (MoA-style):** After all parallel workers complete, `_aggregate()` calls the Gemma 4 architect with all condensed candidates and produces a single synthesized result that is handed back to the StateGraph.

6. **Structured cancellation:** `asyncio.TaskGroup` ensures that if any sibling worker raises an exception, all other in-flight siblings are cancelled and awaited before the exception propagates. No orphaned coroutines, no VRAM leaks.

### 2.5 Component Interaction Diagram (ASCII)

```
User Task
    |
    v
+----------------------------+
|   CodingOrchestrator       |  <-- top-level entry point
|   (plan / monitor / loop)  |
+----------------------------+
    |                  |
    | architect call   | editor call
    v                  v
[Gemma 4 MoE 4-bit]  [Qwen2.5-Coder-32B]
(plan / reflect /     (CodeAct steps /
 aggregate)            file edits)
    |                  |
    +---via litellm----+
              |
    +---------+---------+
    |  LangGraph        |
    |  StateGraph       |  <-- durable loop runtime
    |                   |
    | nodes:            |
    |  plan             |
    |  execute -----> AsyncOrchestrator (fan-out)
    |  check            |    |
    |  reflect          |    v
    |  approval         | asyncio.TaskGroup
    |                   |    | [worker] [worker] [worker]
    |  checkpointer:    |    |    via MCP stdio JSON-RPC
    |  AsyncMongoDBSaver|    v
    |  (every super-step)|  [TypeScript MCP Server]
    +-------------------+         |
              |                   v
    +---------+--------+   [Docker Sandbox]
    |  Memory Layer    |   (bash / IPython kernel)
    |                  |         |
    |  Redis           |   git commit (per edit)
    |  (episodic, hot) |
    |  MongoDB         |
    |  (checkpoint +   |
    |   durable memory)|
    |  Chroma          |
    |  (semantic, LTR) |
    +------------------+
              |
              v
    Tree-sitter Repo Map
    (ranked symbol graph
     for context selection)
```

---

## 3. Key Design Decisions

1. **CodeAct (executable code) as the unified action space, not JSON tool schemas.** The LLM emits Python/bash it was pretrained on; a single CodeAct action can compose control flow and is less susceptible to schema hallucination. Empirically: up to ~20% higher task success across 17 LLMs (Wang et al. 2024 / ICML).

2. **ReAct+Plan-Execute-Monitor over vanilla ReAct or Tree-of-Thoughts.** The hybrid empirically scores 83%→95% on standard benchmarks. ToT costs ~100x compute and does not beat it at equal budget. ToT and LATS are reserved only for bounded isolated sub-decisions (patch selection, localization voting), never as the default top-level loop.

3. **LangGraph StateGraph as the loop runtime, not ad-hoc asyncio coroutines.** LangGraph provides native checkpointing (AsyncMongoDBSaver), human-in-the-loop interrupt/resume, time-travel replay, and thread-isolated state — capabilities that are extremely costly to re-implement correctly in bare asyncio. The state machine IS the loop.

4. **JSON-serializable State only.** Python objects, DB clients, coroutines, and file handles cannot cross super-step boundaries. Violating this causes silent checkpoint corruption. Every value in `State` must round-trip through `json.dumps` / `json.loads`.

5. **Idempotency guards on all side-effecting nodes.** LangGraph re-executes the in-flight node after a crash+resume. Without a `step_markers` guard, a git commit or API call fires twice. Write `'started'` before, `'completed'` after; short-circuit on `'completed'`.

6. **Gemma 4 as architect / Qwen2.5-Coder-32B as editor — strict separation via separate litellm calls.** Mixing architect reasoning and code-output generation in the same call produces sloppy diffs and conflated roles. Separate calls also allow separate system prompts, temperatures, and token budgets.

7. **Docker sandbox with no writable host mount.** All CodeAct actions execute inside a container with a persistent bash + IPython kernel. The host filesystem is never touched by anything the agent runs. A raw shell without sandboxing can `rm -rf /` the host; one Docker exec cannot.

8. **Git checkpoint after every successful file edit.** The git log becomes a rollback ladder. If step 8 fails catastrophically after successful edits at steps 1–7, the orchestrator can revert to any prior commit rather than starting over. Implemented in `_git_checkpoint()` called by the `execute` node on Monitor `pass`.

9. **asyncio.TaskGroup for structured concurrency, never bare gather() or create\_task().** `TaskGroup` auto-cancels sibling workers when one fails — no orphaned coroutines, no leaked VRAM on the shared A6000. `gather(return_exceptions=True)` swallows failures silently; `TaskGroup` raises an `ExceptionGroup` that forces explicit handling.

10. **Semaphore acquired at the leaf worker, never at the fan-out site.** Acquiring the Semaphore *around* `create_task()` can deadlock when all permits are held by parents waiting on children that cannot start. Acquire inside the worker coroutine, after the task is already running inside the TaskGroup.

11. **Plan-time DAG validation with `graphlib.TopologicalSorter.prepare()`.** A dependency cycle detected at runtime causes an infinite wait with no error. `prepare()` raises `CycleError` before any worker is spawned — fail fast, zero tokens wasted.

12. **Condensed result handback from sub-agents.** Each parallel worker returns a `Result(summary, artifacts, ok)` — never its full transcript. Full transcripts from N parallel workers would overflow the Gemma 4 orchestrator context window (acute for a 4-bit model with a tight window).

13. **Purpose-built ACI tools over raw shell.** SWE-agent's central finding: scoped, concise tools (view, edit-with-syntax-check, search\_file, run\_tests) dramatically outperform a raw shell or a flat 100-tool list. Invest in ACI quality before optimizing the LLM backbone.

14. **Tree-sitter repo map for context selection.** Aider-style ranked symbol graph across the polyglot codebase (TypeScript, Rust, Python) so the agent requests only relevant files instead of stuffing the whole repo into the context window.

15. **Explicit max\_iter, token budget, and AgentFinishAction as the only clean exit.** Open-ended loops thrash and burn VRAM. The controller enforces a budget; on budget exhaustion, the best-so-far state is persisted and `AgentFinishAction` is emitted.

---

## 4. Implementation Guide

### 4.1 Core Classes

```python
# orchestrator/types.py
from __future__ import annotations
from enum import Enum
from typing import Annotated, Optional, TypedDict
from operator import add
import datetime


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
    A single node in the Goal Tree.  All values must be JSON-serializable --
    no Python objects, no datetimes (use ISO-8601 strings).
    """
    id: str
    parent_id: Optional[str]
    children: list[str]
    description: str
    status: str                  # Status enum value; stored as str for JSON safety
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
    here.  Everything must survive json.dumps() / json.loads() round-trips.
    """
    session_id: str
    goal_tree: dict[str, Goal]         # id -> Goal; the live plan
    current_goal_id: Optional[str]
    step_markers: dict[str, str]       # step_id -> 'started' | 'completed'
    messages: Annotated[list, add]     # reducer-safe; parallel nodes may append
    error: Optional[str]


def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


# Goal Tree helpers (pure functions, no side effects, safe to call inside nodes)

def create_goal(
    tree: dict[str, Goal],
    gid: str,
    parent_id: Optional[str],
    desc: str,
) -> dict[str, Goal]:
    """Insert a new PENDING goal and wire it into its parent's children list."""
    tree[gid] = Goal(
        id=gid, parent_id=parent_id, children=[],
        description=desc, status=Status.PENDING,
        result=None, error=None, attempts=0,
        started_at=None, updated_at=None,
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
    """Transition a goal to a new status and optionally set result/error/etc."""
    g = tree[gid]
    g["status"] = status
    g["updated_at"] = now_iso()
    for k, v in kwargs.items():
        g[k] = v  # type: ignore[literal-required]
    return tree


def get_ready_goals(tree: dict[str, Goal]) -> list[Goal]:
    """
    Return all PENDING leaf goals (or goals whose children are all COMPLETED).
    These are eligible for immediate execution and may be parallelised.
    """
    return [
        g for g in tree.values()
        if g["status"] == Status.PENDING
        and all(tree[c]["status"] == Status.COMPLETED for c in g["children"])
    ]
```

```python
# orchestrator/coding_orchestrator.py
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
    NEVER return the raw transcript -- it would overflow the orchestrator context.
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


# ---------------------------------------------------------------------------
# Async parallel orchestrator (called by the StateGraph execute node)
# ---------------------------------------------------------------------------

class AsyncOrchestrator:
    """
    Executes a set of independent SubTasks in parallel over a validated
    dependency DAG.  Used by the LangGraph 'execute' node when
    get_ready_goals() returns multiple PENDING leaves.

    Concurrency is dual-gated:
      - asyncio.Semaphore: bounds in-flight workers to A6000 KV-cache capacity.
      - AsyncLimiter (RPM + TPM): leaky-bucket rate limits via aiolimiter.
      - TokenBudget: global remaining-tokens guard (80% safety margin).
    """

    def __init__(
        self,
        max_inflight: int = 4,      # tune to A6000 VRAM / KV-cache headroom
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

    async def plan_and_dispatch(self, ready_goals: list[Goal]) -> list[Result]:
        """
        Validate the ready set as a dependency DAG, then fan-out workers with
        asyncio.TaskGroup for structured concurrency.  Returns condensed Results.
        """
        import graphlib

        subtasks = [
            SubTask(id=g["id"], prompt=g["description"], est_tokens=512)
            for g in ready_goals
        ]
        dep_graph: dict[str, set[str]] = {t.id: t.deps for t in subtasks}

        ts = graphlib.TopologicalSorter(dep_graph)
        ts.prepare()  # raises CycleError immediately -- fail fast, no spawn

        index = {t.id: t for t in subtasks}

        async with asyncio.TaskGroup() as tg:  # structured: auto-cancels siblings on failure
            running: dict[str, asyncio.Task] = {}
            while ts.is_active():
                for tid in ts.get_ready():
                    if tid not in running:
                        running[tid] = tg.create_task(self._run_worker(index[tid]))
                # yield to the event loop so tasks can progress
                await asyncio.sleep(0)
                for tid, task in list(running.items()):
                    if task.done() and not task.cancelled():
                        ts.done(tid)

        return list(self.results.values())

    async def _run_worker(self, t: SubTask) -> str:
        """
        Execute a single sub-task worker:
          1. Reserve token budget (wait in loop, never dispatch on bust).
          2. Acquire Semaphore inside the worker (never at the fan-out site).
          3. Rate-limit then call Qwen2.5-Coder-32B via the MCP stdio bridge.
          4. Condense and store the result.
          5. Re-raise CancelledError -- NEVER swallow it.
        """
        # Pre-flight budget gate
        while not await self.budget.reserve(t.est_tokens):
            await asyncio.sleep(0.5)          # queued, not dispatched; no TPM bust

        try:
            async with self.sem:              # acquired at leaf, not at fan-out
                async with self.rpm_limiter:
                    async with self.tpm_limiter:
                        raw = await self._call_qwen_worker(t)
                        self.results[t.id] = self._condense(t.id, raw)
        except asyncio.CancelledError:
            await self.budget.refund(t.est_tokens)
            raise                             # never swallow -- keeps TaskGroup correct
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
        """
        Strip the raw transcript down to summary + artifact paths.
        Returning full transcripts from N workers would overflow the
        Gemma 4 orchestrator context.
        """
        # In practice: call Gemma to summarise, or extract structured fields.
        return Result(id=tid, summary=raw[:2000], ok=True)

    async def aggregate(self, task: str, results: list[Result]) -> Result:
        """MoA-style aggregation: Gemma 4 synthesises all candidate results."""
        candidates = "\n\n".join(
            f"[{r.id}] {'OK' if r.ok else 'FAILED'}: {r.summary}" for r in results
        )
        prompt = f"Task: {task}\n\nCandidate results:\n{candidates}\n\nSynthesize the best unified result."
        r = await litellm.acompletion(
            model="openai/gemma-4-moe",
            api_base=self._gemma_base,
            messages=[{"role": "user", "content": prompt}],
        )
        return Result(id="aggregated", summary=r.choices[0].message.content)


# ---------------------------------------------------------------------------
# Main coding orchestrator (wraps the StateGraph entry point)
# ---------------------------------------------------------------------------

class CodingOrchestrator:
    """
    Top-level entry point.  Wraps the LangGraph StateGraph with convenience
    methods and the Gemma 4 architect / Qwen2.5-Coder-32B editor routing.

    Lifecycle:
      run_task() -> graph.ainvoke() -> plan -> execute -> check -> [reflect | END]

    Crash recovery: re-invoke run_task() with the same session_id.  The
    AsyncMongoDBSaver will load the latest checkpoint and resume from the
    last completed super-step.
    """

    def __init__(
        self,
        graph,              # compiled LangGraph StateGraph
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
        Entry point.  Pass the same session_id to resume after a crash.
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

    # ------------------------------------------------------------------
    # Architect / editor LLM routing (separate litellm calls)
    # ------------------------------------------------------------------

    async def architect(self, prompt: str) -> str:
        """Planning, self-reflection, aggregation -> Gemma 4 MoE 4-bit."""
        r = await litellm.acompletion(
            model="openai/gemma-4-moe",
            api_base=self._gemma_base,
            messages=[{"role": "user", "content": prompt}],
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

    # ------------------------------------------------------------------
    # Stuck-detection
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Docker sandbox execution
    # ------------------------------------------------------------------

    def execute_in_sandbox(self, cmd: str, timeout: int = 60) -> dict:
        """
        Run a shell command inside the Docker container.
        The host filesystem is NEVER mounted writable; only the container
        is affected by rm -rf or any other destructive command.
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

    # ------------------------------------------------------------------
    # Git checkpoint
    # ------------------------------------------------------------------

    def git_checkpoint(self, message: str) -> None:
        """
        Commit all workspace changes as a rollback-ladder checkpoint.
        Called after every successful file edit (Monitor -> 'pass').
        """
        subprocess.run(["git", "-C", self.workspace, "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", self.workspace, "commit", "-m", message],
            check=True,
        )
```

### 4.2 LangGraph StateGraph Setup

```python
# orchestrator/graph.py
from __future__ import annotations
import asyncio
from typing import Any

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.mongodb.aio import AsyncMongoDBSaver
from langgraph.types import interrupt

from .types import State, Status, Goal, get_ready_goals, update_status, now_iso
from .coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

MONGO_URI = "mongodb://localhost:27017"
GEMMA_BASE = "http://localhost:8000/v1"
QWEN_BASE  = "http://localhost:8001/v1"


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

def make_nodes(orch: CodingOrchestrator, async_orch: AsyncOrchestrator):
    """
    Factory that closes over the orchestrator instances so nodes are plain
    functions/coroutines (no class state on the graph itself).
    """

    async def plan(state: State) -> dict:
        """
        Decompose the current root goal into child Goals via a Gemma 4 architect call.
        Writes new Goal entries into goal_tree.  Pure return-delta -- no in-place mutation.
        """
        root_id = state["current_goal_id"]
        root_desc = state["goal_tree"][root_id]["description"]
        raw_plan = await orch.architect(
            f"Decompose this task into concrete subtasks (one per line):\n{root_desc}"
        )
        tree = dict(state["goal_tree"])  # shallow copy before mutation
        from .types import create_goal
        import uuid
        for i, line in enumerate(raw_plan.strip().splitlines()):
            if line.strip():
                gid = f"{root_id}_sub{i}"
                create_goal(tree, gid, root_id, line.strip())
        return {"goal_tree": tree}

    async def execute_node(state: State) -> dict:
        """
        Execute the current PENDING goal.  If multiple goals are ready in parallel,
        delegates to AsyncOrchestrator.plan_and_dispatch().
        Includes an idempotency guard via step_markers.
        """
        ready = get_ready_goals(state["goal_tree"])
        if not ready:
            return {}

        tree = dict(state["goal_tree"])
        markers = dict(state["step_markers"])

        if len(ready) > 1:
            # Parallel fan-out path
            results = await async_orch.plan_and_dispatch(ready)
            for r in results:
                gid = r.id
                markers[gid] = "completed"
                new_status = Status.COMPLETED if r.ok else Status.FAILED
                update_status(tree, gid, new_status, result=r.summary)
            return {"goal_tree": tree, "step_markers": markers}
        else:
            # Sequential path -- single goal
            goal = ready[0]
            gid = goal["id"]

            # Idempotency guard
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
        # Monitor logic: inspect test output, linter, etc.
        # In practice: run tests, parse output, update status.
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
            f"Write a concise diagnosis and what to do differently on the next attempt."
        )
        # Persist to Redis (hot) and MongoDB (durable) -- external side effect
        # redis_client.set(f"reflect:{gid}:{goal['attempts']}", reflection, ex=3600)
        # mongo_db.reflections.insert_one({...})
        tree = dict(state["goal_tree"])
        update_status(tree, gid, Status.PENDING)   # reset to PENDING for retry
        return {"goal_tree": tree, "messages": [{"role": "reflection", "content": reflection}]}

    async def approval(state: State) -> dict:
        """
        Human-in-the-loop gate before irreversible actions (git push, prod deploy, rm).
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
    Route after the 'check' node.  Reads only values committed in prior
    super-steps -- never intra-super-step values.
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
    Call cp.setup() once (creates MongoDB indexes) before serving requests.
    """
    plan, execute_node, check, reflect, approval = make_nodes(orch, async_orch)

    b = StateGraph(State)
    b.add_node("plan", plan)
    b.add_node("execute", execute_node)
    b.add_node("check", check)
    b.add_node("reflect", reflect)
    b.add_node("approval", approval)

    b.add_edge(START, "plan")
    b.add_edge("plan", "execute")
    b.add_edge("execute", "check")
    b.add_conditional_edges("check", router, ["execute", "reflect", "approval", END])
    b.add_edge("reflect", "execute")
    b.add_edge("approval", "execute")

    async with AsyncMongoDBSaver.from_conn_string(mongo_uri, db_name=db_name) as cp:
        await cp.setup()   # build indexes; idempotent -- call once at startup
        graph = b.compile(checkpointer=cp)
        return graph, cp   # caller must keep cp alive for the graph's lifetime
```

### 4.3 Parallel Fan-out with asyncio.TaskGroup

The `AsyncOrchestrator.plan_and_dispatch()` method in Section 4.1 contains the full implementation. Key points extracted for reference:

```python
# Pattern: structured concurrency with dependency-ordered dispatch
import asyncio, graphlib

async with asyncio.TaskGroup() as tg:       # no orphans; auto-cancels siblings on failure
    running: dict[str, asyncio.Task] = {}
    while ts.is_active():
        for tid in ts.get_ready():           # all currently-unblocked siblings
            if tid not in running:
                running[tid] = tg.create_task(worker(index[tid]))
        await asyncio.sleep(0)              # yield to event loop
        for tid, task in list(running.items()):
            if task.done() and not task.cancelled():
                ts.done(tid)               # release dependents

# Pattern: Semaphore acquired inside the leaf worker (NEVER at fan-out site)
async def _run_worker(self, t):
    async with self.sem:                   # leaf acquisition -- no self-deadlock
        result = await call_model(t)
    return result

# Pattern: CancelledError must be re-raised
try:
    async with self.sem:
        result = await call_model(t)
except asyncio.CancelledError:
    await self.budget.refund(t.est_tokens)
    raise                                  # NEVER swallow -- breaks TaskGroup
```

### 4.4 MongoDB Checkpoint Configuration

```python
# startup.py  (run once at service startup)
import asyncio
from langgraph.checkpoint.mongodb.aio import AsyncMongoDBSaver

MONGO_URI = "mongodb://localhost:27017"
DB_NAME   = "labmate"

async def setup_checkpointer():
    """
    Create indexes, validate connectivity.
    AsyncMongoDBSaver uses the 'checkpoints' collection by default.
    Keep the saver instance alive for the lifetime of the graph.
    """
    async with AsyncMongoDBSaver.from_conn_string(MONGO_URI, db_name=DB_NAME) as cp:
        await cp.setup()   # idempotent -- safe to call on every startup
        print("MongoDB checkpointer ready.")
        return cp

# Usage: pass cp to build_graph(), keep it open with `async with` or by storing the reference.

# Recommended collection layout in MongoDB:
#   labmate.checkpoints       -- LangGraph checkpoint snapshots (auto-managed)
#   labmate.goals_archive     -- Completed subtrees pruned from active State
#   labmate.reflections       -- Reflexion episodic memory (durable)
#   labmate.trajectories      -- Full EventLog for debugging / audit

# TTL index for IN_PROGRESS status recovery (run once in setup):
# db.checkpoints.createIndex(
#   {"state.goal_tree.$**.updated_at": 1},
#   {expireAfterSeconds: 1800}    -- 30 min; adjust to task horizon
# )
```

**Resume pattern:**

```python
# Resume after crash: re-invoke with the same thread_id.
# AsyncMongoDBSaver loads the latest checkpoint automatically.
cfg = {"configurable": {"thread_id": session_id}}
result = await graph.ainvoke(
    None,   # None signals "resume from checkpoint" -- do not pass initial state again
    cfg,
)
```

---

## 5. BDD Test Scenarios

### 5.1 Plan-Execute-Monitor Loop

```gherkin
Feature: Plan-Execute-Monitor coding agent loop with Reflexion recovery

  Scenario: Resolve a failing test via the ReAct+Plan loop within budget
    Given a sandboxed workspace cloned at a target repository commit
    And a failing test that defines the task
    And an editable plan generated by the Plan-Execute-Monitor pass
    When the orchestrator runs the inner ReAct/CodeAct loop
    And each code action is executed in the Docker runtime and returns an observation
    Then the failing test passes in 10 iterations or fewer
    And the agent emits an AgentFinishAction with a unified-diff patch
    And the EventLog contains the full ordered action/observation trajectory

  Scenario: Reflexion error recovery avoids repeating a known mistake
    Given the agent has made the same failing edit twice
    When the controller routes the second failure into a Reflexion step
    And the reflection is written to episodic memory
    And the third attempt queries that episodic memory
    Then the third attempt explicitly avoids the previously reflected failure mode
    And the agent does not re-issue the identical failed edit

  Scenario: Docker sandbox isolates a destructive command from the host
    Given the agent issues an action equivalent to rm -rf /
    When the action is executed inside the Docker sandbox container
    Then only the container filesystem is affected
    And the host filesystem is unaffected
    And the orchestrator records the destructive action as an observation and continues safely

  Scenario: Git checkpoint per edit enables partial rollback
    Given the agent performs 5 successful file edits in sequence
    When each successful edit completes
    Then git log shows 5 new commits, one per edit
    And the orchestrator can roll back to any of the prior 5 commits on a later catastrophic failure

  Scenario: Stuck-detection escalates out of a ReAct loop
    Given the agent emits N consecutive identical actions with no state change
    When the stuck-detector trips
    Then the inner ReAct loop is escalated to a fresh Plan-Execute pass
    And the orchestrator does not continue emitting the repeated action
```

### 5.2 Agent State Machine

```gherkin
Feature: Crash-resilient agent state machine

  Scenario: Checkpoint and resume after orchestrator crash
    Given the agent has completed step 3 of a 7-step goal
    And each completed step was checkpointed to MongoDB under its thread_id
    When the orchestrator process crashes and is restarted
    Then the agent loads the latest checkpoint for that thread_id
    And it resumes execution from step 4
    And it does not re-run steps 1 through 3

  Scenario: Idempotent retry of a failed step with a side effect
    Given step 5 performs a git commit and failed on its first attempt after committing
    And a step-completed marker for step 5 was written before the failure surfaced
    When step 5 is retried on resume
    Then the node sees the step-completed marker and skips the git commit
    And no duplicate commit is created
    And the goal advances to step 6

  Scenario: Human-in-the-loop gate before an irreversible action
    Given the agent is about to run "git push" to a remote
    When the push node sets the goal status to AWAITING_APPROVAL and calls interrupt()
    Then graph execution pauses and the current state is checkpointed
    And no push occurs until a human resumes the thread with an approval
    And on rejection the goal transitions to BLOCKED instead of pushing

  Scenario: SagaLLM-style compensation on late failure
    Given steps 1 through 5 succeeded and produced side effects
    And step 6 fails irrecoverably
    When the compensating transaction runs
    Then the registered compensations for steps 5 down to 1 execute in reverse order
    And the system-wide state is restored to its pre-goal condition
    And the goal status is set to FAILED with the error recorded
```

### 5.3 Multi-Agent Parallel Spawning

```gherkin
Feature: Plan-time DAG validation

  Scenario: Dependency cycle is rejected before any agent is spawned
    Given a task decomposed into sub-tasks A, B, C
    And the dependency edges A->B, B->C, and C->A forming a cycle
    When the orchestrator validates the dependency DAG with a topological sort
    Then a CycleError is raised at planning time
    And no sub-agent worker is spawned
    And no tokens are consumed beyond the planning call

Feature: Parallel execution with dependency ordering

  Scenario: Independent siblings run concurrently while dependents wait
    Given a validated DAG where task C depends on both A and B but not on D
    And tasks A, B, and D have no unmet dependencies
    When the scheduler dispatches the ready set
    Then A, B, and D are spawned in parallel inside a single TaskGroup
    And C is not spawned until both A and B have completed successfully
    And if A raises an exception, B and D are cancelled and C is never spawned

Feature: Token-budget gating of spawns

  Scenario: A spawn that would exceed the global token budget is queued, not dispatched
    Given a global token budget with a remaining balance below the estimated cost of the next agent
    And a sliding-window TPM limiter that is currently saturated
    When the scheduler attempts to dispatch the next ready agent
    Then the spawn is rejected and the agent is held in a pending queue
    And the agent is dispatched only after enough budget/TPM headroom is released
    And no 429 / TPM-exceeded error is raised mid-flight

Feature: Mixture-of-Agents aggregation

  Scenario: N parallel proposals are synthesized into a single result
    Given a sub-task fanned out to N=3 proposer agents in parallel
    When all 3 proposers return their condensed candidate results
    Then an aggregator agent receives all 3 candidates
    And the aggregator produces exactly one synthesized result
    And only the synthesized result (not the raw 3 candidates) is handed back to the orchestrator context
```

---

## 6. Common Pitfalls

The following failure modes are synthesized across all three research areas, ranked by operational impact.

1. **Non-serializable State (state machine pitfall, CRITICAL).** Storing Python objects, coroutines, open file handles, or a MongoDB client inside `State` causes serialization failure at the next AsyncMongoDBSaver checkpoint. Allow only plain JSON types. Every value in `State` must survive `json.dumps()` / `json.loads()` without error.

2. **Swallowing `asyncio.CancelledError` (parallel spawning pitfall, CRITICAL).** A worker that catches `CancelledError` without re-raising breaks `TaskGroup` and `asyncio.timeout` structured cancellation. Tasks appear to "linger" after cancellation, leaking VRAM on the A6000. Use `try/finally` for cleanup and always re-raise.

3. **Missing idempotency guard on side-effecting nodes (state machine pitfall, HIGH).** On crash+resume, LangGraph re-executes the in-flight node. Without `step_markers`, a git commit or API call fires twice. Write `'started'` before the side effect, `'completed'` after. Short-circuit if already `'completed'`.

4. **Fire-and-forget tasks with bare `gather()` or `create_task()` (parallel spawning pitfall, HIGH).** A failed sibling does NOT cancel the others. Orphaned coroutines survive their parent, leak VRAM/KV-cache on the A6000, and silently burn the token budget. Use `asyncio.TaskGroup` exclusively for multi-agent fan-out.

5. **Context window saturation / context rot (loop pitfall, HIGH).** The EventLog grows to fill the full context window, squeezing out current code context and degrading reasoning. Actively compress early history: drop re-derivable content (full file bodies, stale tool outputs), keep a structured decisions+TODO note, re-read on demand. Never let the raw EventLog grow unbounded into the prompt.

6. **DAG cycle deadlock (parallel spawning pitfall, HIGH).** A dependency cycle not detected at plan time leaves workers waiting forever on each other. Call `graphlib.TopologicalSorter.prepare()` before any spawn. It raises `CycleError` immediately — fail fast, zero tokens wasted.

7. **Semaphore self-deadlock (parallel spawning pitfall, HIGH).** Acquiring the Semaphore *around* `create_task()` at the fan-out site can deadlock when all permits are held by parents waiting on children that cannot start. Acquire inside the leaf worker, after the task is running.

8. **Token-budget blind spot (parallel spawning pitfall, HIGH).** Spawning N agents without pre-estimating prompt+completion tokens busts the TPM budget mid-flight, causing cascading 429s and half-finished plans. Pre-estimate tokens with `tiktoken` (approximate for non-OpenAI models) and call `budget.reserve(n)` before the model call.

9. **ReAct myopia / stuck loops (loop pitfall, MEDIUM).** The agent repeats the same action without progress. Track the last N action keys; if N consecutive are identical (or no env state change), escalate from the inner ReAct loop to a fresh Plan-Execute pass rather than burning turns.

10. **Architect and editor in the same model call (loop pitfall, MEDIUM).** Architect reasoning and edit-output generation interfere, producing sloppy diffs. Keep them as strictly separate litellm calls with separate system prompts, temperatures, and model configs (Gemma 4 architect → Qwen2.5-Coder-32B editor).

11. **Unsafe tool ACI — raw shell without sandbox (loop pitfall, MEDIUM).** A raw bash shell risks `rm -rf /` and host damage. Execute every CodeAct action inside the Docker container with no writable host mount. One `docker exec` cannot touch the host filesystem.

12. **No git checkpoint between steps (loop pitfall, MEDIUM).** If step 8 fails catastrophically with no commits since step 1, the working state is unrecoverable. Git-commit after every successful file edit so the git log is a rollback ladder.

13. **Reflexion without episodic memory persistence (loop pitfall, MEDIUM).** Self-reflection that is not stored across attempts repeats the same mistake. Write each reflection to Redis (hot) and MongoDB (durable). Condition the next attempt's execute call on retrieved reflections.

14. **Conditional-edge logic reading intra-super-step values (state machine pitfall, MEDIUM).** The router must read only values committed in a *prior* super-step. Reading a state key not yet committed in the current super-step produces stale routing.

15. **Unbounded Goal Tree growth (state machine pitfall, LOW).** The tree accumulates thousands of nodes and bloats the MongoDB checkpoint document. Prune/archive completed subtrees into a separate collection (`goals_archive`). Cap decomposition depth.

16. **Status deadlock — goal stuck IN\_PROGRESS after crash (state machine pitfall, LOW).** Add a TTL sweep (cron job or background task) that resets goals stuck in `IN_PROGRESS` past a deadline (e.g., 30 min) back to `PENDING` for retry.

17. **Context-window blowup from parallel worker transcripts (parallel spawning pitfall, LOW).** Returning full raw outputs from N parallel workers overflows the Gemma 4 orchestrator context. Each sub-agent must return a condensed `Result` (summary + artifact paths), never its full transcript.

18. **Tree-of-Thoughts as the default top-level loop (loop pitfall, LOW).** ToT costs ~100x the compute of ReAct+Plan and empirically does not beat it at equal budget when running real tools against a repo. Reserve for isolated bounded sub-decisions only.

19. **Nested `asyncio.run()` (parallel spawning pitfall, LOW).** Calling `asyncio.run()` from inside a coroutine already on a running loop raises `RuntimeError: cannot be called from a running event loop`. This is especially relevant at the Python MCP client boundary where sync and async code meet.

20. **Over-spawning beyond A6000 capacity (parallel spawning pitfall, LOW).** Launching 10+ workers for a task needing 2 causes GPU VRAM/KV-cache contention on the shared A6000 and is slower due to scheduling overhead. Let the DAG width drive parallelism; cap with the Semaphore to the hardware's real concurrent-decode capacity.

---

## 7. Dependencies

| Library | Version | Purpose | Install |
|---|---|---|---|
| `langgraph` | `>=0.2` | StateGraph runtime, checkpointing, human-in-the-loop, time-travel | `pip install langgraph` |
| `langgraph-checkpoint-mongodb` | latest | `AsyncMongoDBSaver` for durable checkpoint persistence | `pip install langgraph-checkpoint-mongodb` |
| `litellm` | `>=1.40` | Model-agnostic unified LLM API; routes Gemma 4 (architect) and Qwen2.5-Coder-32B (editor) behind one interface | `pip install litellm` |
| `pydantic` | `>=2.0` | Typed Action/Observation/Plan schemas; `model_json_schema()` for prompt injection | `pip install pydantic` |
| `asyncio` | stdlib (3.11+) | TaskGroup (structured concurrency), Semaphore, Lock; core of the async orchestrator | built-in |
| `graphlib` | stdlib (3.9+) | `TopologicalSorter` for DAG cycle detection and ready-set scheduling | built-in |
| `aiolimiter` | `>=1.1` | `AsyncLimiter` leaky-bucket for RPM/TPM rate limiting, event-loop native | `pip install aiolimiter` |
| `tenacity` | `>=8.2` | Async retry with exponential backoff + jitter for transient 429/5xx and worker timeouts | `pip install tenacity` |
| `tiktoken` | `>=0.7` | Pre-flight token estimation for budget gating (approximate for Gemma/Qwen; replace with model-native tokenizer for exact counts) | `pip install tiktoken` |
| `tree-sitter` + `tree-sitter-language-pack` | latest | Incremental parsing to build the Aider-style ranked repo-map / symbol graph | `pip install tree-sitter tree-sitter-language-pack` |
| `networkx` | `>=3.0` | DAG construction, validation, visualization, and ancestor/descendant queries during planning | `pip install networkx` |
| `docker` (docker-py) | `>=7.0` | Container lifecycle management for the sandboxed execution runtime | `pip install docker` |
| `redis-py` | `>=5.0` | Reflexion episodic memory buffer (hot/fast retrieval for next attempt) | `pip install redis` |
| `pymongo` | `>=4.0` | Durable checkpoint + trajectory store; Reflexion long-term episodic memory | `pip install pymongo` |
| `chromadb` | `>=0.5` | Long-term semantic memory / vector retrieval across sessions | `pip install chromadb` |
| `GitPython` | `>=3.1` | Programmatic repo ops: git-checkpoint-per-edit, diff, revert, patch application | `pip install gitpython` |
| `anyio` | `>=4.0` | Backend-agnostic structured concurrency; fallback if not pinned to asyncio | `pip install anyio` |

---

## 8. Reference Papers & Repos

### Papers

| Paper | arXiv ID | Venue | Relevance |
|---|---|---|---|
| ReAct: Synergizing Reasoning and Acting in Language Models (Yao et al. 2023) | 2210.03629 | ICLR 2023 | Canonical Thought→Action→Observation inner loop; backbone of the execute node |
| Reflexion: Language Agents with Verbal Reinforcement Learning (Shinn et al. 2023) | 2303.11366 | NeurIPS 2023 | Actor/evaluator/self-reflection cycle; 91% pass@1 HumanEval vs 80% GPT-4; backs the reflect node |
| Plan-and-Solve Prompting (Wang et al. 2023) | 2305.04091 | ACL 2023 | Front-loaded plan-then-execute; basis for Plan-Execute-Monitor decomposition |
| SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering (Yang et al. 2024) | 2405.15793 | NeurIPS 2024 | ACI design (scoped tools beat raw shell); primary reference for Labmate's tool layer |
| OpenHands / CodeAct: Executable Code Actions Elicit Better LLM Agents (Wang et al. 2024) | 2402.01030 | ICML 2024 | CodeAct unified action space; up to ~20% higher success vs text/JSON across 17 LLMs |
| OpenHands Platform (Wang et al. 2024) | 2407.16741 | ICLR 2025 | Event-stream agent abstraction, AgentDelegateAction multi-agent orchestration |
| ReAcTree: Hierarchical LLM Agent Trees with Control Flow (Choi et al. 2025) | 2511.02424 | AAMAS 2026 | Dynamically expanded Goal Tree with sequence/fallback/parallel nodes; 61% vs 31% ReAct on WAH-NL |
| GoalAct: Global Planning and Hierarchical Execution (Chen et al. 2025) | 2504.16563 | NCIIP 2025 | Global plan + hierarchical execution split; backs the Goal Tree design |
| Mixture-of-Agents Enhances LLM Capabilities (Wang et al. 2024) | 2406.04692 | — | Layered proposer/aggregator; backs the fan-in aggregation step |
| LLM Compiler for Parallel Function Calling (Kim et al. 2023) | 2312.04511 | — | Plan-once/dispatch-many DAG; validates the static fan-out approach |
| Large Language Monkeys: Repeated Sampling (Brown et al. 2024) | 2407.21787 | — | Justifies parallel repeated sampling (best-of-N) as a scaling axis |
| LATS: Language Agent Tree Search (Zhou et al. 2023) | 2310.04406 | ICML 2024 | MCTS over action space; 92.7% HumanEval but 10-30x cost; reserved for isolated hard sub-decisions |
| Agentless (Xia et al. 2024) | 2407.01489 | FSE 2025 | Non-agentic fast path: 27.33% SWE-bench Lite at $0.34/issue |
| AgentBench: Evaluating LLMs as Agents (Liu et al. 2023) | 2308.03688 | ICLR 2024 | 8-environment benchmark; identifies long-horizon reasoning failure modes |
| Tree of Thoughts (Yao et al. 2023) | 2305.10601 | NeurIPS 2023 | ToT/BFS; use selectively NOT as default loop |

### Repos

| Repo | URL | Relevance |
|---|---|---|
| All-Hands-AI/OpenHands | https://github.com/All-Hands-AI/OpenHands | Most directly reusable architecture: event-stream step() abstraction, CodeActAgent, AgentController, Docker sandbox, multi-agent delegation |
| langchain-ai/langgraph | https://github.com/langchain-ai/langgraph | Primary StateGraph runtime + checkpointing |
| SWE-agent/SWE-agent | https://github.com/SWE-agent/SWE-agent | ACI design reference (scoped editor/viewer/search tools) |
| Aider-AI/aider | https://github.com/Aider-AI/aider | Tree-sitter repo map, architect/editor two-model mode, git-native auto-commit |
| noahshinn/reflexion | https://github.com/noahshinn/reflexion | Official Reflexion implementation; actor/evaluator/self-reflection loop |
| togethercomputer/moa | https://github.com/togethercomputer/MoA | Reference MoA layered proposer/aggregator implementation |
| SqueezeAILab/LLMCompiler | https://github.com/SqueezeAILab/LLMCompiler | Plan-and-execute parallel function calling over a task DAG |
| OpenAutoCoder/Agentless | https://github.com/OpenAutoCoder/Agentless | Non-agentic baseline: localize→repair→validate |
| microsoft/autogen | https://github.com/microsoft/autogen | Multi-agent conversation patterns (reference for collaboration) |
| crewAIInc/crewAI | https://github.com/crewAIInc/crewAI | Role-based orchestration (batteries-included alternative) |
| THUDM/AgentBench | https://github.com/THUDM/AgentBench | Benchmark suite for stress-testing long-horizon reasoning |
| anthropics/anthropic-cookbook | https://github.com/anthropics/anthropic-cookbook | Orchestrator-workers and parallelization recipes |

---

## 9. SOTA Improvements (Future Work)

These upgrades are intentionally deferred until the initial version is working and benchmarked.

1. **Agentless fast path as a first-attempt tier.** Before engaging the full agentic loop, run an Agentless-style non-agentic pass (hierarchical localization → diff-format patch → validation). It achieved 27.33% on SWE-bench Lite at $0.34/issue with no LLM-controlled flow. Use it as the cheap first tier; escalate to the ReAct loop only when it fails. This is especially efficient on the A6000 where spinning up the full agentic scaffolding has a meaningful VRAM cost.

2. **OpenHands micro-agent specialization.** Split the monolithic agent into specialized micro-agents: Localizer (identifies affected files), Coder/Editor (generates patches), Verifier (runs tests, interprets results), coordinated by the CodingOrchestrator. Specialized micro-agents outperform a single generalist because each has a smaller, more focused context.

3. **LangGraph `Send()` API for dynamic fan-out.** Replace the static DAG fan-out with `Send()` for tasks where the number of sub-agents is unknown at plan time (e.g., "one worker per changed file"). `Send()` dispatches an arbitrary runtime-determined number of parallel branches and fans in via an `operator.add` reducer, avoiding brittle static planning.

4. **LATS (Language Agent Tree Search) for isolated hard sub-decisions.** Reserve MCTS over the action space (LATS, Zhou et al. 2023) for bounded sub-problems: candidate patch selection, fault localization voting. It scored 92.7% pass@1 on HumanEval with GPT-4 but costs 10–30x per call — gate it behind stuck-detection or difficulty scoring rather than running it as the default.

5. **Streaming condensed results from parallel workers.** Replace batch-collect with async generators: the aggregator begins synthesizing as soon as the first workers complete, reducing perceived latency and orchestrator memory pressure. Especially impactful when one of N workers is significantly slower than the rest.

6. **Speculative parallel execution.** Start the most-likely DAG branches before their dependency outcomes are confirmed; cancel-on-miss via TaskGroup. Trades extra tokens and VRAM for wall-clock latency reduction on the critical path. Only viable once token costs and VRAM pressure are well-characterised.

7. **ACI A/B evaluation (SWE-agent methodology).** Treat the ACI tool set as a tuned, evaluated component: grid-search command names, feedback messages, and scoping on a held-out dev split. Validate against live/held-out benchmarks (SWE-bench-Live, SWE-bench-Pro) to avoid overfitting the scaffold to SWE-bench Verified's static problem set.

8. **First-class reversible context compaction.** Rather than naive truncation or LLM summarization, implement a compaction policy that marks content as re-derivable (full file bodies, stale tool outputs) and re-reads on demand, while maintaining a structured decisions+TODO note that survives across compaction cycles. Add persistent cross-session note-taking so the orchestrator warms up on a given codebase across runs.

9. **Python 3.13 `ExceptionGroup` introspection (`except*`).** Upgrade to Python 3.13's improved `ExceptionGroup` handling so partial multi-agent failures can be triaged per-task in the aggregator rather than collapsing to a single raised exception. This enables graceful degradation: tolerate individual worker failures, surface them in the aggregate result, and continue.

10. **Apache Burr as a lighter StateGraph alternative.** If LangGraph's surface area (and dependency footprint) exceeds what Labmate needs, evaluate Apache Burr: in-process state machine, built-in observability UI, state snapshots, and time-travel replay with a significantly smaller API surface.
