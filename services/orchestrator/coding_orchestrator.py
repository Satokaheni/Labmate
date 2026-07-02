# services/orchestrator/coding_orchestrator.py
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field

import litellm
from aiolimiter import AsyncLimiter

from . import client_context, events
from .completion_guard import is_assertion_verification, reconcile_cutoff, reconcile_ok
from .edit_intent import exposes_bug_intent, requires_editing
from .iteration_budget import REFUNDABLE_TOOLS, IterationBudget
from .load_skill_guard import already_loaded_message, is_repeat_load
from .local_tools import (
    LOCAL_TOOL_NAMES,
    build_sandbox_test_args,
    request_local_tool,
    shape_sandbox_test_result,
    verify_written_content,
)
from .loop_checkpoint import LoopCheckpoint
from .loop_detection import LoopDetector, call_signature, repeat_limit_for
from .memory_search import MemorySearch
from .message_repair import message_repair_enabled, sanitize_messages
from .model_client import acompletion_with_failover, resolve_bases
from .progress_breaker import ProgressBreaker, ProgressStep
from .prompt_assembler import PromptAssembler, measure_prompt_segments
from .sandbox_edits import detect_sandbox_writes
from .steer_inject import inject_steer
from .test_outcome import classify_test_attempt
from .tool_grounding import DEFAULT_TOOL_RESULT_BUDGET, ground_tool_result
from .tool_manifest import client_doc_skills, doc_skill_load_response, manifest_local_tool_names
from .types import State
from .verification_stop import (
    MAX_VERIFY_INFRA_ERRORS,
    build_expose_test_nudge,
    build_infra_unverified_note,
    build_verify_nudge,
    needs_verification,
)

# Max chars of RAW tool output (test results, file contents, bash stdout/stderr,
# skill results) fed back into the ReAct context per tool call. Generous on
# purpose: the weak local model must SEE real evidence, not a 600-char summary.
# Over budget → ground_tool_result keeps a head + tail + marker (end-of-output
# evidence like FAILED/assert lines survives). Replaces the old [:4000]/[:2000]
# hard cuts. See services/orchestrator/tool_grounding.py.
LABMATE_TOOL_RESULT_BUDGET = int(
    os.getenv("LABMATE_TOOL_RESULT_BUDGET", str(DEFAULT_TOOL_RESULT_BUDGET))
)

# Timeout (seconds) for skill tool calls. Must exceed the skill-worker's own
# call timeout so the skill has time to finish before we abort.
SKILL_CALL_TIMEOUT = float(os.getenv("SKILL_CALL_TIMEOUT", "135"))

# Sequencing strategy for react_execute (A/B knob — see eval/seq_ab):
#   skill_first (DEFAULT): a confidently-matched skill runs deterministically and
#       returns — ONE skill per goal, no multi-skill sequencing. This is the
#       well-tested harness path and the one the loop-mechanics tests exercise.
#   react: skip the skill-first fast-path and run the multi-tool ReAct loop, which
#       can chain multiple skills (test-gen -> code-review -> fix) within one goal,
#       at the cost of the loop occasionally bypassing a matching skill.
SEQUENCING_MODE = os.getenv("SEQUENCING_MODE", "skill_first")

# When 1 (default), a load_skill call for a skill ALREADY loaded this goal is
# short-circuited (no real reload) AND the wasted iteration is refunded, so the
# weak local model cannot burn its step budget re-loading the same skills. When
# 0, the redundant reload is still short-circuited but the budget is NOT
# refunded (lets an operator A/B the refund half in isolation).
REFUND_REPEAT_LOAD_SKILL = os.getenv("LABMATE_REFUND_REPEAT_LOAD_SKILL", "1") == "1"

# Durable per-turn inner-loop checkpoint (Option A). OFF by default — the inner
# loop was just stabilized, so this is regression-safe; flip ON after the
# resilience A/B (sibling lite-orchestrator plan) validates it. When OFF,
# _run_react_loop performs ZERO load/save/clear and is byte-identical to today.
ENABLE_LOOP_CHECKPOINT = os.getenv("ENABLE_LOOP_CHECKPOINT", "0") not in (
    "0",
    "false",
    "False",
    "",
)


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------


def _infer_language(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "Python",
        "ts": "TypeScript",
        "js": "JavaScript",
        "rs": "Rust",
        "go": "Go",
        "md": "Markdown",
        "txt": "Text",
        "json": "JSON",
        "yaml": "YAML",
        "yml": "YAML",
        "sh": "Shell",
    }.get(ext, "Text")


def _infer_mime(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "py": "text/x-python",
        "ts": "application/typescript",
        "js": "application/javascript",
        "md": "text/markdown",
        "json": "application/json",
        "sh": "text/x-sh",
    }.get(ext, "text/plain")


def _run_tests_passed(content: str) -> bool:
    """True if a run_tests tool result indicates a pass.

    The run_tests tool returns {"ok": bool, "exit_code": int, "raw_output": str}.
    A result whose JSON has ok True or exit_code 0 is a pass. Non-JSON or an
    error result is treated as NOT passed (the guard stays armed).
    """
    import json as _json

    try:
        data = _json.loads(content)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if "error" in data:
        return False
    if data.get("ok") is True:
        return True
    return data.get("exit_code") == 0


def _run_bash_passed(content: str) -> bool:
    """Best-effort: did a pytest run_bash invocation pass?

    run_bash returns raw stdout/stderr text (or an error JSON blob on
    failure). Treat an error JSON as failed. Otherwise match anchored
    pytest summary patterns to avoid false positives from arbitrary text
    containing 'ok' (e.g. 'ok' in filenames, variable names, etc).
    """
    import json as _json
    import re

    try:
        data = _json.loads(content)
        if isinstance(data, dict) and "error" in data:
            return False
    except (TypeError, ValueError):
        pass

    lowered = str(content).lower()

    # Check for traceback or " error" as explicit failure (not a summary pattern).
    if "traceback" in lowered or " error" in lowered:
        return False

    # Look for anchored pytest summary patterns.
    # A pytest summary with failures looks like "X failed" where X is non-zero.
    # Pattern to detect: digit + "failed" or "error(s)" — this matches "1 failed", "2 errors" etc.
    # We check if the count is non-zero by looking for non-zero leading digit.
    has_failed = re.search(r"\b[1-9]\d*\s+(failed|errors?)\b", lowered) is not None

    # If there are actual failures (count > 0), it failed.
    if has_failed:
        return False

    # Otherwise, look for a passing summary: "X passed" where X > 0.
    has_passed = re.search(r"\b[1-9]\d*\s+passed\b", lowered) is not None

    # If we see tests passing and no non-zero failure count, it passed.
    if has_passed:
        return True

    # Fallback: if no summary patterns found, return False to avoid
    # false positives from incomplete or malformed output.
    return False


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
    tools_used: list[str] = field(default_factory=list)
    # True iff a real verification (run_tests / assertion-backed sandbox run)
    # PASSED during this run. Threaded to the final-answer reconciliation seam in
    # main.py so an honest "tests pass" claim backed by a real run is not
    # downgraded (the react loop's own reconcile_ok already used this; main.py
    # was re-judging the rendered answer WITHOUT it -> false ok=False).
    tests_passed: bool = False


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
        redis=None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.sem = asyncio.Semaphore(max_inflight)
        self.rpm_limiter = AsyncLimiter(rpm, 60)
        self.tpm_limiter = AsyncLimiter(tpm, 60)
        self.budget = TokenBudget(budget)
        self.results: dict[str, Result] = {}
        # Per-task AGENTS.md instructions for the ReAct loop's cached prefix. Defaults
        # to "" so react_execute works when driven directly (tests); the graph execute
        # node propagates the real value from the CodingOrchestrator per task.
        self.agent_instructions: str = ""
        self._qwen_base = qwen_api_base
        self._gemma_base = gemma_api_base
        # Ordered endpoint list for failover (primary + LABMATE_FALLBACK_BASES).
        self._bases = resolve_bases(gemma_api_base)
        self._editor_bases = resolve_bases(qwen_api_base)
        self.skill_router = skill_router
        self.mcp = mcp
        self.codegraph_mcp = None  # set after construction if codegraph-embedder is running
        self.context_manager = None  # set after construction for conversation continuity
        self._active_session_id = ""  # set in execute node to track the current session
        self.memory_search: MemorySearch | None = (
            None  # set after construction when a memory store is wired
        )
        self.workspace = workspace
        self.max_steps = max_steps
        self.redis = redis
        # Injected post-construction by the orchestrator bootstrap when a Mongo
        # handle is available (CheckpointStore over the loop_checkpoints
        # collection). None in unit tests / when checkpointing is unwired.
        self.checkpoint_store = None
        self._now: Callable[[], float] = now if now is not None else time.monotonic

    async def plan_and_dispatch(self, ready_goals: list[dict]) -> list[Result]:
        """
        Validate the ready set as a dependency DAG, then fan-out workers with
        asyncio.TaskGroup for structured concurrency. Returns condensed Results.
        """
        import graphlib

        subtasks = [
            SubTask(id=g["id"], prompt=g["description"], est_tokens=512) for g in ready_goals
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

        # Dispatch by sequencing mode (A/B):
        #   react       — pure multi-tool ReAct loop, no skill-first fast-path (opt-in).
        #   skill_first — deterministic single-skill fast-path, ReAct only when no skill
        #                 matches (default; well-tested harness path).

        # Find-and-fix routing: a goal that needs file edits / verification
        # ("fix", "make the tests pass", "review then fix the code") cannot be
        # served by a single read-only skill dispatch — it must enter the
        # multi-tool ReAct loop so the model can interleave read + edit + run
        # (skills stay callable inside the loop via call_skill_tool). Gated by
        # ROUTE_EDIT_TO_REACT (default ON); when off, behavior is identical to
        # before. No effect in 'react' mode (already runs the loop).
        if SEQUENCING_MODE != "react" and requires_editing(goal):
            return await self._run_react_loop(goal, self.max_steps)

        if SEQUENCING_MODE != "react":
            skilled = await self._run_skill_first(goal)
            if skilled is not None:
                return skilled
        return await self._run_react_loop(goal, self.max_steps)

    async def _run_skill_first(self, goal: str) -> dict | None:
        """Deterministic single-skill execution.

        The selector (SkillRouter.select) is highly reliable at picking the correct
        skill, whereas the free ReAct loop sometimes bypasses a matching skill via
        run_bash/finish. So when a skill clearly matches the goal, run it
        deterministically — select → load body → tool call → dispatch — and return
        the formatted result. Returns None when NO skill matched, so the caller can
        fall through to the ReAct loop.
        """

        if self.skill_router is None:
            return None
        try:
            skill_result = await self.skill_router.run(goal)
        except Exception:
            skill_result = None
        if not isinstance(skill_result, dict):
            return None

        ok = bool(skill_result.get("ok"))
        # When a skill fails (ok=False), prefer the error message.
        # If ok=True, extract the result payload and format it.
        if not ok:
            # Skill failed: surface the MOST SPECIFIC error available, not just the
            # generic discriminator. Worker failure shapes:
            #   tool_error        -> real text in result (MCP content list)
            #   skill_unavailable -> human message in detail
            #   dispatch_failed   -> human message in detail
            # Never the literal "null"; never crash on a None error value.
            err = skill_result.get("error") or "skill failed"
            detail = skill_result.get("detail")
            res = skill_result.get("result")
            if err == "tool_error" and res is not None:
                if isinstance(res, dict) and isinstance(res.get("content"), list):
                    inner = "\n".join(
                        c.get("text", "")
                        for c in res["content"]
                        if isinstance(c, dict) and c.get("text")
                    )
                else:
                    inner = json.dumps(res, default=str)
                text = f"{err}: {inner}" if inner else err
            elif detail:
                text = f"{err}: {detail}"
            else:
                text = err
            text = str(text)[:2000]
        else:
            # Skill succeeded: extract and format the result
            res = skill_result.get("result")
            if isinstance(res, dict) and isinstance(res.get("content"), list):
                # Extract text from content list items
                text_parts = [
                    c.get("text", "")
                    for c in res["content"]
                    if isinstance(c, dict) and c.get("text")
                ]
                # If content list had items but no text fields, use placeholder
                text = "\n".join(text_parts) if text_parts else "(no output)"
            elif isinstance(res, str):
                # Use placeholder for empty strings
                text = res if res else "(no output)"
            elif res is None or res is False:
                # None/missing/empty result: use neutral placeholder
                text = "(no output)"
            else:
                # Structured result: serialize to JSON
                text = json.dumps(res, default=str)
        # Include the skill name in tools_used for curator sequence tracking
        skill_name = skill_result.get("skill_name", "") if isinstance(skill_result, dict) else ""
        tools_list = [skill_name] if skill_name else []
        # Reconcile ok with the answer: a single-skill goal runs no in-loop test
        # verification, so tests_passed=False. The live fix here is the PUNT
        # shape — a read-only skill that returns ok=True with "file too large /
        # provide a snippet" must NOT be reported as a success (report §4.5).
        summary = text[:2000]
        recon_ok, note = reconcile_ok(ok, summary, tests_passed=False)
        if note:
            summary = (summary + " " + note)[:2000]
        return {"ok": recon_ok, "summary": summary, "tools_used": tools_list, "tests_passed": False}

    def _maybe_repair(self, messages: list[dict]) -> list[dict]:
        """Repair the messages list right before a model call, when enabled.

        Drops orphaned tool results and merges illegal adjacent same-role runs
        so malformed sequences (from injected synthetic turns) never reach the
        OpenAI-compatible endpoint. No-op pass-through when the flag is off.
        """
        if message_repair_enabled():
            return sanitize_messages(messages)
        return messages

    @staticmethod
    def _turn_made_progress(*, has_tool_calls: bool, content: str | None, is_finish: bool) -> bool:
        """A ReAct turn made progress if it produced real output: a tool call,
        new non-empty assistant content, or a finish. Used by the no-progress
        breaker to decide whether to increment or reset its idle counter.
        """
        return bool(is_finish or has_tool_calls or (content or "").strip())

    def _checkpoint_active(self, task_id: str | None) -> bool:
        """Checkpointing runs only when the flag is ON, a store is wired, and a
        task_id is available. All three absent in unit tests -> complete no-op."""
        return bool(
            ENABLE_LOOP_CHECKPOINT and self.checkpoint_store is not None and task_id is not None
        )

    async def _run_react_loop(self, goal: str, max_steps: int) -> dict:
        """Multi-tool ReAct loop bounded by ``max_steps``.

        Returns {"ok": bool, "summary": str}. Activation budget is reset by the
        caller (react_execute) at the start of each goal.
        """

        # Build the prefix ONCE per goal. The same frozen system message and tools
        # list are reused on every ReAct step below, so llama-server's longest-common-
        # prefix prompt cache hits and only the appended tail is recomputed.
        assembler = PromptAssembler(
            skill_router=self.skill_router,
            codegraph_enabled=self.codegraph_mcp is not None,
            memory_enabled=self.memory_search is not None,
            client_manifest=client_context.get_manifest(),
            workspace_root=client_context.get_workspace_root(),
            agent_instructions=self.agent_instructions or "",
        )
        tools = assembler.tools()  # frozen list — never rebuilt per step
        # Compute the set of tools that route to the local client (if attached).
        # Falls back to LOCAL_TOOL_NAMES if no manifest is present.
        local_tool_names = manifest_local_tool_names(
            client_context.get_manifest(), LOCAL_TOOL_NAMES
        )
        messages = [
            assembler.system_message(),  # frozen system dict at index 0
        ]

        # Inject conversation continuity (best-effort, after system message before goal)
        if self.context_manager is not None and self._active_session_id:
            try:
                continuity_block = await self.context_manager.conversation_context(
                    self._active_session_id
                )
                if continuity_block:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"CONVERSATION SO FAR (context — the user's new message is below):\n"
                                f"{continuity_block}"
                            ),
                        }
                    )
            except Exception:
                # Best-effort: on any failure, proceed without injection (log to stderr)
                import sys

                print(
                    "warning: conversation-continuity injection failed",
                    file=sys.stderr,
                    flush=True,
                )

        # Append the actual goal message
        messages.append(
            {"role": "user", "content": goal},
        )

        # Per-goal tool-loop detector — halt early if the model repeats the same
        # tool call or cycles a tiny set of calls and would otherwise burn the budget.
        loop_detector = LoopDetector()

        # Per-goal tools accumulation for skill-curator sequence capture.
        _tools_used: list[str] = []

        # Verification-stop guard (hermes pattern). Track which files this run
        # edited and whether a passing verification has been observed. The guard
        # fires ONLY when the model tries to finish after editing without a
        # passing test run, and is capped at MAX_VERIFY_NUDGES.
        edited_files: set[str] = set()
        tests_passed: bool = False
        verify_nudges_used: int = 0
        max_verify_nudges = int(os.getenv("MAX_VERIFY_NUDGES", "2"))
        infra_error_streak: int = 0
        _last_infra_reason: str = "test toolchain error"
        # Inverted success signal: for an "expose the bug" goal a test that RAN and
        # FAILED is the verification (a passing test would NOT expose the bug). When
        # true, a failing run sets tests_passed (the unified verification-met signal)
        # and the verify nudge steers toward RUNNING the test, not making it pass.
        _expose_bug: bool = exposes_bug_intent(goal)
        _bug_exposed: bool = False

        # Skills already loaded THIS goal. A repeat load_skill for a name in
        # this set is short-circuited + refunded (see load_skill dispatch below)
        # so the model stops churning its iteration budget re-loading skills.
        loaded_skills: set[str] = set()

        # ReAct loop — bounded by an IterationBudget (replaces the bare
        # range(max_steps) cap). The budget grants ONE grace turn after
        # exhaustion and refunds cheap read-only iterations (CHEAP_TOOLS).
        # Additionally, record_turn() enforces a hard absolute turn ceiling that
        # cannot be refunded, preventing infinite loops from distinct cheap reads.
        # Edit/fix goals are inherently multi-step (edit -> run tests -> see
        # failure -> edit again), so they get a higher iteration ceiling than
        # read/answer goals. Non-edit goals keep the existing default cap.
        if requires_editing(goal):
            cap = int(os.getenv("LABMATE_MAX_ITERATIONS_EDIT", "12"))
        else:
            cap = int(os.getenv("LABMATE_MAX_ITERATIONS", str(self.max_steps)))
        budget = IterationBudget(max_total=cap)
        # Wall-clock deadline (guard layered on top of step counting). 0 disables.
        deadline_s = float(os.getenv("LABMATE_GOAL_DEADLINE_S", "600"))
        noprogress_limit = int(os.getenv("LABMATE_NOPROGRESS_LIMIT", "5"))
        breaker = ProgressBreaker(default_cap=noprogress_limit)
        start = self._now()
        # Steer deferral: read at turn N, inject at turn N+1.
        # BUT: pre-written steers (available before turn 1) should NOT be injected on turn 1;
        # they should be deferred to turn 2 (unit test expectation).
        # Mid-loop steers (written during turn N, read at turn N+1's top) MUST be injected
        # on turn N+1 (BDD test expectation, because there's no turn N+2).
        # Approach: check if steer exists before loop starts. If yes, defer it.
        # If no, then steers read during the loop are mid-loop and should be injected immediately.
        try:
            _task_id = events.current_task_id()
        except AttributeError:
            _task_id = None

        _prewritten_steer: str | None = None
        if _task_id is not None and self.redis is not None:
            _prewritten_steer = await events.read_and_clear_steer(self.redis, _task_id)

        _pending_steer: str | None = None

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
                loaded_skills = set(_loaded.loaded_skills)
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

        # token-cost Task 1: emit the per-segment token breakdown ONCE per goal
        # (at the first model call) so a live run reveals where the prompt fill goes.
        _measured_segments = False
        try:
            while True:
                # ── Live interrupt: cancel + steer (top of every turn) ──────────
                # task_id comes from the active EventEmitter (set per-task in
                # main._handle); None in unit tests with no emitter / no redis,
                # in which case both checks are skipped and the loop is unchanged.
                try:
                    _task_id = events.current_task_id()
                except AttributeError:
                    _task_id = None  # FakeEmitter in tests lacks _task_id
                _new_midloop_steer = None
                _to_inject = None  # steer to inject into THIS turn's model call

                if _task_id is not None and self.redis is not None:
                    # (1) Cancel — honest partial halt (this is the in-loop cancel
                    #     check that was previously MISSING entirely).
                    if await events.is_cancelled(self.redis, _task_id):
                        await events.emit("turn.cancelled", task_id=_task_id, steps=budget.used)
                        return {
                            "ok": False,
                            "summary": (
                                "cancelled by user mid-turn; partial progress only — "
                                "the requested work was not fully completed"
                            ),
                            "tools_used": _tools_used,
                        }
                    # (2) Steer — handle both pre-written and mid-loop steers differently.
                    #     Pre-written (read before loop): defer to turn 2 (unit test).
                    #     Mid-loop (written during loop): inject immediately on next turn.
                    # Read any newly available steer from mid-loop writes.
                    _new_midloop_steer = await events.read_and_clear_steer(self.redis, _task_id)

                    # Deferral logic for pre-written steers:
                    # On turn 1, set _pending_steer to _prewritten_steer for use on turn 2.
                    # On later turns, use the deferral pattern for mid-loop steers.
                    if _pending_steer is None and _prewritten_steer is not None:
                        # Turn 1 with pre-written steer: defer it to turn 2
                        _pending_steer = _prewritten_steer
                        _prewritten_steer = None  # consumed, don't re-use
                    elif _new_midloop_steer is not None:
                        # Mid-loop steer: inject immediately (don't defer)
                        _to_inject = _new_midloop_steer

                # Wall-clock guard: stop if this goal has run past its deadline.
                if deadline_s > 0 and (self._now() - start) > deadline_s:
                    _ok, _sum = reconcile_cutoff(
                        "wall-clock deadline exceeded",
                        edited_files=edited_files,
                        tests_passed=tests_passed,
                    )
                    return {
                        "ok": _ok,
                        "summary": _sum,
                        "tools_used": _tools_used,
                        "tests_passed": tests_passed,
                    }

                # Hard absolute ceiling (prevents infinite loops of distinct cheap reads).
                if not budget.record_turn():
                    _ok, _sum = reconcile_cutoff(
                        "absolute turn limit exceeded",
                        edited_files=edited_files,
                        tests_passed=tests_passed,
                    )
                    return {
                        "ok": _ok,
                        "summary": _sum,
                        "tools_used": _tools_used,
                        "tests_passed": tests_passed,
                    }

                # Consume one unit; on exhaustion take the single grace turn,
                # else stop with a clear "budget exhausted" outcome.
                if not budget.consume():
                    if not budget.grace():
                        _ok, _sum = reconcile_cutoff(
                            "budget exhausted",
                            edited_files=edited_files,
                            tests_passed=tests_passed,
                        )
                        return {
                            "ok": _ok,
                            "summary": _sum,
                            "tools_used": _tools_used,
                            "tests_passed": tests_passed,
                        }
                    # grace turn: fall through and run one more iteration.

                # Track tools used this turn so a cheap-only turn can be refunded.
                _turn_tools: list[str] = []

                step = budget.used - 1  # for logging (0-indexed)
                # Inject steer only for this model call, without modifying the
                # persistent messages list, so it appears exactly once.
                _messages_for_model = messages
                # Use either the deferred pre-written steer or the immediately-injected mid-loop steer.
                # Defer pre-written steers to turn 2+ (step > 0) so they are injected as corrections
                # to tool results, not as initial instructions. Mid-loop steers are immediate (step-agnostic).
                _steer_this_turn = None
                if step > 0 and _pending_steer is not None:
                    _steer_this_turn = _pending_steer
                elif _to_inject is not None:
                    _steer_this_turn = _to_inject
                if _steer_this_turn:
                    _messages_for_model = inject_steer(messages, _steer_this_turn)
                    if _task_id is not None and self.redis is not None:
                        await events.emit("steer.injected", task_id=_task_id, text=_steer_this_turn)
                    # Clear the pending steer so it is not re-injected on subsequent turns.
                    # Mid-loop steers (_to_inject) are already cleared at line 517 each turn.
                    if _pending_steer is not None:
                        _pending_steer = None

                if not _measured_segments:
                    _measured_segments = True
                    try:
                        import logging as _logging

                        _seg = measure_prompt_segments(assembler, _messages_for_model)
                        _logging.getLogger("orchestrator").info(
                            "token_breakdown session=%s %s", self._active_session_id, _seg
                        )
                    except Exception:
                        pass

                r = await acompletion_with_failover(
                    model="openai/gemma-4-31b",
                    bases=self._bases,
                    api_key="not-needed",
                    messages=self._maybe_repair(_messages_for_model),
                    tools=tools,
                    tool_choice="auto",
                    extra_body={"thinking_budget_tokens": 2048},
                )

                msg = r.choices[0].message

                # Emit reasoning event if present
                _turn_reasoning = events.extract_reasoning(r)
                if _turn_reasoning:
                    await events.emit(
                        "reasoning",
                        node="execute",
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

                # Ensure tool_calls from the actual message are always included in the dict,
                # in case model_dump() returned an empty or missing tool_calls list.
                # This is critical for the sanitizer to correctly identify tool_call_ids
                # and not drop legitimate tool results as orphaned.
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
                        "tools_used": _tools_used,
                    }

                # Process each tool call
                for tc in tool_calls:
                    name = tc.function.name
                    _turn_tools.append(name)
                    # Accumulate all dispatched tools for skill-curator sequence capture
                    # (excluding "finish" which is not a real tool dispatch).
                    if name != "finish":
                        _tools_used.append(name)
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except (json.JSONDecodeError, ValueError):
                        args = {}

                    content = ""

                    if name == "finish":
                        summary = str(args.get("summary", ""))[:2000]
                        _infra_blocked = (
                            bool(edited_files)
                            and not tests_passed
                            and infra_error_streak >= MAX_VERIFY_INFRA_ERRORS
                        )
                        if needs_verification(
                            edited_files,
                            tests_passed,
                            verify_nudges_used,
                            max_verify_nudges,
                            infra_error_streak=infra_error_streak,
                        ):
                            verify_nudges_used += 1
                            await events.emit(
                                "verify.nudge",
                                files=sorted(edited_files),
                                nudge=verify_nudges_used,
                                max_nudges=max_verify_nudges,
                            )
                            # Append synthetic tool result for the finish tool_call
                            # before re-entering the loop, so the message sequence
                            # is valid: assistant(finish) -> tool(finish) -> user(nudge)
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc.id,
                                    "content": json.dumps(
                                        {
                                            "finish_deferred": True,
                                            "reason": "verification required before completion",
                                        }
                                    ),
                                }
                            )
                            messages.append(
                                {
                                    "role": "user",
                                    # Expose-bug goals get a nudge to RUN the test and
                                    # confirm it FAILS (not to make it pass).
                                    "content": (
                                        build_expose_test_nudge(edited_files)
                                        if _expose_bug
                                        else build_verify_nudge(edited_files)
                                    ),
                                }
                            )
                            # Re-enter the loop: do not return, do not run the
                            # remaining tool calls in this assistant turn.
                            break
                        # Either no verification was owed, or the nudge cap was
                        # reached. If we edited without ever verifying, annotate
                        # the summary honestly rather than claiming a pass.
                        if _infra_blocked:
                            _summary = args.get("summary") or ""
                            summary = (
                                _summary
                                + "\n\n"
                                + build_infra_unverified_note(edited_files, _last_infra_reason)
                            ).strip()
                        elif _expose_bug and edited_files and not tests_passed:
                            # Expose-bug goal that wrote a test but never ran it to
                            # confirm it fails — honest note, not a "tests must pass".
                            summary = (
                                summary + " [verification-stop: the test was NOT run "
                                "to confirm it exposes the bug]"
                            )[:2000]
                        elif edited_files and not tests_passed:
                            summary = (
                                summary + " [verification-stop: tests were NOT "
                                "verified to pass within the nudge budget]"
                            )[:2000]
                        # Reconcile the final ok with the finish summary, reusing
                        # the verification-stop guard's tests_passed signal so a
                        # success CLAIM ("I fixed it / tests pass") that was NOT
                        # backed by a passing run_tests this run is gated, and a
                        # punt summary is never reported as a success (§4.5).
                        recon_ok, note = reconcile_ok(True, summary, tests_passed=tests_passed)
                        if note:
                            summary = (summary + " " + note)[:2000]
                        return {
                            "ok": recon_ok,
                            "summary": summary,
                            "tools_used": _tools_used,
                            "tests_passed": tests_passed,
                        }

                    # No-progress / tool-loop detection. finish already returned
                    # above, so only genuinely dispatched tools reach here.
                    # Special case: for repeat load_skill calls, skip the halt check
                    # because we will dedupe them (short-circuit + refund). Still
                    # record the signature for backstop detection in case a true
                    # loop of failed loads occurs.
                    _is_repeat_load_skill = name == "load_skill" and is_repeat_load(
                        args.get("name", ""), loaded_skills
                    )
                    if not _is_repeat_load_skill and loop_detector.record(
                        call_signature(name, args),
                        repeat_limit=repeat_limit_for(name),
                    ):
                        _reason = loop_detector.reason()
                        await events.emit(
                            "loop.detected",
                            tool=name,
                            reason=_reason,
                            signature=call_signature(name, args),
                            steps=step + 1,
                        )
                        import logging as _logging

                        _logging.getLogger("orchestrator").warning(
                            "tool-loop detected (%s) on '%s' at step %d — halting",
                            _reason,
                            name,
                            step + 1,
                        )
                        return {
                            "ok": False,
                            "summary": (
                                f"loop detected ({_reason}): repeated tool "
                                f"'{name}' — halting to avoid burning steps"
                            ),
                            "tools_used": _tools_used,
                        }

                    # For repeat load_skill, record the signature for backstop
                    # loop detection in case a true loop of failed loads occurs.
                    if _is_repeat_load_skill:
                        loop_detector.record(
                            call_signature(name, args),
                            repeat_limit=repeat_limit_for(name),
                        )

                    # Emit tool.start for all non-finish tools
                    _tool_id = uuid.uuid4().hex[:12]
                    _kind, _emit_name = events.tool_event_display(name, args)
                    _t0 = time.monotonic()
                    await events.emit(
                        "tool.start",
                        tool_id=_tool_id,
                        name=_emit_name,
                        kind=_kind,
                        args=args,
                        reasoning_why=_turn_reasoning,
                    )

                    if name == "load_skill" and (
                        self.skill_router is not None or client_context.get_manifest() is not None
                    ):
                        _skill_name = args.get("name", "")
                        if is_repeat_load(_skill_name, loaded_skills):
                            # Already loaded this goal: do NOT reload. Return a
                            # clear "already loaded — call its tools directly"
                            # result and refund the wasted iteration so a churn
                            # of redundant loads cannot starve real work.
                            obs = already_loaded_message(_skill_name, loaded_skills)
                            content = json.dumps(obs)
                            if REFUND_REPEAT_LOAD_SKILL:
                                budget.refund()
                            await events.emit(
                                "load_skill.deduped",
                                name=_skill_name,
                                loaded=sorted(loaded_skills),
                                refunded=REFUND_REPEAT_LOAD_SKILL,
                            )
                        else:
                            # Check if this is a client documentation skill.
                            manifest = client_context.get_manifest()
                            _doc = (
                                client_doc_skills(manifest).get(_skill_name) if manifest else None
                            )

                            if _doc is not None:
                                # Client documentation skill: return its body + a usage
                                # steer so the model follows it directly instead of
                                # inventing a (non-existent) call_skill_tool for it.
                                obs = doc_skill_load_response(_skill_name, _doc["body"])
                                content = json.dumps(obs)
                                loaded_skills.add(_skill_name)
                            else:
                                # Pod skill: dispatch to runner.
                                if self.skill_router is not None:
                                    obs = self.skill_router.runner.load_skill(_skill_name)
                                    content = json.dumps(obs)
                                    # Record a successful first load so a later repeat is
                                    # deduped. Only record on a real 'loaded'/'already_loaded'
                                    # status — an error (unknown skill / cap) must NOT be
                                    # remembered as loaded.
                                    _resp = obs.get("response") if isinstance(obs, dict) else None
                                    _status = (
                                        _resp.get("status") if isinstance(_resp, dict) else None
                                    )
                                    if _skill_name and _status in ("loaded", "already_loaded"):
                                        loaded_skills.add(_skill_name)
                                else:
                                    # No skill_router and not a doc-skill: skill not found.
                                    obs = {
                                        "name": "load_skill",
                                        "response": {
                                            "status": "error",
                                            "message": f"Unknown skill: {_skill_name}",
                                        },
                                    }
                                    content = json.dumps(obs)

                    elif name == "call_skill_tool" and self.skill_router is not None:
                        res = await self.skill_router.execute(
                            args.get("skill", ""),
                            args.get("tool", ""),
                            args.get("arguments", {}),
                        )
                        content = ground_tool_result(json.dumps(res), LABMATE_TOOL_RESULT_BUDGET)
                        # Emit artifact.created if the skill produced a file
                        if isinstance(res, dict):
                            _result = (
                                res.get("result") if isinstance(res.get("result"), dict) else {}
                            )
                            _path = _result.get("path") or _result.get("file") or ""
                            _content_str = _result.get("content") or _result.get("output") or ""
                            if _path and _content_str and isinstance(_content_str, str):
                                try:
                                    await events.emit(
                                        "artifact_created",
                                        artifact={
                                            "id": "art-" + uuid.uuid4().hex[:8],
                                            "name": _path.split("/")[-1] or _path,
                                            "path": _path,
                                            "language": _infer_language(_path),
                                            "mime": _infer_mime(_path),
                                            "sizeBytes": len(_content_str.encode()),
                                            "lineCount": _content_str.count("\n") + 1,
                                            "preview": "code"
                                            if _path.endswith((".py", ".ts", ".js", ".rs", ".go"))
                                            else "doc",
                                            "content": _content_str,
                                            "downloadUrl": f"/artifacts/{_path}",
                                        },
                                    )
                                except Exception:
                                    pass  # artifact emission is best-effort
                        # Verification-stop: an assertion-bearing code-sandbox run
                        # that exits 0 is a real verification for tasks with no
                        # test suite (so a correct fix is not downgraded to ok=False).
                        if is_assertion_verification(
                            args.get("skill", ""),
                            args.get("tool", ""),
                            args.get("arguments", {}),
                            res,
                        ):
                            tests_passed = True
                        # Edit-accounting: files written via code-sandbox (the
                        # workaround used when no local-tool client is attached)
                        # must count as edits so reconcile_cutoff can credit a
                        # verified run.
                        _sb_writes = detect_sandbox_writes(
                            args.get("skill", ""),
                            args.get("tool", ""),
                            args.get("arguments", {}),
                            res,
                        )
                        if _sb_writes:
                            edited_files |= _sb_writes

                    elif name == "run_tests":
                        # First-class test runner. Two paths:
                        # 1. Client-routed when the client declared run_tests.
                        # 2. Pod code-sandbox fallback (when no client attached).
                        # In both cases, apply the same verification accounting
                        # (tests_passed, infra_error_streak, _bug_exposed).
                        if self.redis is not None and "run_tests" in local_tool_names:
                            # Client-routed: tests run on the client side via local
                            # tool handler; we get back {ok, exit_code, raw_output}.
                            try:
                                # Tests can run for minutes — give the round-trip a long
                                # deadline. The client enforces its own timeout and returns
                                # a 124 exit code if it times out.
                                _tms = args.get("timeout_ms")
                                _ms = (
                                    float(_tms)
                                    if isinstance(_tms, int | float) and _tms
                                    else float(os.getenv("LABMATE_TEST_TIMEOUT_MS", "120000"))
                                )
                                _test_timeout_s = max(30.0, _ms / 1000.0 + 15.0)
                                result = await request_local_tool(
                                    self.redis, "run_tests", args, timeout=_test_timeout_s
                                )
                                if isinstance(result, dict):
                                    shaped = {
                                        "ok": bool(result.get("ok")),
                                        "exit_code": int(result.get("exit_code", 1)),
                                        "raw_output": str(result.get("raw_output", ""))[-8000:],
                                    }
                                else:
                                    shaped = {
                                        "ok": False,
                                        "exit_code": 1,
                                        "raw_output": str(result)[-8000:],
                                    }
                                content = ground_tool_result(
                                    json.dumps(shaped), LABMATE_TOOL_RESULT_BUDGET
                                )
                                # SAME verification accounting as the pod path (kept in sync).
                                if _run_tests_passed(content):
                                    tests_passed = True
                                _outcome = classify_test_attempt(content)
                                if _outcome.infra_error:
                                    infra_error_streak += 1
                                    _last_infra_reason = _outcome.reason
                                elif _outcome.ran:
                                    infra_error_streak = 0
                                # Expose-bug inversion: a test that RAN and FAILED
                                # exposes the bug -> that IS the verification.
                                if (
                                    _expose_bug
                                    and _outcome.ran
                                    and not _outcome.passed
                                    and not _outcome.infra_error
                                ):
                                    _bug_exposed = True
                                    tests_passed = True
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        elif self.skill_router is not None:
                            # Pod code-sandbox fallback (no client attached).
                            # pytest is BLOCKED through the generic exec_run bash seam
                            # (sandbox rule), so route to the code-sandbox skill's run_tests
                            # tool, which runs the REAL pytest suite (LocalSubprocessExecutor
                            # on RunPod). Always an ABSOLUTE test_path rooted at the workspace.
                            sb_args = build_sandbox_test_args(args, self.workspace)
                            try:
                                envelope = await self.skill_router.execute(
                                    "code-sandbox",
                                    "run_tests",
                                    sb_args,
                                    timeout=min(sb_args["timeout"] + 15, SKILL_CALL_TIMEOUT),
                                )
                                shaped = shape_sandbox_test_result(envelope)
                                content = ground_tool_result(
                                    json.dumps(shaped), LABMATE_TOOL_RESULT_BUDGET
                                )
                                if _run_tests_passed(content):
                                    tests_passed = True
                                _outcome = classify_test_attempt(content)
                                if _outcome.infra_error:
                                    infra_error_streak += 1
                                    _last_infra_reason = _outcome.reason
                                elif _outcome.ran:
                                    infra_error_streak = 0
                                # Expose-bug inversion: a test that RAN and FAILED
                                # exposes the bug -> that IS the verification.
                                if (
                                    _expose_bug
                                    and _outcome.ran
                                    and not _outcome.passed
                                    and not _outcome.infra_error
                                ):
                                    _bug_exposed = True
                                    tests_passed = True
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "no test runner available"})

                    elif name in local_tool_names:
                        if self.redis is not None:
                            try:
                                result = await request_local_tool(self.redis, name, args)
                                # Reliable write: after a write_file the client may
                                # report success without the bytes landing. Read the
                                # file back and confirm it matches what we asked to
                                # write; surface an explicit error to the model on
                                # mismatch so it cannot claim "code updated" falsely.
                                if name == "write_file":
                                    requested = str(args.get("content", ""))
                                    try:
                                        readback = await request_local_tool(
                                            self.redis,
                                            "read_file",
                                            {"path": args.get("path", "")},
                                        )
                                    except Exception as exc:
                                        readback = f"<read-back failed: {exc}>"
                                    verify_err = verify_written_content(requested, readback)
                                    if verify_err is not None:
                                        content = json.dumps({"error": verify_err})
                                    else:
                                        content = json.dumps(
                                            {"result": result, "verified": True},
                                            default=str,
                                        )
                                        # Verification-stop: record a successful edit.
                                        _path = str(args.get("path", "")).strip()
                                        if _path:
                                            edited_files.add(_path)
                                else:
                                    content = ground_tool_result(
                                        json.dumps({"result": result}, default=str),
                                        LABMATE_TOOL_RESULT_BUDGET,
                                    )
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "no local tool client connected"})

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
                                content = ground_tool_result(
                                    "\n".join(c.text for c in obs.content if hasattr(c, "text")),
                                    LABMATE_TOOL_RESULT_BUDGET,
                                )
                                # Verification-stop: a passing pytest via run_bash also
                                # counts as a verification (secondary signal).
                                if "pytest" in str(args.get("command", "")) and _run_bash_passed(
                                    content
                                ):
                                    tests_passed = True
                                # Track infra errors only for pytest invocations
                                if "pytest" in str(args.get("command", "")):
                                    _bash_outcome = classify_test_attempt(content)
                                    if _bash_outcome.infra_error:
                                        infra_error_streak += 1
                                        _last_infra_reason = _bash_outcome.reason
                                    elif _bash_outcome.ran:
                                        infra_error_streak = 0
                                    # Expose-bug inversion (see run_tests handler).
                                    if (
                                        _expose_bug
                                        and _bash_outcome.ran
                                        and not _bash_outcome.passed
                                        and not _bash_outcome.infra_error
                                    ):
                                        _bug_exposed = True
                                        tests_passed = True
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "no bash runner available"})

                    elif name == "code_semantic_search":
                        if self.codegraph_mcp is not None:
                            try:
                                obs = await self.codegraph_mcp.call_tool(
                                    "code_semantic_search",
                                    {"query": args.get("query", ""), "k": args.get("k", 8)},
                                )
                                content = ground_tool_result(
                                    "\n".join(c.text for c in obs.content if hasattr(c, "text")),
                                    LABMATE_TOOL_RESULT_BUDGET,
                                )
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps(
                                {"error": "codegraph semantic search not available"}
                            )

                    elif name == "memory_search":
                        if self.memory_search is not None:
                            try:
                                content = await self.memory_search.search(
                                    args.get("query", ""),
                                    args.get("k"),
                                )
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "memory search not available"})

                    else:
                        content = json.dumps({"error": f"unknown tool: {name}"})

                    # Emit tool.done — derive status from content (error key = error)
                    try:
                        _parsed = json.loads(content) if isinstance(content, str) else content
                        _td_status = (
                            "error" if isinstance(_parsed, dict) and "error" in _parsed else "done"
                        )
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
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": content,
                        }
                    )

                # Refund this turn if EVERY tool call it made was a refundable read/verify/inspect (REFUNDABLE_TOOLS).
                # Pure inspection (read_file / list_dir / code_semantic_search) and
                # verification (run_tests / run_bash / memory_search) must not starve genuine work.
                # A turn with no tool calls already returned above, so _turn_tools is non-empty here.
                if _turn_tools and all(t in REFUNDABLE_TOOLS for t in _turn_tools):
                    budget.refund()

                # No-progress breaker (after the turn's work). Compute whether
                # this turn advanced; a stalled turn increments the idle count.
                made_progress = self._turn_made_progress(
                    has_tool_calls=bool(tool_calls),
                    content=msg.content,
                    is_finish=False,  # finish already returned above
                )
                pstep: ProgressStep = breaker.step(made_progress, cap=noprogress_limit)
                if pstep.tripped:
                    _ok, _sum = reconcile_cutoff(
                        f"no-progress breaker tripped ({pstep.consecutive} consecutive idle turns)",
                        edited_files=edited_files,
                        tests_passed=tests_passed,
                    )
                    return {
                        "ok": _ok,
                        "summary": _sum,
                        "tools_used": _tools_used,
                        "tests_passed": tests_passed,
                    }

                # Update pending steer for next iteration (defer injection by one turn).
                # For pre-written steers: already handled above (set on turn 1, used on turn 2).
                # For mid-loop steers: never deferred, injected immediately when read.
                # (No update needed here since mid-loop steers are never put into _pending_steer)

                # ── Insertion B: durable inner-loop checkpoint — SAVE turn ─────
                # Best-effort end-of-turn snapshot. A crash before the next model
                # call resumes here (Insertion A) on the next run_task().
                if self._checkpoint_active(_cp_task_id):
                    await self.checkpoint_store.save(
                        LoopCheckpoint(
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
                            loaded_skills=sorted(loaded_skills),
                            start_monotonic_offset=self._now() - start,
                            turn=budget.used,
                        )
                    )

        except Exception as exc:
            return {"ok": False, "summary": f"error: {str(exc)[:1000]}", "tools_used": _tools_used}
        finally:
            # ── Insertion C: durable inner-loop checkpoint — CLEAR on exit ─────
            # Every terminal path (return or exception) flows through here, so a
            # finished/aborted goal never leaves a stale checkpoint to be wrongly
            # resumed by a later same-task run.
            if self._checkpoint_active(_cp_task_id):
                await self.checkpoint_store.clear(_cp_task_id)

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
                            tools_used=ret.get("tools_used", []),
                            tests_passed=ret.get("tests_passed", False),
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
        r = await acompletion_with_failover(
            model="openai/gemma-4-31b",
            bases=self._bases,
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
        # Ordered endpoint lists for failover (primary + LABMATE_FALLBACK_BASES).
        self._bases = resolve_bases(gemma_api_base)
        self._editor_bases = resolve_bases(qwen_api_base)
        self.max_iter = max_iter
        self.stuck_n = stuck_n
        self.mcp = mcp  # MCPClientManager | None
        self.agent_instructions: str = ""  # set per-task from AGENT.md
        self.skill_router = skill_router  # SkillRouter | None
        self.context_manager = None  # set after construction for conversation continuity
        self._recent_actions: list[str] = []
        self._gate_futures: dict[str, asyncio.Future] = {}

    async def run_task(
        self,
        task: str,
        session_id: str,
        user_id: str = "",
        workspace_id: str = "",
        agent_instructions: str = "",
    ) -> dict:
        """
        Entry point. Pass the same session_id to resume after a crash.
        Returns the final State dict.

        Routing is single-intent only (the multi-intent decompose path + routing_mode
        A/B toggle were removed after an A/B showed no quality benefit at higher cost).
        """
        from .types import create_goal

        # Set AGENT.md instructions for this task — used by _build_messages()
        self.agent_instructions = agent_instructions

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
            "verify_retries": 0,  # FIX 9: bound verify->reflect passes
            "direct_answer": False,  # FIX 10: set True by the plan node's direct-answer fast-path
        }
        cfg = {
            "configurable": {
                "thread_id": session_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
            }
        }
        return await self.graph.ainvoke(initial, cfg)

    def _build_messages(self, prompt: str) -> list[dict]:
        """Prepend AGENT.md as a system message when present."""
        if self.agent_instructions:
            return [
                {"role": "system", "content": self.agent_instructions},
                {"role": "user", "content": prompt},
            ]
        return [{"role": "user", "content": prompt}]

    async def architect(
        self, prompt: str, thinking_budget: int = 3000, max_tokens: int | None = None
    ) -> str:
        """
        Planning, self-reflection, aggregation -> Gemma 4 31B dense.

        thinking_budget controls per-request reasoning depth via llama.cpp's
        thinking_budget_tokens field (only honored when server started without
        --reasoning-budget flag). Pass thinking_budget=0 for fast tool-dispatch nodes.

        max_tokens caps the CONTENT length (separate from thinking_budget, which only
        caps reasoning). Structured pre-flight calls (ambiguity triage, routing) MUST
        pass a tight cap — without it the model can ramble a small JSON out to thousands
        of content tokens (~3.8k observed live), the dominant pre-answer latency cost.
        Leave None for answer-generating calls (direct answer, reflect) that need length.
        """
        r = await acompletion_with_failover(
            model="openai/gemma-4-31b",
            bases=self._bases,
            api_key="not-needed",
            messages=self._build_messages(prompt),
            extra_body={"thinking_budget_tokens": thinking_budget},
            **({"max_tokens": max_tokens} if max_tokens is not None else {}),
        )
        return r.choices[0].message.content

    async def editor(self, prompt: str, thinking_budget: int = 2048) -> str:
        """Code generation, file edits -> Qwen2.5-Coder-32B (or Gemma when QWEN_BASE==GEMMA_BASE).

        thinking_budget must always be set: post-April-2026 llama.cpp builds default
        to INT_MAX if omitted, which can cause non-deterministic hangs.
        """
        r = await acompletion_with_failover(
            model="openai/qwen2.5-coder-32b",
            bases=self._editor_bases,
            api_key="not-needed",
            messages=self._build_messages(prompt),
            extra_body={"thinking_budget_tokens": thinking_budget},
        )
        return r.choices[0].message.content

    def is_stuck(self, action_key: str) -> bool:
        """
        Returns True if the last stuck_n actions are all identical.
        Triggers escalation from the inner ReAct loop to a fresh Plan-Execute pass.
        """
        self._recent_actions.append(action_key)
        self._recent_actions = self._recent_actions[-self.stuck_n :]
        return len(self._recent_actions) == self.stuck_n and len(set(self._recent_actions)) == 1

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
                text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
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
            stream = await acompletion_with_failover(
                model="openai/gemma-4-31b",
                bases=self._bases,
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

    async def stream(
        self, prompt: str, user_id: str = "", workspace_id: str = ""
    ) -> AsyncGenerator[str, None]:
        """Async generator — run a task and yield the final answer as a single chunk.

        Used by the CLI connector and future frontend connectors.
        A future version may yield incremental tokens as they are produced.
        """
        if self.graph is None:
            raise RuntimeError(
                "graph not wired — call build_graph(orch) and assign orch.graph before streaming"
            )
        import uuid

        session_id = str(uuid.uuid4())
        state = await self.run_task(prompt, session_id, user_id=user_id, workspace_id=workspace_id)
        root = state.get("goal_tree", {}).get("root", {})
        if state.get("awaiting_clarification"):
            yield (
                state.get("clarification_question", "")
                or state.get("final_answer")
                or root.get("result", "")
                or str(state)
            )
            return
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
