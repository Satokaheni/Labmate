"""Pure state helpers for the lite orchestrator (no I/O)."""

from __future__ import annotations

import copy

from services.orchestrator.types import create_goal


def build_initial_state(
    task: str, session_id: str, user_id: str = "", workspace_id: str = ""
) -> dict:
    """The initial single-goal state — identical shape to CodingOrchestrator.run_task's
    `initial` (coding_orchestrator.py:1771), so lite and graph start from the same state."""
    return {
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
        "verify_retries": 0,
        "direct_answer": False,
    }


def snapshot(state: dict) -> dict:
    """A JSON-serializable deep copy for checkpointing. Transient/unpicklable keys are
    dropped (the lite loop rebuilds them on resume)."""
    snap = copy.deepcopy(dict(state))
    snap.pop("_transient", None)
    return snap


def restore(payload: dict) -> dict:
    """Rebuild loop state from a checkpoint snapshot."""
    return copy.deepcopy(dict(payload))
