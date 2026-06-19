from __future__ import annotations

import datetime
from enum import Enum
from operator import add
from typing import Annotated, TypedDict, Optional


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
    parent_id: str | None
    children: list[str]
    description: str
    status: str          # Status enum value; stored as str for JSON safety
    result: str | None
    error: str | None
    attempts: int
    started_at: str | None    # ISO-8601
    updated_at: str | None    # ISO-8601


class State(TypedDict, total=False):
    """
    The single JSON-serializable state object persisted by AsyncMongoDBSaver
    at every LangGraph super-step.

    RULE: Never store Python objects, coroutines, DB clients, or file handles
    here. Everything must survive json.dumps() / json.loads() round-trips.
    """
    session_id: str
    goal_tree: dict[str, Goal]        # id -> Goal; the live plan
    current_goal_id: str | None
    step_markers: dict[str, str]      # step_id -> 'started' | 'completed'
    messages: Annotated[list, add]    # reducer-safe; parallel nodes may append
    error: str | None
    final_answer: str                 # Clean summary for Discord/user display
    workspace_id: str                 # which workspace this session belongs to
    user_id: str                      # stable user identifier


def now_iso() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')


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
        status=Status.PENDING.value,
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
    g["status"] = status.value
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
        if g["status"] == Status.PENDING.value
        and all(tree[c]["status"] == Status.COMPLETED.value for c in g["children"])
    ]
