# services/orchestrator/graph.py
from __future__ import annotations

import json
import logging
import os

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from .types import State, Status, Goal, get_ready_goals, update_status, now_iso, create_goal
from .coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
from . import events
from .task_complexity import classify_complexity

_log = logging.getLogger("graph")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
# llama.cpp serves Gemma 4 31B on port 8000 (CUDA on RunPod, Metal on Mac Mini, Vulkan on AMD).
# On 48 GB (A6000 / Mac Mini): both servers co-reside.
# On 32 GB discrete GPU: run one server at a time; set QWEN_BASE = GEMMA_BASE for model-swap mode.
GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
# On single-GPU setups, QWEN_BASE defaults to GEMMA_BASE (Gemma 4 serves both roles).
# On dual-GPU, set QWEN_BASE=http://localhost:8001/v1 to enable the specialist Qwen worker.
QWEN_BASE  = os.getenv("QWEN_BASE",  GEMMA_BASE)

# A1: tasks at or above this ambiguity score route to the approval gate before planning.
AMBIGUITY_THRESHOLD = float(os.getenv("AMBIGUITY_THRESHOLD", "0.6"))

# A2: artifacts scoring below this route back through reflect for revision.
CRITIQUE_THRESHOLD = float(os.getenv("CRITIQUE_THRESHOLD", "0.90"))

# Critique is a heavy multi-call skill (writing CoVe fans out one LLM call per
# verification question), so it needs a longer dispatch budget than the router's
# default 60s. Must stay UNDER the skill-worker's CALL_TIMEOUT (default 120s) so
# the worker writes the result before the orchestrator stops polling — otherwise
# the gate times out and silently fails open. Override via CRITIQUE_DISPATCH_TIMEOUT.
CRITIQUE_DISPATCH_TIMEOUT = float(os.getenv("CRITIQUE_DISPATCH_TIMEOUT", "110"))

# FIX 10 (A3): thinking budget for the assess_ambiguity node's architect() call
#   (was hardcoded 1024; a lower default is faster on the local Q4 model).
# PERF: 768 thinking tokens ≈ 25s of decode on the Q4 host for a single yes/no-ish
# ambiguity judgement that runs on EVERY request. 384 roughly halves that with
# negligible effect on the binary "is this ambiguous" call; raise via env if the
# gate starts missing genuinely ambiguous prompts.
ASSESS_THINKING_BUDGET = int(os.getenv("ASSESS_THINKING_BUDGET", "384"))

# FIX 10 (B2): direct-answer fast-path. When route() reports a skill-less SINGLE intent
# (e.g. "What is 2+2?"), the plan node answers directly via architect() and HALTS the
# graph instead of architect-decomposing + re-routing through execute/ReAct (which would
# re-run select() and even dispatch a skill to compute a trivial answer). This collapses
# ~10 model calls / ~65 s down to a single architect() call.
ENABLE_DIRECT_ANSWER_FASTPATH = os.getenv("ENABLE_DIRECT_ANSWER_FASTPATH", "1") not in (
    "0",
    "false",
    "False",
    "",
)
DIRECT_ANSWER_THINKING_BUDGET = int(os.getenv("DIRECT_ANSWER_THINKING_BUDGET", "1024"))

# FIX 9: latency knobs for the reflect/verify gate.
# MAX_VERIFY_RETRIES: how many times a low critique_score may drive a verify->reflect pass
#   before the artifact is ACCEPTED and we proceed to check. 1 -> at most one reflect pass;
#   0 -> verify is advisory and never loops.
MAX_VERIFY_RETRIES = int(os.getenv("MAX_VERIFY_RETRIES", "1"))
# PERF/FIT: which artifact types run the automatic critique verify-gate. The critique
# skill scores output against an academic-paper constitution (C1 citations, C4 "no new
# numbers", IMRaD section structure) via a slow Q4 evaluator call. That rubric fits a
# paper draft — but NOT chat answers and NOT code (code isn't IMRaD-structured, and the
# gate runs with no tests/lint signals, so on code it's just an LLM second-guessing
# itself against a citation rubric). It also adds ~50-70s/request and "routinely scores
# a correct artifact < 0.90" before the gate accepts it anyway. So the automatic gate is
# OFF by default (empty set → verify always skips). Paper critique is still available
# on demand: an explicit "critique this draft" request routes to the critique skill
# directly. To re-enable the auto-gate for a type set CRITIQUE_ARTIFACT_TYPES=writing
# (or code,writing).
CRITIQUE_ARTIFACT_TYPES = tuple(
    t.strip() for t in os.getenv("CRITIQUE_ARTIFACT_TYPES", "").split(",") if t.strip()
)
# MAX_GOAL_ATTEMPTS: how many times a FAILED goal may be reflect-retried (replaces the old
#   hardcoded cap of 3). A goal with attempts >= MAX_GOAL_ATTEMPTS is considered exhausted.
MAX_GOAL_ATTEMPTS = int(os.getenv("MAX_GOAL_ATTEMPTS", "2"))
# REFLECT_THINKING_BUDGET: thinking budget for the reflect node's architect() call
#   (was hardcoded 3000; lower is faster on the local Q4 model).
REFLECT_THINKING_BUDGET = int(os.getenv("REFLECT_THINKING_BUDGET", "1500"))

# Task 3: import the error classifier and add rate-limit knobs
from .error_classifier import classify_error, ErrorClass, is_terminal

# RATE_LIMITED handling: a 429 may be retried a bounded number of times with a
# short backoff before being treated as exhausted. Keep these small — on the
# local pod a sustained 429 (e.g. Semantic Scholar without a key) will recur.
MAX_RATE_LIMIT_RETRIES = int(os.getenv("MAX_RATE_LIMIT_RETRIES", "1"))
RATE_LIMIT_BACKOFF_SECONDS = float(os.getenv("RATE_LIMIT_BACKOFF_SECONDS", "2.0"))

# Revise-before-deliver (opt-in). A lightweight single-pass gate that re-reads the
# final answer against the task and may produce ONE revised answer before delivery.
# OFF by default — like the critique auto-gate — so it never adds latency to every
# answer. ORTHOGONAL to the heavy critique verify-gate (verify node); this is the
# general-purpose lightweight pass for all task types.
ENABLE_FINALIZE_REVISION = os.getenv("ENABLE_FINALIZE_REVISION", "0") not in (
    "0",
    "false",
    "False",
    "",
)
MAX_FINALIZE_REVISIONS = int(os.getenv("MAX_FINALIZE_REVISIONS", "1"))
FINALIZE_REVISION_THINKING_BUDGET = int(
    os.getenv("FINALIZE_REVISION_THINKING_BUDGET", "1024")
)

from .finalize_revision import should_revise, build_revision_prompt


def classify_artifact(text: str) -> str:
    """Cheap heuristic artifact classifier for the A2 verify gate."""
    if not text:
        return "other"
    code_markers = ("```", "def ", "class ", "import ", "function ", "=>", "};", "public ")
    if any(m in text for m in code_markers):
        return "code"
    if len(text.strip()) > 200:
        return "writing"
    return "other"


# Maximum number of prior reflections (most-recent) fed back into a retry's
# diagnosis prompt. Bounded so the prompt cannot grow unboundedly across
# many retries; deterministic ordering (chronological) for reproducibility.
MAX_PRIOR_REFLECTIONS = 3


def collect_prior_reflections(
    state: "State",
    goal_id: str,
    cap: int = MAX_PRIOR_REFLECTIONS,
) -> list[str]:
    """Return prior reflection texts recorded for `goal_id`, in chronological
    order, keeping only the last `cap`.

    Pure: reads only `state["messages"]`. Reflection messages are the dicts
    appended by the reflect node, shaped
    ``{"role": "reflection", "goal_id": <id>, "content": <text>}``.
    Entries that are not reflections, or that lack a matching ``goal_id``
    (e.g. legacy pre-change reflections, or reflections for other goals),
    are ignored. ``cap <= 0`` yields an empty list.
    """
    messages = state.get("messages") or []
    matches = [
        m["content"]
        for m in messages
        if isinstance(m, dict)
        and m.get("role") == "reflection"
        and m.get("goal_id") == goal_id
        and "content" in m
    ]
    if cap <= 0:
        return []
    return matches[-cap:]


def _run_had_side_effects(state: "State") -> bool:
    """Conservative heuristic: did this run execute a side-effecting tool/skill?

    Revising after side effects is unsafe (the answer describes actions already
    taken), so the revise gate must skip in that case. We approximate "had side
    effects" from the LAST artifact's type: 'code' artifacts come from skills that
    write/edit files or run code in the sandbox (a side effect); 'writing' and
    'other' are read-only prose answers. This is intentionally conservative — when
    unsure, prefer NOT revising. Pure: reads only state['last_artifact'].
    """
    artifact = state.get("last_artifact") or {}
    return artifact.get("type") == "code"


def make_nodes(orch: CodingOrchestrator, async_orch: AsyncOrchestrator):
    """
    Factory that closes over the orchestrator instances so nodes are plain
    async functions (no class state on the graph itself).
    """

    async def plan(state: State) -> dict:
        """
        Route the current root goal as ONE intent (single-intent routing).
        If a skill confidently matches, create ONE child Goal and proceed to execute.
        Otherwise, take the direct-answer fast-path (answer directly and HALT).
        """
        import copy
        import uuid

        root_id = state["current_goal_id"]
        goal_desc = state["goal_tree"][root_id]["description"]

        # Include skill catalog in the prompt if available
        catalog = ""
        skill_router = getattr(orch, "skill_router", None)
        if skill_router is not None:
            catalog = skill_router.runner.catalog_prompt()

        # Call route() to handle single-intent routing (only if skill_router has the
        # route method; the architect fallback below preserves backward compatibility
        # for old tests / when route() raises because live services are unavailable).
        route_result = None
        try:
            if skill_router is not None and hasattr(skill_router, "route"):
                route_result = await skill_router.route(goal_desc)
        except Exception as e:
            # Fallback on any route() error (LLM unavailable, network error, TypeError, etc.)
            _log.debug("route() failed, falling back to architect: %s", e)
            route_result = None

        if route_result is not None:
            # Single-intent routing: at most ONE child goal.
            #   - a skill confidently matched -> create ONE child Goal and proceed to
            #     execute (ReAct/one-shot handles any sub-steps in a single pass).
            #   - no skill matched -> direct-answer fast-path below.
            if route_result.skills:
                tree = copy.deepcopy(state["goal_tree"])
                child_id = uuid.uuid4().hex[:12]
                create_goal(tree, child_id, root_id, route_result.sub_intents[0])
                return {"goal_tree": tree, "awaiting_clarification": False}

            # FIX 10 (B3): direct-answer fast-path for a skill-less intent.
            # route() returns (skills=[], needs_clarification=False, sub_intents=[the_intent])
            # for a trivial / no-skill-needed task (e.g. "What is 2+2?"). Answer directly with
            # ONE architect() call, mark root COMPLETED, and HALT (clarification_router sees
            # final_answer/direct_answer and returns END).
            if ENABLE_DIRECT_ANSWER_FASTPATH:
                answer = await orch.architect(
                    goal_desc, thinking_budget=DIRECT_ANSWER_THINKING_BUDGET
                )
                tree = copy.deepcopy(state["goal_tree"])
                update_status(tree, root_id, Status.COMPLETED, result=answer)
                await events.emit(
                    "reasoning",
                    node="plan",
                    summary="answering directly (no skill needed)",
                    text=answer[:500],
                )
                return {
                    "goal_tree": tree,
                    "final_answer": answer,
                    "direct_answer": True,
                }

        # Fallback when no skill_router or no route() method (for old tests and backward compat)
        prompt = f"Decompose this task into concrete subtasks (one per line):\n{goal_desc}"
        if catalog:
            prompt += f"\n\nAvailable skills:\n{catalog}\nIf the whole task is accomplishable by a single available skill, emit ONE subtask describing that work. Otherwise produce a small number of coherent subtasks, each a self-contained unit that may map to a skill."

        raw_plan = await orch.architect(prompt)
        # Deep copy to avoid mutating the checkpoint's prior goal_tree.
        tree = copy.deepcopy(state["goal_tree"])
        children_created = 0
        for i, line in enumerate(raw_plan.strip().splitlines()):
            if line.strip():
                gid = f"{root_id}_sub{i}"
                create_goal(tree, gid, root_id, line.strip())
                children_created += 1
        await events.emit(
            "reasoning",
            node="plan",
            summary=f"decomposed into {children_created} subtask(s)",
            text=raw_plan[:500],
        )
        return {"goal_tree": tree}

    async def execute_node(state: State) -> dict:
        """
        Execute all ready PENDING goals via AsyncOrchestrator.plan_and_dispatch().
        This ensures all goals (including single ones) are processed through the
        skill-aware ReAct executor, preserving concurrency for multiple goals.
        Includes an idempotency guard via step_markers that tracks per-GOAL-ID completion,
        not result hash, so retries after reflect can produce different outcomes.
        """
        import copy

        ready = get_ready_goals(state["goal_tree"])
        if not ready:
            return {}

        # Deep copy to avoid mutating the checkpoint's prior goal_tree.
        tree = copy.deepcopy(state["goal_tree"])
        markers = dict(state["step_markers"])

        results = await async_orch.plan_and_dispatch(ready)
        last_artifact = {"type": "other", "payload": ""}
        error_class_seen: str | None = None
        _accumulated_tools: list[str] = []  # accumulate tools for skill-curator
        _tests_passed = False  # True if any completed goal verified via a passing test run
        for r in results:
            gid = r.id
            # Idempotency guard (FIX #4): mark per-GOAL-ID only when COMPLETED.
            if markers.get(gid) == "completed":
                continue
            new_status = Status.COMPLETED if r.ok else Status.FAILED
            if not r.ok:
                # Classify the failure to decide retry vs. terminal. Replaces the
                # crude _NONRETRYABLE_ERROR_MARKERS substring test (FIX 9).
                cls = classify_error(r.summary or "")
                error_class_seen = cls.value
                tree[gid]["error_class"] = cls.value
                if is_terminal(cls):
                    # Environmental/deterministic: will fail identically on every
                    # retry -> mark EXHAUSTED so check()/router() finalize with the
                    # honest error instead of paying reflect()+re-execute per attempt.
                    tree[gid]["attempts"] = MAX_GOAL_ATTEMPTS
                elif cls == ErrorClass.RATE_LIMITED:
                    # Bounded backoff + capped retries. Cap is the SMALLER of the
                    # rate-limit cap and the goal cap so a 429 never out-retries a
                    # normal failure.
                    prior = tree[gid].get("attempts", 0)
                    rl_cap = min(MAX_RATE_LIMIT_RETRIES, MAX_GOAL_ATTEMPTS)
                    if prior >= rl_cap:
                        tree[gid]["attempts"] = MAX_GOAL_ATTEMPTS  # exhausted
                    else:
                        if RATE_LIMIT_BACKOFF_SECONDS > 0:
                            import asyncio
                            await asyncio.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                        tree[gid]["attempts"] = prior + 1
                else:
                    # TRANSIENT or RETRYABLE (unknown) -> normal increment.
                    tree[gid]["attempts"] = tree[gid].get("attempts", 0) + 1
            if r.ok:
                update_status(tree, gid, new_status, result=r.summary)
            else:
                update_status(tree, gid, new_status, result=r.summary, error=r.summary)
            if r.ok:
                markers[gid] = "completed"
            if r.ok and r.summary:
                last_artifact = {
                    "type": classify_artifact(r.summary),
                    "payload": r.summary,
                }
            # Accumulate tools_used from this result
            if r.ok and hasattr(r, "tools_used") and isinstance(r.tools_used, list):
                _accumulated_tools.extend(r.tools_used)
            # Track whether any completed goal verified via a passing test run, so
            # the final-answer reconciliation in main.py can honor an honest
            # "tests pass" claim instead of downgrading it (tests_passed was lost
            # between the react loop and the rendered-answer re-check).
            if r.ok and getattr(r, "tests_passed", False):
                _tests_passed = True

        out: dict = {
            "goal_tree": tree,
            "step_markers": markers,
            "last_artifact": last_artifact,
        }
        if error_class_seen is not None:
            out["error_class"] = error_class_seen
        # Thread tools_used into state for skill-curator capture
        if _accumulated_tools:
            out["tools_used"] = _accumulated_tools
        # Thread the verified-tests signal for main.py's final-answer reconciliation.
        if _tests_passed:
            out["tests_passed"] = True
        return out

    async def check(state: State) -> dict:
        """
        Finalize root when all children are terminal; handle failed retryables.
        If a child failed but has attempts < 3, defer finalization and route to reflect.
        (FIX #1/#2: Stop masking failures as success)
        """
        import copy
        tree = copy.deepcopy(state["goal_tree"])
        root_id = "root"
        root = tree.get(root_id, {})

        # FIX (regression): after FIX 3 the sub-intents form a nested CHAIN under root
        # (root -> subN -> ... -> sub1), so root["children"] holds ONLY the last
        # sub-intent. Iterating root["children"] therefore (a) drops every nested
        # sub-intent's result from the final answer and (b) risks finalizing as soon
        # as the single direct child is terminal while deeper descendants are still
        # PENDING. Walk the FULL subtree (all transitive descendants of root) instead.
        descendants: list[tuple[int, str]] = []
        seen: set[str] = set()
        stack: list[tuple[int, str]] = [(1, c) for c in root.get("children", [])]
        while stack:
            depth, cid = stack.pop()
            if cid in tree and cid not in seen:
                seen.add(cid)
                descendants.append((depth, cid))
                stack.extend((depth + 1, gc) for gc in tree[cid].get("children", []))
        # Order DEEPEST-first so the final answer lists sub-intents in SUBMISSION
        # order (FIX 3 makes the first sub-intent the deepest leaf). Tie-break on id
        # for determinism within a depth.
        descendants.sort(key=lambda dc: (-dc[0], dc[1]))
        children = [cid for _depth, cid in descendants]
        if not children:
            return {}

        # FIX (regression from FIX 3): a FAILED descendant PERMANENTLY blocks all of
        # its ancestors in the nested chain — every goal upstream of it stays PENDING
        # forever because its child never COMPLETED, so get_ready_goals() never
        # releases them and all_terminal is never reached. If we only handled the
        # failed_retryable branch inside the all_terminal block, a mid-chain failure
        # would fall through to `return {}` and the router would END the task with no
        # final_answer and no error (the failed sub-intent never reaching reflect).
        # So detect retryable FAILED descendants FIRST, regardless of all_terminal,
        # and route to reflect for the deepest-first one so it can be retried.
        failed_retryable = [
            c for c in children
            if tree.get(c, {}).get("status") == Status.FAILED.value
            and tree.get(c, {}).get("attempts", 0) < MAX_GOAL_ATTEMPTS  # FIX 9: was hardcoded 3
        ]
        if failed_retryable:
            # Don't finalize; defer to reflect by routing to first failed child.
            # (children is ordered deepest-first / submission order, so this retries
            # the earliest-submitted failed sub-intent first.)
            await events.emit(
                "reasoning",
                node="check",
                summary="deferring to reflect for failed child retry",
                text=f"Child {failed_retryable[0]} failed but retryable (attempts < {MAX_GOAL_ATTEMPTS})",
            )
            return {"goal_tree": tree, "current_goal_id": failed_retryable[0]}

        # Only finalize when every descendant has reached a terminal status.
        # (No retryable failures remain at this point — a FAILED-and-exhausted
        # descendant still blocks its ancestors at PENDING, so guard finalization
        # below on whether finalization is actually possible.)
        all_terminal = all(
            tree.get(c, {}).get("status") in (Status.COMPLETED.value, Status.FAILED.value)
            for c in children
        )
        if not all_terminal:
            # Non-terminal descendants remain but NONE are retryable-failed (the
            # branch above handled those). The only way a descendant stays
            # non-terminal here is being PENDING behind a FAILED-and-exhausted
            # (attempts >= 3) descendant that permanently blocks it. Finalize as a
            # failure rather than silently ending with no answer.
            exhausted_failed = [
                c for c in children
                if tree.get(c, {}).get("status") == Status.FAILED.value
            ]
            if not exhausted_failed:
                # Genuinely still in progress (no failures): keep waiting.
                return {}
            # Fall through to finalization below, surfacing the exhausted failure(s).

        # All retryable failures handled above; no retryables remain: FINALIZE
        # Build answer from all children, noting failures
        completed_results = [
            f"**{tree[c]['description'][:80]}**\n{tree[c]['result']}"
            for c in children
            if tree.get(c, {}).get("result")
        ]
        failed_children = [
            c for c in children
            if tree.get(c, {}).get("status") == Status.FAILED.value
        ]
        answer = "\n\n".join(completed_results) if completed_results else "Task completed."
        if failed_children:
            failed_summary = "; ".join(
                f"{tree[c]['description'][:40]} (error: {(tree[c].get('error') or 'unknown')[:50]})"
                for c in failed_children
            )
            answer += f"\n\nFailed subtasks: {failed_summary}"

        # Determine root status: FAILED if any child failed, else COMPLETED
        failed_any = bool(failed_children)
        final_status = Status.FAILED if failed_any else Status.COMPLETED
        update_status(tree, root_id, final_status, result=answer)

        await events.emit(
            "reasoning",
            node="check",
            summary="finalizing" if not failed_any else f"{len(failed_children)} child(ren) failed",
            text=answer[:500],
        )

        result = {
            "goal_tree": tree,
            "final_answer": answer,
            "current_goal_id": root_id,
        }
        if failed_any:
            failed_summary = "; ".join(
                f"{tree[c]['description'][:40]} (error: {(tree[c].get('error') or 'unknown')[:50]})"
                for c in failed_children
            )
            result["error"] = f"{len(failed_children)} subtask(s) failed: {failed_summary}"
        return result

    async def reflect(state: State) -> dict:
        """
        Reflexion: write a natural-language diagnosis to episodic memory.
        Conditions the next execute attempt on the stored reflection AND on
        any prior reflections for THIS goal, so retries escalate strategy
        instead of repeating the same diagnosis.
        """
        import copy
        gid = state["current_goal_id"]
        goal = state["goal_tree"][gid]

        priors = collect_prior_reflections(state, gid)
        prior_section = ""
        if priors:
            numbered = "\n".join(f"  {i}. {p}" for i, p in enumerate(priors, 1))
            prior_section = (
                "\nPreviously tried (do NOT repeat these fixes — they already failed):\n"
                f"{numbered}\n"
                "Propose a DIFFERENT approach and escalate your strategy.\n"
            )

        reflection = await orch.architect(
            f"The following subtask failed (attempt {goal['attempts']}):\n"
            f"Goal: {goal['description']}\n"
            f"Error: {goal['error']}\n"
            f"{prior_section}"
            "Write a concise diagnosis and what to do differently on the next attempt.",
            thinking_budget=REFLECT_THINKING_BUDGET,  # FIX 9: was 3000
        )
        await events.emit(
            "reasoning",
            node="reflect",
            summary="diagnosing failed subtask",
            text=reflection[:500],
        )
        # Deep copy to avoid mutating the checkpoint's prior goal_tree.
        tree = copy.deepcopy(state["goal_tree"])
        update_status(tree, gid, Status.PENDING)
        return {
            "goal_tree": tree,
            # Tag with goal_id so collect_prior_reflections can attribute this
            # reflection on the next retry of the same goal.
            "messages": [{"role": "reflection", "goal_id": gid, "content": reflection}],
        }

    async def approval(state: State) -> dict:
        """
        Human-in-the-loop gate before irreversible actions.
        interrupt() checkpoints state and suspends execution until the thread is resumed.
        """
        import copy
        gid = state["current_goal_id"]
        # NOTE: the "awaiting approval" reasoning event is emitted by the upstream
        # assess_ambiguity node (which completes and is checkpointed before this node),
        # NOT here. interrupt() re-runs this whole node on resume, so emitting here would
        # duplicate the event on every resume. assess_ambiguity fires it exactly once.
        decision = interrupt({"action": "irreversible", "goal": gid})
        # Deep copy to avoid mutating the checkpoint's prior goal_tree.
        tree = copy.deepcopy(state["goal_tree"])
        new_status = Status.IN_PROGRESS if decision == "approve" else Status.BLOCKED
        update_status(tree, gid, new_status)
        return {"goal_tree": tree}

    async def assess_ambiguity(state: State) -> dict:
        goal = state.get("root_goal") or state["goal_tree"][state["current_goal_id"]]["description"]
        prompt = (
            "You are triaging a task before an autonomous agent executes it.\n"
            f"TASK: {goal}\n\n"
            "List the assumptions an agent must make to act on this as written. "
            "Then rate overall ambiguity from 0.0 (fully specified) to 1.0 (critically "
            "underspecified).\n\n"
            "The score measures ONE thing only: is the CORE objective underspecified? "
            "Score it on whether the agent can tell WHAT to do, not on whether every "
            "execution detail is pinned down.\n\n"
            "Score HIGH (>= 0.6, typically 0.7-0.9) ONLY when the CORE task is "
            "underspecified, i.e. it has any of:\n"
            "  - an undefined referent (e.g. \"it\", \"the thing\", \"this\") with no "
            "antecedent naming the actual object,\n"
            "  - no concrete deliverable (you cannot tell what artifact to produce),\n"
            "  - undefined success criteria (you'd have to guess what 'done' means, or "
            "no target/metric is given for a 'make it better'-style request),\n"
            "  - essential information missing without which you cannot meaningfully "
            "begin.\n\n"
            "Score LOW (~0.0-0.3) when the core objective is clear and actionable, EVEN "
            "IF minor execution parameters are unstated. Unstated MINOR parameters are "
            "NOT ambiguity — the agent should assume reasonable defaults and proceed. "
            "These do NOT raise the score:\n"
            "  - output format (list vs table vs JSON),\n"
            "  - count or quantity (how many results/items),\n"
            "  - which specific library, method, or algorithm to use,\n"
            "  - styling, naming, or other cosmetic choices.\n\n"
            "Examples:\n"
            "  \"make it better\" -> 0.85  (undefined referent, no deliverable)\n"
            "  \"fix the thing\" -> 0.9  (undefined referent, no deliverable)\n"
            "  \"improve performance\" (no system/target/metric) -> 0.75  (undefined "
            "success criteria)\n"
            "  \"search the Hugging Face Hub for emotion classification datasets\" -> 0.1 "
            " (clear objective; format and count are defaults, not ambiguity)\n"
            "  \"write a python function that reverses a string\" -> 0.1  (the "
            "implementation method is just a default to pick)\n"
            "  \"what is 2+2?\" -> 0.0\n"
            "  \"add a docstring to the reverse_string function in utils.py\" -> 0.1\n\n"
            "When ambiguity is high, set \"blocking_question\" to the single most useful "
            "question to ask the user; otherwise leave it empty.\n"
            "Respond as JSON: "
            '{"assumptions": ["..."], "ambiguity": 0.0, "blocking_question": "" }'
        )
        raw = await orch.architect(prompt, thinking_budget=ASSESS_THINKING_BUDGET)  # FIX 10 (A3): was 1024
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
                raise ValueError("not an object")
        except (json.JSONDecodeError, ValueError):
            out = {}
        try:
            ambiguity = float(out.get("ambiguity", 0.0))
        except (TypeError, ValueError):
            ambiguity = 0.0

        assumptions = out.get("assumptions", []) or []
        blocking_question = out.get("blocking_question", "") or ""
        # Human-readable reasoning text for connectors (the CLI renders this verbatim):
        # the blocking question when ambiguous, else a short assumptions line — never raw JSON.
        if blocking_question:
            reasoning_text = blocking_question
        elif assumptions:
            reasoning_text = "Assuming: " + "; ".join(str(a) for a in assumptions[:3])
        else:
            reasoning_text = ""
        await events.emit(
            "reasoning",
            node="assess_ambiguity",
            summary=f"ambiguity={ambiguity:.2f}; {len(assumptions)} assumption(s)",
            text=reasoning_text,
        )

        result = {
            "root_goal": goal,
            "assumptions": assumptions,
            "ambiguity": ambiguity,
            "blocking_question": blocking_question,
        }

        # Task 3: Call complexity classifier to determine skip flags (if feature enabled).
        # The classifier is pure and deterministic, always returns the same result for
        # a given task. Stash both the classifier verdict and the committed skip flags
        # in the result dict so StateGraph commits them to the session state.
        complexity = classify_complexity(goal)
        result["complexity"] = {
            "skip_ambiguity": complexity.skip_ambiguity,
            "skip_verify": complexity.skip_verify,
            "reason": complexity.reason,
        }
        # Stash the skip flags as top-level state keys so the routers can read them.
        result["skip_ambiguity"] = complexity.skip_ambiguity
        result["skip_verify"] = complexity.skip_verify

        # On high ambiguity, HALT and ask the user a clarifying question rather than
        # guessing. ambiguity_router sends this node's output to END; main._handle /
        # coding_orchestrator.stream surface clarification_question as the answer and
        # suppress any guess. Reuse the same clarification_request event the plan node
        # emits so downstream consumers see one consistent shape.
        # HOWEVER: if skip_ambiguity is True (trivial task), honor that decision and
        # never halt for clarification even if the LLM scores the task ambiguous.
        if ambiguity >= AMBIGUITY_THRESHOLD and not complexity.skip_ambiguity:
            question = blocking_question or "Could you clarify what you'd like me to do?"
            await events.emit(
                "clarification_request",
                question=question,
                task=goal,
                session_id=state.get("session_id", ""),
            )
            result["awaiting_clarification"] = True
            result["clarification_question"] = question

        return result

    async def verify(state: State) -> dict:
        """
        A2: critique code/writing artifacts. Dispatches the critique skill via
        the orchestrator's skill_router. Defaults score to 1.0 (pass) on any
        failure so a critique outage never blocks the pipeline.
        """
        artifact = state.get("last_artifact") or {"type": "other", "payload": ""}
        atype = artifact.get("type")
        router_obj = getattr(orch, "skill_router", None)
        if atype not in CRITIQUE_ARTIFACT_TYPES or router_obj is None:
            await events.emit(
                "reasoning",
                node="verify",
                summary=f"skipped (artifact type '{atype}' not in critique set)",
                text="",
            )
            # FIX 9: nothing to critique -> never reflect.
            return {
                "verified": True,
                "critique_score": 1.0,
                "critique_notes": "",
                "_verify_reflect": False,
            }

        score = 1.0
        notes = ""
        try:
            res = await router_obj.execute(
                "critique",
                "critique",
                {
                    "output": artifact.get("payload", ""),
                    "task": state.get("root_goal", ""),
                    "critique_type": atype,
                },
                timeout=CRITIQUE_DISPATCH_TIMEOUT,
            )
            if isinstance(res, dict) and res.get("ok"):
                payload = res.get("result")
                if isinstance(payload, dict):
                    score = float(payload.get("score", 1.0))
                    notes = str(payload.get("notes", ""))
        except Exception:
            score = 1.0
            notes = ""

        # FIX 9: bound verify-driven reflection. A below-threshold artifact reflects at most
        # MAX_VERIFY_RETRIES times; after that we ACCEPT it and proceed to check. This stops
        # the local Q4 critique (which routinely scores a CORRECT artifact < 0.90) from
        # re-critiquing/re-generating a completed answer indefinitely.
        prior_retries = state.get("verify_retries", 0)
        should_reflect = (score < CRITIQUE_THRESHOLD) and (prior_retries < MAX_VERIFY_RETRIES)

        await events.emit(
            "reasoning",
            node="verify",
            summary=f"critique_score={score:.2f}",
            text=notes,
        )
        result: dict = {
            "verified": True,
            "critique_score": score,
            "critique_notes": notes,
            "_verify_reflect": should_reflect,
        }
        if should_reflect:
            # Increment the committed counter so the next verify pass eventually accepts.
            result["verify_retries"] = prior_retries + 1
        return result

    async def revise(state: State) -> dict:
        """Opt-in lightweight revise-before-deliver gate.

        After `check` finalized the answer (final_answer set) and BEFORE delivery,
        make at most MAX_FINALIZE_REVISIONS bounded model calls to re-read the
        answer against the task and optionally replace it. Pass-through (returns {})
        when disabled or when should_revise() says it is unsafe/unnecessary, so the
        delivered answer is identical to today.
        """
        import copy

        if not ENABLE_FINALIZE_REVISION:
            return {}

        final_answer = state.get("final_answer") or ""
        errored = state.get("error") is not None
        attempts = int(state.get("finalize_revisions", 0))
        had_side_effects = _run_had_side_effects(state)

        if not should_revise(
            final_answer,
            had_side_effects=had_side_effects,
            attempts=attempts,
            max_attempts=MAX_FINALIZE_REVISIONS,
            errored=errored,
        ):
            return {}

        task = state.get("root_goal") or final_answer
        prompt = build_revision_prompt(task, final_answer)
        revised = await orch.architect(
            prompt, thinking_budget=FINALIZE_REVISION_THINKING_BUDGET
        )
        revised = (revised or "").strip()
        # A blank revision is a model glitch — keep the original answer but still
        # spend the attempt so the gate stays bounded/idempotent.
        out_answer = revised if revised else final_answer

        await events.emit(
            "reasoning",
            node="revise",
            summary="revised final answer" if revised and revised != final_answer
            else "kept final answer unchanged",
            text=out_answer[:500],
        )

        # Mirror final_answer onto the root goal's result so every delivery path
        # (stream_final_answer reads final_answer; clarification/direct paths read
        # final_answer) sees the revised text.
        tree = copy.deepcopy(state["goal_tree"]) if state.get("goal_tree") else None
        if tree is not None and "root" in tree:
            update_status(tree, "root", Status(tree["root"]["status"]), result=out_answer)

        result: dict = {
            "final_answer": out_answer,
            "finalize_revisions": attempts + 1,
            "revised": bool(revised and revised != final_answer),
        }
        if tree is not None:
            result["goal_tree"] = tree
        return result

    return plan, execute_node, check, reflect, approval, assess_ambiguity, verify, revise


def router(state: State) -> str:
    """
    Route after the 'check' node. Reads only values committed in prior
    super-steps — never intra-super-step values.
    Finalization (final_answer set) is the terminal signal.
    (FIX #2: make finalization terminal so failed FAILED root doesn't loop)
    """
    # If final_answer is set, check node finalized the tree — run the opt-in
    # revise-before-deliver gate before ending. The revise node is a pass-through
    # (returns {}) when the feature is disabled, so delivery is identical to today.
    if state.get("final_answer"):
        return "revise"

    gid = state.get("current_goal_id")
    if gid is None:
        return END

    goal = state["goal_tree"].get(gid)
    if goal is None:
        return END

    if goal["status"] == Status.FAILED.value and goal["attempts"] < MAX_GOAL_ATTEMPTS:  # FIX 9: was < 3
        return "reflect"
    if goal["status"] == Status.AWAITING_APPROVAL.value:
        return "approval"
    if get_ready_goals(state["goal_tree"]):
        return "execute"
    return END


def ambiguity_router(state: State) -> str:
    """A1: route after assess_ambiguity. Task 3: honors skip_ambiguity flag before
    the threshold check. On high ambiguity, HALT (END) so the agent asks the user a
    clarifying question instead of guessing; otherwise plan."""
    # Task 3: If skip_ambiguity is True, short-circuit and proceed directly to plan
    if state.get("skip_ambiguity"):
        return "plan"
    if float(state.get("ambiguity", 0.0)) >= AMBIGUITY_THRESHOLD:
        return END
    return "plan"


def verify_router(state: State) -> str:
    """A2 / FIX 9 / Task 3: route after verify. Task 3: honors skip_verify flag before
    the threshold check. The verify node decides (and commits) whether a reflect pass is
    warranted via `_verify_reflect`, which is True only when the artifact scored below
    CRITIQUE_THRESHOLD AND fewer than MAX_VERIFY_RETRIES verify->reflect passes have been
    taken. This router is a pure function of that committed flag, so the verify<->reflect
    loop is bounded (it cannot loop forever).

    Backward-compat: if `_verify_reflect` was never set by the verify node (e.g. a hand-built
    state in older tests), fall back to the original threshold comparison."""
    # Task 3: If skip_verify is True, short-circuit and proceed directly to check
    if state.get("skip_verify"):
        return "check"
    if "_verify_reflect" in state:
        return "reflect" if state.get("_verify_reflect") else "check"
    if float(state.get("critique_score", 1.0)) < CRITIQUE_THRESHOLD:
        return "reflect"
    return "check"


def clarification_router(state: State) -> str:
    """Route after the 'plan' node. Halt the graph (END) when the plan node has
    already produced a terminal outcome, so the agent does NOT proceed to execute
    and guess / redo work. This is END when the plan node:
      - requested clarification (awaiting_clarification), so we don't guess at an
        ambiguous task; OR
      - FIX 10: took the direct-answer fast-path (direct_answer / final_answer set),
        so we don't re-decompose and re-route a question the plan already answered.
    Otherwise continue to execute."""
    if state.get("awaiting_clarification") or state.get("direct_answer") or state.get("final_answer"):
        return END
    return "execute"


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

def build_graph(
    orch: CodingOrchestrator,
    async_orch: AsyncOrchestrator,
    mongo_uri: str = MONGO_URI,
    db_name: str = "labmate",
):
    """
    Compile the StateGraph with a MongoDBSaver checkpointer.
    Returns (compiled_graph, checkpointer). The caller MUST keep checkpointer
    alive for the graph's lifetime.

    Call once at startup; MongoDBSaver.from_conn_string() creates MongoDB indexes (idempotent).
    """
    from langgraph.checkpoint.mongodb import MongoDBSaver

    plan_node, execute_node, check_node, reflect_node, approval_node, assess_node, verify_node, revise_node = make_nodes(
        orch, async_orch
    )

    b = StateGraph(State)
    b.add_node("plan", plan_node)
    b.add_node("execute", execute_node)
    b.add_node("verify", verify_node)
    b.add_node("check", check_node)
    b.add_node("reflect", reflect_node)
    b.add_node("approval", approval_node)
    b.add_node("assess_ambiguity", assess_node)
    b.add_node("revise", revise_node)

    b.add_edge(START, "assess_ambiguity")
    b.add_conditional_edges("assess_ambiguity", ambiguity_router, ["plan", END])
    b.add_conditional_edges("plan", clarification_router, ["execute", END])
    b.add_edge("execute", "verify")
    b.add_conditional_edges("verify", verify_router, ["reflect", "check"])
    b.add_conditional_edges("check", router, ["execute", "reflect", "approval", "revise", END])
    b.add_edge("revise", END)
    b.add_edge("reflect", "execute")
    b.add_edge("approval", "execute")

    from pymongo import MongoClient

    client = MongoClient(mongo_uri)
    cp = MongoDBSaver(client, db_name=db_name)
    graph = b.compile(checkpointer=cp)
    return graph, cp
