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

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

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


def default_store_path() -> Path:
    """Central sidecar location: env override or services/skills/.skill_telemetry.json."""
    override = os.getenv("LABMATE_TELEMETRY_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "skills" / ".skill_telemetry.json"


def load(path: Path) -> dict:
    """Read the store; return an empty store on missing/empty/corrupt file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {"version": 1, "skills": {}}
    if not text.strip():
        return {"version": 1, "skills": {}}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("corrupt telemetry store at %s, starting empty", path)
        return {"version": 1, "skills": {}}
    if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
        return {"version": 1, "skills": {}}
    data.setdefault("version", 1)
    return data


def save(store: dict, path: Path) -> None:
    """Atomically persist the store (temp file in same dir + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _env_int(name: str, default: int) -> int:
    """Read an env var as int; fall back to default on error."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def record_use_best_effort(
    name: str,
    ok: bool,
    *,
    path: "Path | None" = None,
    now: "datetime | None" = None,
    stale_after_days: "int | None" = None,
    archive_after_days: "int | None" = None,
) -> None:
    """Best-effort: record one skill dispatch and recompute states.

    NEVER raises — telemetry must never break a skill dispatch. Any failure
    (load, record, save) is caught and logged to stderr.
    """
    try:
        store_path = path if path is not None else default_store_path()
        moment = now if now is not None else datetime.now(timezone.utc)
        stale = stale_after_days if stale_after_days is not None else _env_int(
            "SKILL_STALE_AFTER_DAYS", 30
        )
        archive = archive_after_days if archive_after_days is not None else _env_int(
            "SKILL_ARCHIVE_AFTER_DAYS", 90
        )
        store = load(store_path)
        store = record_use(store, name, ok, moment)
        store = apply_transitions(store, moment, stale, archive)
        save(store, store_path)
    except Exception:  # pragma: no cover - defensive; telemetry is best-effort
        log.warning("skill telemetry record_use failed for %s", name, exc_info=True)
