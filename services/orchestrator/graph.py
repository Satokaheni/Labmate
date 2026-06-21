# services/orchestrator/graph.py
from __future__ import annotations

import os

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from .types import State, Status, Goal, get_ready_goals, update_status, now_iso, create_goal
from .coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

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
        Decompose the current root goal into child Goals via a Gemma 4 architect call.
        Includes the skill catalog in the prompt when available.
        Writes new Goal entries into goal_tree. Pure return-delta — no in-place mutation.
        """
        import copy

        root_id = state["current_goal_id"]
        root_desc = state["goal_tree"][root_id]["description"]

        # Include skill catalog in the prompt if available
        catalog = ""
        if getattr(orch, "skill_router", None) is not None:
            catalog = orch.skill_router.runner.catalog_prompt()

        prompt = f"Decompose this task into concrete subtasks (one per line):\n{root_desc}"
        if catalog:
            prompt += f"\n\nAvailable skills:\n{catalog}\nIf the whole task is accomplishable by a single available skill, emit ONE subtask describing that work. Otherwise produce a small number of coherent subtasks, each a self-contained unit that may map to a skill."

        raw_plan = await orch.architect(prompt)
        # Deep copy to avoid mutating the checkpoint's prior goal_tree.
        tree = copy.deepcopy(state["goal_tree"])
        for i, line in enumerate(raw_plan.strip().splitlines()):
            if line.strip():
                gid = f"{root_id}_sub{i}"
                create_goal(tree, gid, root_id, line.strip())
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

        children = root.get("children", [])
        if not children:
            return {}

        # Only check finalization when every child has reached a terminal status
        all_terminal = all(
            tree.get(c, {}).get("status") in (Status.COMPLETED.value, Status.FAILED.value)
            for c in children
        )
        if not all_terminal:
            return {}

        # Check for failed children that can still be retried (attempts < 3)
        failed_retryable = [
            c for c in children
            if tree.get(c, {}).get("status") == Status.FAILED.value
            and tree.get(c, {}).get("attempts", 0) < 3
        ]
        if failed_retryable:
            # Don't finalize; defer to reflect by routing to first failed child
            return {"goal_tree": tree, "current_goal_id": failed_retryable[0]}

        # All children are terminal and no retryables remain: FINALIZE
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
        decision = interrupt({"action": "irreversible", "goal": gid})
        # Deep copy to avoid mutating the checkpoint's prior goal_tree.
        tree = copy.deepcopy(state["goal_tree"])
        new_status = Status.IN_PROGRESS if decision == "approve" else Status.BLOCKED
        update_status(tree, gid, new_status)
        return {"goal_tree": tree}

    async def assess_ambiguity(state: State) -> dict:
        import json
        goal = state.get("root_goal") or state["goal_tree"][state["current_goal_id"]]["description"]
        prompt = (
            "You are triaging a task before an autonomous agent executes it.\n"
            f"TASK: {goal}\n\n"
            "List the assumptions an agent must make to act on this as written. "
            "Then rate overall ambiguity from 0.0 (fully specified) to 1.0 (critically "
            "underspecified). Respond as JSON: "
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
        return {
            "root_goal": goal,
            "assumptions": out.get("assumptions", []) or [],
            "ambiguity": ambiguity,
            "blocking_question": out.get("blocking_question", "") or "",
        }

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
    """A1: route after assess_ambiguity."""
    if float(state.get("ambiguity", 0.0)) >= AMBIGUITY_THRESHOLD:
        return "approval"
    return "plan"


def verify_router(state: State) -> str:
    """A2: route after verify. Sub-threshold artifacts loop through reflect."""
    if float(state.get("critique_score", 1.0)) < CRITIQUE_THRESHOLD:
        return "reflect"
    return "check"


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
    b.add_conditional_edges("assess_ambiguity", ambiguity_router, ["approval", "plan"])
    b.add_edge("plan", "execute")
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
