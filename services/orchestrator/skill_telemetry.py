"""Pure per-skill usage telemetry store + atomic persistence.

Foundation for the skill curator. Tracks how often each skill is used,
when it was last used, and an auto-computed lifecycle state
(active -> stale -> archived). The counting and state logic are PURE
(no clock, no filesystem, no globals) so they are deterministically
testable; the only impure surface is load()/save() and the best-effort
wire-in wrapper.

CRITICAL: never write to stdout. All logging goes to stderr.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger("skill_telemetry")  # -> stderr via host handlers

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"


def new_entry(now: datetime, created_by: str = "human") -> dict:
    """A fresh, zeroed telemetry entry for a skill first seen at `now`."""
    return {
        "use_count": 0,
        "success_count": 0,
        "fail_count": 0,
        "last_used_at": None,
        "created_by": created_by,
        "state": STATE_ACTIVE,
        "pinned": False,
        "created_at": now.isoformat(),
    }


def record_use(store: dict, name: str, ok: bool, now: datetime) -> dict:
    """Return a NEW store with `name`'s counters bumped for one dispatch.

    Creates the entry if absent. use_count always +1; success_count +1 on
    ok else fail_count +1; last_used_at = now. Does not mutate `store`.
    """
    skills = dict(store.get("skills", {}))
    entry = dict(skills.get(name) or new_entry(now))
    entry["use_count"] = int(entry.get("use_count", 0)) + 1
    if ok:
        entry["success_count"] = int(entry.get("success_count", 0)) + 1
    else:
        entry["fail_count"] = int(entry.get("fail_count", 0)) + 1
    entry["last_used_at"] = now.isoformat()
    skills[name] = entry
    return {"version": store.get("version", 1), "skills": skills}
