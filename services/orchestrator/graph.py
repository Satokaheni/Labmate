# services/orchestrator/graph.py
from __future__ import annotations

import asyncio
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
QWEN_BASE  = os.getenv("QWEN_BASE",  "http://localhost:8001/v1")


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
                if markers.get(gid) == "completed":
                    continue  # idempotency guard: skip already-completed goals on crash-resume
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
        Human-in-the-loop gate before irreversible actions.
        interrupt() checkpoints state and suspends execution until the thread is resumed.
        """
        gid = state["current_goal_id"]
        decision = interrupt({"action": "irreversible", "goal": gid})
        tree = dict(state["goal_tree"])
        new_status = Status.IN_PROGRESS if decision == "approve" else Status.BLOCKED
        update_status(tree, gid, new_status)
        return {"goal_tree": tree}

    return plan, execute_node, check, reflect, approval


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

    if goal["status"] == Status.FAILED.value and goal["attempts"] < 3:
        return "reflect"
    if goal["status"] == Status.AWAITING_APPROVAL.value:
        return "approval"
    if get_ready_goals(state["goal_tree"]):
        return "execute"
    return END
