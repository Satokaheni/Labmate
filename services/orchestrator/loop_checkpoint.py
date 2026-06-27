"""Durable per-turn checkpoint for the inner ReAct loop (_run_react_loop).

The OUTER LangGraph layer is already crash-durable via AsyncMongoDBSaver, which
checkpoints between super-steps. This module closes the complementary gap in the
INNER loop: a crash mid-_run_react_loop otherwise loses the whole turn-by-turn
state (messages, iteration budget, edited files, verify-nudge count, loop-detector
signatures, wall-clock start) and the loop restarts from turn 0 on the next
run_task() with the same session.

Two halves:
  * PURE serialize/deserialize (LoopCheckpoint + to_dict/from_dict) — no async,
    no I/O, exhaustively unit-testable. from_dict NEVER raises: any structural
    problem (missing key, unknown version, wrong type) yields None.
  * An async, BEST-EFFORT store (CheckpointStore) over a Mongo collection. Every
    method swallows + logs (stderr) all errors and never raises, so the ReAct
    loop is never broken by a checkpoint failure.

CLAUDE.md: stdout is sacred (log to stderr); no tiktoken; asyncio-correct.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("loop_checkpoint")

CHECKPOINT_VERSION = 1

# Mongo collection holding one in-flight inner-loop checkpoint per task_id.
CHECKPOINT_COLLECTION = "loop_checkpoints"


@dataclass
class LoopCheckpoint:
    """Resumable snapshot of one _run_react_loop, taken at end of each turn.

    All fields are JSON-able. ``start_monotonic_offset`` is ELAPSED seconds
    already spent on this goal at save time — NOT a raw time.monotonic() value
    (monotonic clocks are not comparable across a process restart). On resume the
    loop rebases its deadline clock against this elapsed offset.
    """
    task_id: str
    goal: str
    messages: list[dict] = field(default_factory=list)
    used: int = 0
    absolute_turns: int = 0
    grace_used: bool = False
    edited_files: list[str] = field(default_factory=list)
    tests_passed: bool = False
    verify_nudges_used: int = 0
    loop_signatures: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    start_monotonic_offset: float = 0.0
    turn: int = 0


# Required keys whose presence + type from_dict validates. (version handled
# separately; defaulted/optional scalars are coerced, not required.)
_REQUIRED = {
    "task_id": str,
    "goal": str,
    "messages": list,
}


def to_dict(cp: LoopCheckpoint) -> dict:
    """Serialize a LoopCheckpoint to a JSON-able dict (PURE)."""
    return {
        "version": CHECKPOINT_VERSION,
        "task_id": cp.task_id,
        "goal": cp.goal,
        "messages": cp.messages,
        "used": cp.used,
        "absolute_turns": cp.absolute_turns,
        "grace_used": cp.grace_used,
        "edited_files": list(cp.edited_files),
        "tests_passed": cp.tests_passed,
        "verify_nudges_used": cp.verify_nudges_used,
        "loop_signatures": list(cp.loop_signatures),
        "tools_used": list(cp.tools_used),
        "start_monotonic_offset": cp.start_monotonic_offset,
        "turn": cp.turn,
    }


def from_dict(d: dict | None) -> LoopCheckpoint | None:
    """Deserialize a dict into a LoopCheckpoint (PURE). Never raises.

    Returns None on: None input, unknown version, a missing/mistyped REQUIRED
    field, or any unexpected structural error. Extra keys (e.g. Mongo's _id) are
    ignored. Optional scalars are coerced defensively.
    """
    if not isinstance(d, dict):
        return None
    try:
        if int(d.get("version", -1)) != CHECKPOINT_VERSION:
            return None
        for key, typ in _REQUIRED.items():
            if key not in d or not isinstance(d[key], typ):
                return None
        return LoopCheckpoint(
            task_id=d["task_id"],
            goal=d["goal"],
            messages=list(d["messages"]),
            used=int(d.get("used", 0)),
            absolute_turns=int(d.get("absolute_turns", 0)),
            grace_used=bool(d.get("grace_used", False)),
            edited_files=list(d.get("edited_files", []) or []),
            tests_passed=bool(d.get("tests_passed", False)),
            verify_nudges_used=int(d.get("verify_nudges_used", 0)),
            loop_signatures=list(d.get("loop_signatures", []) or []),
            tools_used=list(d.get("tools_used", []) or []),
            start_monotonic_offset=float(d.get("start_monotonic_offset", 0.0)),
            turn=int(d.get("turn", 0)),
        )
    except (TypeError, ValueError, KeyError) as exc:
        _log.warning("from_dict rejected a malformed checkpoint: %s", exc)
        return None


class CheckpointStore:
    """Best-effort persistence of one inner-loop checkpoint per task_id.

    Backed by a Motor (async) collection. EVERY method swallows and logs (to
    stderr via logging) all errors and never raises — a checkpoint failure must
    never break the ReAct loop. Keyed by task_id; save() upserts so only the
    latest snapshot per task is kept.
    """

    def __init__(self, collection: Any) -> None:
        self._col = collection

    async def save(self, cp: LoopCheckpoint) -> None:
        try:
            await self._col.update_one(
                {"task_id": cp.task_id},
                {"$set": to_dict(cp)},
                upsert=True,
            )
        except Exception as exc:  # best-effort: never break the loop
            _log.warning("checkpoint save failed for %s: %s", cp.task_id, exc)

    async def load(self, task_id: str) -> LoopCheckpoint | None:
        try:
            doc = await self._col.find_one({"task_id": task_id})
        except Exception as exc:
            _log.warning("checkpoint load failed for %s: %s", task_id, exc)
            return None
        return from_dict(doc)

    async def clear(self, task_id: str) -> None:
        try:
            await self._col.delete_one({"task_id": task_id})
        except Exception as exc:
            _log.warning("checkpoint clear failed for %s: %s", task_id, exc)


class FakeCheckpointStore:
    """In-memory CheckpointStore for tests and as a default DI seam."""

    def __init__(self) -> None:
        self._mem: dict[str, dict] = {}

    async def save(self, cp: LoopCheckpoint) -> None:
        self._mem[cp.task_id] = to_dict(cp)

    async def load(self, task_id: str) -> LoopCheckpoint | None:
        return from_dict(self._mem.get(task_id))

    async def clear(self, task_id: str) -> None:
        self._mem.pop(task_id, None)


__all__ = [
    "CHECKPOINT_VERSION",
    "CHECKPOINT_COLLECTION",
    "LoopCheckpoint",
    "to_dict",
    "from_dict",
    "CheckpointStore",
    "FakeCheckpointStore",
]
