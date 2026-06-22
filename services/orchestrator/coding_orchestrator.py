# services/orchestrator/coding_orchestrator.py
from __future__ import annotations

import asyncio
import litellm
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncGenerator
from aiolimiter import AsyncLimiter

from .types import Goal, State, Status, get_ready_goals, update_status, now_iso
from . import events


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
        skill_router=None,
        mcp=None,
        workspace: str = ".",
        max_steps: int = 6,
    ) -> None:
        self.sem = asyncio.Semaphore(max_inflight)
        self.rpm_limiter = AsyncLimiter(rpm, 60)
        self.tpm_limiter = AsyncLimiter(tpm, 60)
        self.budget = TokenBudget(budget)
        self.results: dict[str, Result] = {}
        self._qwen_base = qwen_api_base
        self._gemma_base = gemma_api_base
        self.skill_router = skill_router
        self.mcp = mcp
        self.workspace = workspace
        self.max_steps = max_steps

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
                if running:
                    # Wait for at least one task to complete before re-polling
                    done, pending = await asyncio.wait(
                        running.values(), return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        # Find which tid this task belongs to
                        for tid, t in list(running.items()):
                            if t is task:
                                del running[tid]
                                if not task.cancelled():
                                    ts.done(tid)
                                break
                else:
                    # No ready tasks and nothing running: yield control briefly
                    await asyncio.sleep(0.01)

        return list(self.results.values())

    async def react_execute(self, goal: str) -> dict:
        """
        ReAct executor: tool-using loop bounded by max_steps.

        Returns {"ok": bool, "summary": str}

        Tools available:
        - load_skill (when skill_router is present)
        - call_skill_tool (when skill_router is present)
        - run_bash (always)
        - finish (always)
        """
        import json

        # Reset the per-task skill-activation budget. SkillRunner.load_skill caps
        # activations at max_chain PER TASK; without a reset the counter accrues
        # across the process lifetime and load_skill starts failing after ~8 goals
        # (silently breaking routing for every later goal). Reset at this per-goal
        # boundary so each goal gets a fresh budget.
        if self.skill_router is not None:
            try:
                self.skill_router.runner.reset_activations()
            except Exception:
                pass

        # ── Skill-first deterministic routing ────────────────────────────────
        # The selector (SkillRouter.select) is highly reliable at picking the
        # correct skill (18/18 in live smoke tests), whereas the free ReAct loop
        # below sometimes bypasses a matching skill via run_bash/finish. So when a
        # skill clearly matches the goal, run it deterministically — select →
        # load body (progressive disclosure) → tool call → dispatch — and return.
        # Fall through to the ReAct loop only when NO skill matches (run() is None).
        if self.skill_router is not None:
            try:
                skill_result = await self.skill_router.run(goal)
            except Exception:
                skill_result = None
            if isinstance(skill_result, dict):
                ok = bool(skill_result.get("ok"))
                # When a skill fails (ok=False), prefer the error message.
                # If ok=True, extract the result payload and format it.
                if not ok:
                    # Skill failed: surface the error message, never the literal "null"
                    text = skill_result.get("error", "skill failed")
                else:
                    # Skill succeeded: extract and format the result
                    res = skill_result.get("result")
                    if isinstance(res, dict) and isinstance(res.get("content"), list):
                        text = "\n".join(
                            c.get("text", "") for c in res["content"]
                            if isinstance(c, dict) and c.get("text")
                        )
                    elif isinstance(res, str):
                        text = res
                    else:
                        # Structured result: serialize to JSON
                        text = json.dumps(res, default=str)
                return {"ok": ok, "summary": text[:2000]}

        # Build tool list
        tools = []
        if self.skill_router is not None:
            tools.append(self.skill_router.runner.tool_schema())
            # Only include call_skill_tool when skill_router is available
            tools.append({
                "type": "function",
                "function": {
                    "name": "call_skill_tool",
                    "description": "Execute a tool within a loaded skill.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill": {"type": "string", "description": "Skill name"},
                            "tool": {"type": "string", "description": "Tool name"},
                            "arguments": {"type": "object", "description": "Tool arguments"},
                        },
                        "required": ["skill", "tool", "arguments"],
                    },
                },
            })

        # Always include run_bash and finish
        tools.extend([
            {
                "type": "function",
                "function": {
                    "name": "run_bash",
                    "description": "Run a bash command in the workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Bash command"},
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finish",
                    "description": "Finish the task and return the summary.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "description": "Task summary"},
                        },
                        "required": ["summary"],
                    },
                },
            },
        ])

        # Build system prompt with catalog
        catalog = ""
        if self.skill_router is not None:
            catalog = self.skill_router.runner.catalog_prompt()

        system = (
            "You are an execution agent with access to specialized SKILLS plus a generic shell. "
            "CRITICAL RULE: if ANY available skill matches the task, you MUST accomplish it with "
            "that skill — call load_skill(name) to read its instructions, then "
            "call_skill_tool(skill, tool, arguments) to run the right tool. Do NOT use run_bash to "
            "hand-replicate what a skill already does (e.g. do not grep/sed/write files yourself "
            "when a code-search, test-generation, parsing, audit, or documentation skill exists). "
            "Use run_bash ONLY when no available skill fits the task. "
            "Do NOT call finish until the work is actually done — and when a matching skill exists, "
            "finish only AFTER call_skill_tool has returned its result. Call finish(summary) to end. "
            "SANDBOX RULE: run_bash is for read-only inspection (ls, cat, grep, git status) only. "
            "Any code you author or execute — Python, Node, shell scripts, pytest — MUST go through "
            "the code-sandbox skill (load_skill('code-sandbox') then call_skill_tool), NEVER run_bash."
        )
        if catalog:
            system += f"\n\n{catalog}"

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": goal},
        ]

        # ReAct loop
        try:
            for step in range(self.max_steps):
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
                    # Fallback for responses without model_dump (FIX #3):
                    # Include tool_calls in OpenAI format when present, so tool messages have valid preceding assistant entry
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

                    # Emit tool.start for all non-finish tools
                    _tool_id = uuid.uuid4().hex[:12]
                    _kind = "skill" if name == "call_skill_tool" else "tool"
                    _emit_name = args.get("skill", name) if name == "call_skill_tool" else name
                    _t0 = time.monotonic()
                    await events.emit(
                        "tool.start",
                        tool_id=_tool_id,
                        name=_emit_name,
                        kind=_kind,
                        args=args,
                        reasoning_why=_turn_reasoning,
                    )

                    if name == "load_skill" and self.skill_router is not None:
                        obs = self.skill_router.runner.load_skill(args.get("name", ""))
                        content = json.dumps(obs)

                    elif name == "call_skill_tool" and self.skill_router is not None:
                        res = await self.skill_router.execute(
                            args.get("skill", ""),
                            args.get("tool", ""),
                            args.get("arguments", {}),
                        )
                        content = json.dumps(res)[:4000]

                    elif name == "run_bash":
                        if self.mcp is not None:
                            try:
                                obs = await self.mcp.call_tool(
                                    "exec_run",
                                    {
                                        "command": args.get("command", ""),
                                        "cwd": self.workspace,
                                        "timeout": 30000,
                                    },
                                )
                                content = "\n".join(
                                    c.text for c in obs.content if hasattr(c, "text")
                                )
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "no bash runner available"})

                    else:
                        content = json.dumps({"error": f"unknown tool: {name}"})

                    # Emit tool.done — derive status from content (error key = error)
                    try:
                        _parsed = json.loads(content) if isinstance(content, str) else content
                        _td_status = "error" if isinstance(_parsed, dict) and "error" in _parsed else "done"
                    except Exception:
                        _td_status = "done"
                    await events.emit(
                        "tool.done",
                        tool_id=_tool_id,
                        status=_td_status,
                        summary=str(content)[:200],
                        result=content,
                        duration_ms=int((time.monotonic() - _t0) * 1000),
                    )

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

    async def _run_worker(self, t: SubTask) -> str:
        """
        Execute a single sub-task worker.
        1. Reserve token budget (wait in loop, never dispatch on bust).
        2. Acquire Semaphore inside the worker (never at the fan-out site).
        3. Rate-limit then call react_execute (skill-aware ReAct loop).
        4. Condense and store the result.
        5. Re-raise CancelledError — NEVER swallow it.
        6. For non-cancellation exceptions, store failed Result and return normally
           (do NOT re-raise to preserve sibling results).
        """
        while not await self.budget.reserve(t.est_tokens):
            await asyncio.sleep(0.5)

        try:
            async with self.sem:
                async with self.rpm_limiter:
                    async with self.tpm_limiter:
                        ret = await self.react_execute(t.prompt)
                        self.results[t.id] = Result(
                            id=t.id,
                            summary=ret["summary"],
                            ok=ret["ok"],
                        )
        except asyncio.CancelledError:
            await self.budget.refund(t.est_tokens)
            raise  # never swallow — keeps TaskGroup correct
        except Exception as exc:
            await self.budget.refund(t.est_tokens)
            # Store the failed result and return normally (do NOT re-raise).
            # Re-raising would cause TaskGroup to cancel all siblings, losing their results.
            # This is a worker-level error, not a coordination error.
            self.results[t.id] = Result(
                id=t.id,
                summary=f"worker error: {str(exc)[:200]}",
                ok=False,
            )
            return t.id

        return t.id

    async def _call_qwen_worker(self, t: SubTask) -> str:
        """
        DEPRECATED: Route to Qwen2.5-Coder-32B (specialist editor) via litellm.
        This method is no longer used (_run_worker delegates to react_execute instead).
        Kept for backwards compatibility but should not be revived without setting
        extra_body={"thinking_budget_tokens": ...} per CLAUDE.md rule 6.
        """
        r = await litellm.acompletion(
            model="openai/qwen2.5-coder-32b",
            api_base=self._qwen_base,
            api_key="not-needed",
            messages=[{"role": "user", "content": t.prompt}],
            extra_body={"thinking_budget_tokens": 2048},
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
            api_key="not-needed",
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
        skill_router=None,
    ) -> None:
        self.graph = graph
        self.workspace = workspace_path
        self.container = docker_container
        self._gemma_base = gemma_api_base
        self._qwen_base = qwen_api_base
        self.max_iter = max_iter
        self.stuck_n = stuck_n
        self.mcp = mcp          # MCPClientManager | None
        self.skill_router = skill_router  # SkillRouter | None
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
            "root_goal": task,
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
            api_key="not-needed",
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
            api_key="not-needed",
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

    async def stream_final_answer(self, task: str, final_state: dict) -> str:
        """Compose the user-facing reply with a streamed LLM call.

        Emits answer.delta per chunk (typewriter effect) and answer.done at the end.
        Best-effort: on any error, returns the assembled final_answer without raising.
        """
        assembled = ""
        if isinstance(final_state, dict):
            assembled = (
                final_state.get("final_answer")
                or final_state.get("goal_tree", {}).get("root", {}).get("result", "")
                or ""
            )
        prompt = (
            "Write a concise, friendly answer to the user's request using the results below. "
            "Do not mention tools, skills, or internal steps.\n\n"
            f"Request: {task}\n\nResults:\n{assembled}"
        )
        acc = ""
        try:
            stream = await litellm.acompletion(
                model="openai/gemma-4-31b",
                api_base=self._gemma_base,
                api_key="not-needed",
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                extra_body={"thinking_budget_tokens": 0},
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    acc += delta
                    await events.emit("answer.delta", text=delta)
            await events.emit("answer.done", text=acc)
            return acc or assembled
        except Exception as exc:
            import logging
            _log = logging.getLogger("orchestrator")
            _log.warning("stream_final_answer failed, using assembled answer: %s", exc)
            return assembled

    async def stream(self, prompt: str, user_id: str = "", workspace_id: str = "") -> "AsyncGenerator[str, None]":
        """Async generator — run a task and yield the final answer as a single chunk.

        Used by the CLI connector and future frontend connectors.
        A future version may yield incremental tokens as they are produced.
        """
        if self.graph is None:
            raise RuntimeError("graph not wired — call build_graph(orch) and assign orch.graph before streaming")
        import uuid
        session_id = str(uuid.uuid4())
        state = await self.run_task(prompt, session_id, user_id=user_id, workspace_id=workspace_id)
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

        Tolerates "nothing to commit" (CalledProcessError with exit code 1 when
        there are no staged changes), which is common for read-only skills
        like ast-repo-map or web-search that produce no working-tree changes.
        """
        def _commit() -> None:
            subprocess.run(["git", "-C", self.workspace, "add", "-A"], check=True)
            try:
                subprocess.run(
                    ["git", "-C", self.workspace, "commit", "-m", message],
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                # Exit code 1 means "nothing to commit" — common for read-only skills.
                # Allow it; re-raise anything else.
                if exc.returncode != 1:
                    raise

        await asyncio.to_thread(_commit)
