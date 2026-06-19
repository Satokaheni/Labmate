# services/orchestrator/coding_orchestrator.py
from __future__ import annotations

import asyncio
import litellm
import subprocess
from dataclasses import dataclass, field
from typing import AsyncGenerator
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
        """Strip the raw transcript to summary + artifact paths (max 2000 chars)."""
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
        mcp=None,
    ) -> None:
        self.graph = graph
        self.workspace = workspace_path
        self.container = docker_container
        self._gemma_base = gemma_api_base
        self._qwen_base = qwen_api_base
        self.max_iter = max_iter
        self.stuck_n = stuck_n
        self.mcp = mcp          # MCPClientManager | None
        self._recent_actions: list[str] = []
        self._gate_futures: dict[str, asyncio.Future] = {}

    async def run_task(
        self,
        task: str,
        session_id: str,
        user_id: str = "",
        workspace_id: str = "",
    ) -> dict:
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
            "final_answer": "",
            "workspace_id": workspace_id,
            "user_id": user_id,
        }
        cfg = {
            "configurable": {
                "thread_id": session_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
            }
        }
        return await self.graph.ainvoke(initial, cfg)

    async def architect(self, prompt: str, thinking_budget: int = 3000) -> str:
        """
        Planning, self-reflection, aggregation -> Gemma 4 31B dense.

        thinking_budget controls per-request reasoning depth via llama.cpp's
        thinking_budget_tokens field (only honored when server started without
        --reasoning-budget flag). Pass thinking_budget=0 for fast tool-dispatch nodes.
        """
        r = await litellm.acompletion(
            model="openai/gemma-4-31b",
            api_base=self._gemma_base,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking_budget_tokens": thinking_budget},
        )
        return r.choices[0].message.content

    async def editor(self, prompt: str, thinking_budget: int = 2048) -> str:
        """Code generation, file edits -> Qwen2.5-Coder-32B (or Gemma when QWEN_BASE==GEMMA_BASE).

        thinking_budget must always be set: post-April-2026 llama.cpp builds default
        to INT_MAX if omitted, which can cause non-deterministic hangs.
        """
        r = await litellm.acompletion(
            model="openai/qwen2.5-coder-32b",
            api_base=self._qwen_base,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking_budget_tokens": thinking_budget},
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

    async def run_in_sandbox(self, cmd: str, timeout_ms: int = 30_000) -> dict:
        """Async sandbox execution — routes through MCP bridge when available.

        When self.mcp is set, calls exec_run on the MCP bridge (works on
        RunPod and any environment where docker exec is unavailable).
        Falls back to execute_in_sandbox() via asyncio.to_thread when no MCP
        client is present (e.g. tests, local dev without the bridge).
        """
        if self.mcp is not None:
            try:
                result = await self.mcp.call_tool(
                    "exec_run",
                    {"command": cmd, "cwd": self.workspace, "timeout": timeout_ms},
                )
                text = "\n".join(
                    c.text for c in result.content if hasattr(c, "text")
                )
                is_error = bool(result.isError)
                return {
                    "stdout": text,
                    "stderr": "",
                    "exit_code": 1 if is_error else 0,
                    "ok": not is_error,
                }
            except Exception as exc:
                return {"stdout": "", "stderr": str(exc), "exit_code": 1, "ok": False}

        return await asyncio.to_thread(self.execute_in_sandbox, cmd, 60)

    async def stream(self, prompt: str) -> "AsyncGenerator[str, None]":
        """Async generator — run a task and yield the final answer as a single chunk.

        Used by the CLI connector and future frontend connectors.
        A future version may yield incremental tokens as they are produced.
        """
        if self.graph is None:
            raise RuntimeError("graph not wired — call build_graph(orch) and assign orch.graph before streaming")
        import uuid
        session_id = str(uuid.uuid4())
        state = await self.run_task(prompt, session_id)
        root = state.get("goal_tree", {}).get("root", {})
        yield state.get("final_answer") or root.get("result", "") or str(state)

    # ── Human-in-the-loop gate interface (approve / reject via any connector) ──

    def _gate_future(self, task_id: str) -> asyncio.Future:
        """Return (or create) the pending gate Future for task_id."""
        loop = asyncio.get_running_loop()
        if task_id not in self._gate_futures:
            self._gate_futures[task_id] = loop.create_future()
        return self._gate_futures[task_id]

    async def pending_gate(self, task_id: str) -> bool:
        return task_id in self._gate_futures and not self._gate_futures[task_id].done()

    async def approve_gate(self, task_id: str) -> None:
        fut = self._gate_futures.pop(task_id, None)
        if fut and not fut.done():
            fut.set_result("approve")

    async def reject_gate(self, task_id: str) -> None:
        fut = self._gate_futures.pop(task_id, None)
        if fut and not fut.done():
            fut.set_result("reject")

    async def git_checkpoint(self, message: str) -> None:
        """Async checkpoint — runs git add + commit in a thread pool worker.

        Always uses subprocess directly; the MCP bridge exposes read-only git
        tools (status/diff/log) and has no git_commit tool.
        """
        def _commit() -> None:
            subprocess.run(["git", "-C", self.workspace, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", self.workspace, "commit", "-m", message],
                check=True,
            )

        await asyncio.to_thread(_commit)
