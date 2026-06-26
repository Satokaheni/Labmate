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


def _idle_days(entry: dict, now: datetime) -> float:
    """Days since the entry was last used, or since created_at if never used."""
    ref = entry.get("last_used_at") or entry.get("created_at")
    if not ref:
        return 0.0
    try:
        ref_dt = datetime.fromisoformat(ref)
    except (TypeError, ValueError):
        return 0.0
    return (now - ref_dt).total_seconds() / 86400.0


def compute_state(
    entry: dict,
    now: datetime,
    stale_after_days: int = 30,
    archive_after_days: int = 90,
) -> str:
    """PURE: the state this entry SHOULD have given its idle time.

    Pinned entries bypass all transitions and stay active. Thresholds are
    inclusive: idle >= archive_after_days -> archived; idle >= stale_after_days
    -> stale; otherwise active.
    """
    if entry.get("pinned"):
        return STATE_ACTIVE
    idle = _idle_days(entry, now)
    if idle >= archive_after_days:
        return STATE_ARCHIVED
    if idle >= stale_after_days:
        return STATE_STALE
    return STATE_ACTIVE


def apply_transitions(
    store: dict,
    now: datetime,
    stale_after_days: int = 30,
    archive_after_days: int = 90,
) -> dict:
    """Return a NEW store with each entry's `state` recomputed."""
    skills = {}
    for name, entry in store.get("skills", {}).items():
        new = dict(entry)
        new["state"] = compute_state(new, now, stale_after_days, archive_after_days)
        skills[name] = new
    return {"version": store.get("version", 1), "skills": skills}
