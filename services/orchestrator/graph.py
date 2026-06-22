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


def make_nodes(orch: CodingOrchestrator, async_orch: AsyncOrchestrator):
    """
    Factory that closes over the orchestrator instances so nodes are plain
    async functions (no class state on the graph itself).
    """

    async def plan(state: State) -> dict:
        """
        Decompose the current root goal into child Goals via multi-intent routing.
        If route() needs clarification, emit clarification_request and pause.
        Otherwise, expand matched skills into a sequential chain of child Goals.
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

        # Call route() to handle multi-intent decomposition and routing
        # (only if skill_router has the route method; fallback for old tests/code)
        route_result = None
        try:
            if skill_router is not None and hasattr(skill_router, "route"):
                route_result = await skill_router.route(goal_desc)
        except Exception as e:
            # Fallback on any route() error (LLM unavailable, network error, TypeError, etc.)
            # This preserves backward compatibility when route() would require live services.
            _log.debug("route() failed, falling back to architect: %s", e)
            route_result = None

        if route_result is not None:
            # If route() successfully routed skills, use them.
            if route_result.skills:
                # Confident multi-intent route: one sequential child Goal per skill.
                # Build a real dependency CHAIN so get_ready_goals() (which releases a
                # PENDING goal only once ALL its children are COMPLETED — i.e. the
                # DEEPEST LEAF first) executes the sub-intents in SUBMISSION ORDER.
                #
                # FIX 3: nest the sub-intents in REVERSE so the FIRST sub-intent becomes
                # the deepest leaf (runs first) and the LAST sub-intent is root's direct
                # child (runs last):
                #     root -> sub3 -> sub2 -> sub1(leaf, runs first)
                # Iterating reversed() means each sub-intent is nested as the child of the
                # NEXT sub-intent's goal. create_goal keeps parent_id and the parent's
                # children[] mutually consistent. The previous (buggy) forward chaining made
                # the LAST sub-intent the leaf, so the chain executed backwards.
                tree = copy.deepcopy(state["goal_tree"])
                prev_id = root_id
                for sub_intent in reversed(route_result.sub_intents):
                    child_id = uuid.uuid4().hex[:12]
                    create_goal(tree, child_id, prev_id, sub_intent)
                    prev_id = child_id

                return {"goal_tree": tree, "awaiting_clarification": False}

            # If route() needs clarification, ALWAYS pause and ask — never guess.
            # This applies even to a single ambiguous intent (sub_intents == [goal_desc]).
            if route_result.needs_clarification:
                # Clarification request: emit and pause.
                await events.emit(
                    "clarification_request",
                    question=route_result.clarification_question,
                    task=goal_desc,
                    session_id=state.get("session_id", ""),
                )
                return {
                    "awaiting_clarification": True,
                    "clarification_question": route_result.clarification_question,
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
        for r in results:
            gid = r.id
            # Idempotency guard (FIX #4): mark per-GOAL-ID only when COMPLETED.
            # FAILED goals are NOT marked, so they stay retryable and increment attempts each time.
            # This prevents the deadlock where a retry with the same summary was skipped.
            if markers.get(gid) == "completed":
                continue  # idempotency guard: already applied for this goal
            new_status = Status.COMPLETED if r.ok else Status.FAILED
            # Increment attempts on FAILED (needed for router's reflect branch to trigger)
            if not r.ok:
                tree[gid]["attempts"] = tree[gid].get("attempts", 0) + 1
            # Set both result and error on failure so reflect/check can surface the error
            if r.ok:
                update_status(tree, gid, new_status, result=r.summary)
            else:
                update_status(tree, gid, new_status, result=r.summary, error=r.summary)
            # Mark as completed only if succeeded; FAILED goals stay retryable
            if r.ok:
                markers[gid] = "completed"
            # Track last artifact for A2 verify gate
            if r.ok and r.summary:
                last_artifact = {
                    "type": classify_artifact(r.summary),
                    "payload": r.summary,
                }

        return {"goal_tree": tree, "step_markers": markers, "last_artifact": last_artifact}

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
            and tree.get(c, {}).get("attempts", 0) < 3
        ]
        if failed_retryable:
            # Don't finalize; defer to reflect by routing to first failed child.
            # (children is ordered deepest-first / submission order, so this retries
            # the earliest-submitted failed sub-intent first.)
            await events.emit(
                "reasoning",
                node="check",
                summary="deferring to reflect for failed child retry",
                text=f"Child {failed_retryable[0]} failed but retryable (attempts < 3)",
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
        Conditions the next execute attempt on the stored reflection.
        """
        import copy
        gid = state["current_goal_id"]
        goal = state["goal_tree"][gid]
        reflection = await orch.architect(
            f"The following subtask failed (attempt {goal['attempts']}):\n"
            f"Goal: {goal['description']}\n"
            f"Error: {goal['error']}\n"
            "Write a concise diagnosis and what to do differently on the next attempt.",
            thinking_budget=3000,
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
            "messages": [{"role": "reflection", "content": reflection}],
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
            "RUBRIC — score HIGH (0.7-0.9) when the task has any of:\n"
            "  - an undefined referent (e.g. \"make it better\", \"fix the thing\" — no "
            "object is named),\n"
            "  - no concrete deliverable (you cannot tell what artifact to produce),\n"
            "  - undefined scope (you'd have to guess what 'done' means).\n"
            "Score LOW (0.0-0.2) for a clear, actionable request where the deliverable "
            "and scope are unambiguous (e.g. \"write a python function that reverses a "
            "string\").\n"
            "Examples:\n"
            "  \"make it better\" -> 0.85\n"
            "  \"fix the thing\" -> 0.9\n"
            "  \"write a python function that reverses a string\" -> 0.1\n"
            "  \"add a docstring to the reverse_string function in utils.py\" -> 0.1\n\n"
            "When ambiguity is high, set \"blocking_question\" to the single most useful "
            "question to ask the user; otherwise leave it empty.\n"
            "Respond as JSON: "
            '{"assumptions": ["..."], "ambiguity": 0.0, "blocking_question": "" }'
        )
        raw = await orch.architect(prompt, thinking_budget=1024)
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
        await events.emit(
            "reasoning",
            node="assess_ambiguity",
            summary=f"ambiguity={ambiguity:.2f}; {len(assumptions)} assumption(s)",
            text=blocking_question or json.dumps(out),
        )

        result = {
            "root_goal": goal,
            "assumptions": assumptions,
            "ambiguity": ambiguity,
            "blocking_question": blocking_question,
        }

        # On high ambiguity, HALT and ask the user a clarifying question rather than
        # guessing. ambiguity_router sends this node's output to END; main._handle /
        # coding_orchestrator.stream surface clarification_question as the answer and
        # suppress any guess. Reuse the same clarification_request event the plan node
        # emits so downstream consumers see one consistent shape.
        if ambiguity >= AMBIGUITY_THRESHOLD:
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
        if atype not in ("code", "writing") or router_obj is None:
            await events.emit(
                "reasoning",
                node="verify",
                summary="skipped (no code/writing artifact)",
                text="",
            )
            return {"verified": True, "critique_score": 1.0, "critique_notes": ""}

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
            )
            if isinstance(res, dict) and res.get("ok"):
                payload = res.get("result")
                if isinstance(payload, dict):
                    score = float(payload.get("score", 1.0))
                    notes = str(payload.get("notes", ""))
        except Exception:
            score = 1.0
            notes = ""

        await events.emit(
            "reasoning",
            node="verify",
            summary=f"critique_score={score:.2f}",
            text=notes,
        )
        return {"verified": True, "critique_score": score, "critique_notes": notes}

    return plan, execute_node, check, reflect, approval, assess_ambiguity, verify


def router(state: State) -> str:
    """
    Route after the 'check' node. Reads only values committed in prior
    super-steps — never intra-super-step values.
    Finalization (final_answer set) is the terminal signal.
    (FIX #2: make finalization terminal so failed FAILED root doesn't loop)
    """
    # If final_answer is set, check node finalized the tree — end execution
    if state.get("final_answer"):
        return END

    gid = state.get("current_goal_id")
    if gid is None:
        return END

    goal = state["goal_tree"].get(gid)
    if goal is None:
        return END

    if goal["status"] == Status.FAILED.value and goal["attempts"] < 3:
        return "reflect"
    if goal["status"] == Status.AWAITING_APPROVAL.value:
        return "approval"
    if get_ready_goals(state["goal_tree"]):
        return "execute"
    return END


def ambiguity_router(state: State) -> str:
    """A1: route after assess_ambiguity. On high ambiguity, HALT (END) so the agent
    asks the user a clarifying question instead of guessing; otherwise plan."""
    if float(state.get("ambiguity", 0.0)) >= AMBIGUITY_THRESHOLD:
        return END
    return "plan"


def verify_router(state: State) -> str:
    """A2: route after verify. Sub-threshold artifacts loop through reflect."""
    if float(state.get("critique_score", 1.0)) < CRITIQUE_THRESHOLD:
        return "reflect"
    return "check"


def clarification_router(state: State) -> str:
    """Route after the 'plan' node. Halt the graph (END) when the plan node has
    requested clarification from the user, so the agent does NOT proceed to
    execute and guess at an ambiguous task. Otherwise continue to execute."""
    if state.get("awaiting_clarification"):
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

    plan_node, execute_node, check_node, reflect_node, approval_node, assess_node, verify_node = make_nodes(
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

    b.add_edge(START, "assess_ambiguity")
    b.add_conditional_edges("assess_ambiguity", ambiguity_router, ["plan", END])
    b.add_conditional_edges("plan", clarification_router, ["execute", END])
    b.add_edge("execute", "verify")
    b.add_conditional_edges("verify", verify_router, ["reflect", "check"])
    b.add_conditional_edges("check", router, ["execute", "reflect", "approval", END])
    b.add_edge("reflect", "execute")
    b.add_edge("approval", "execute")

    from pymongo import MongoClient

    client = MongoClient(mongo_uri)
    cp = MongoDBSaver(client, db_name=db_name)
    graph = b.compile(checkpointer=cp)
    return graph, cp
