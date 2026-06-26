# Skill Usage Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pure, atomically-persisted per-skill usage telemetry store (`use_count`/`success_count`/`fail_count`/`last_used_at`/`state`/`pinned`/`created_by`/`created_at`) plus a pure `compute_state` auto-transition function, and wire a best-effort `record_use(name, ok)` at the skill-dispatch completion seam in `SkillRouter.run()` — without changing any dispatch behavior.

**Architecture:** A new pure module `services/orchestrator/skill_telemetry.py` owns a single **central** JSON sidecar (one file, one lock — simpler and atomic-by-design vs. N per-skill files, which would each need their own lock and re-introduce a torn-multi-file-write hazard the central file avoids). Persistence is atomic (temp file in the same directory + `os.replace`). All counting and state logic is pure functions over a plain `dict` store, exhaustively unit-tested with no I/O. The only impure surface is `load`/`save` (filesystem) and a thin best-effort wrapper wired into `SkillRouter.run()` that catches every exception and logs to stderr so telemetry can never break a skill dispatch.

**Tech Stack:** Python 3, stdlib `json`/`os`/`tempfile`/`datetime`, `logging` (stderr only), `pytest` + `pytest-asyncio` (`asyncio_mode = auto`), `pytest-bdd`. No new third-party dependencies. No `tiktoken`. No stdout writes.

## Global Constraints

- **stdout is sacred** — this module is imported by the orchestrator process; never `print()` / write to stdout. Use `logging.getLogger("skill_telemetry")` → stderr only.
- **No tiktoken** anywhere.
- **Atomic writes only** — persist via a temp file in the *same directory* + `os.replace(tmp, path)` (atomic on POSIX). Never write the live file in place.
- **Telemetry is best-effort and additive** — a telemetry failure (load, save, compute, or wire-in) MUST NOT change or break skill-dispatch behavior. Every wired call is wrapped in `try/except Exception` that logs and swallows.
- **Pure core** — `record_use`, `compute_state`, `new_entry`, `apply_transitions` take their inputs (including `now`) explicitly and return values with no global/clock/filesystem access. `now` is always injected for deterministic tests.
- **Env knobs** (read at the wiring layer, defaulted in the pure layer): `SKILL_STALE_AFTER_DAYS` (default `30`), `SKILL_ARCHIVE_AFTER_DAYS` (default `90`).
- **File naming:** Python files `snake_case.py`; classes `PascalCase`; functions `snake_case`. Tests under `tests/` mirroring `services/`.
- **Timestamps** are timezone-aware UTC ISO-8601 strings (`datetime.now(timezone.utc).isoformat()`); `now` injected into pure functions is a `datetime`.
- **BDD layer:** feature → `tests/services/orchestrator/features/skill_usage_telemetry.feature` tagged `@mocked`; step defs → `tests/services/orchestrator/test_skill_usage_telemetry_bdd.py` (`pytestmark = [pytest.mark.bdd, pytest.mark.mocked]`, `scenarios("features/skill_usage_telemetry.feature")`). Async steps use `from tests.conftest import run_async`. `fake_model` fixture already exists in `tests/conftest.py` (not needed here — telemetry has no model call — but the BDD harness conventions are followed).

---

## File Map

| File | Create/Modify | Responsibility |
|---|---|---|
| `services/orchestrator/skill_telemetry.py` | **Create** | Pure store: `new_entry`, `record_use`, `compute_state`, `apply_transitions`, `STATE_ACTIVE/STALE/ARCHIVED`; impure I/O: `default_store_path`, `load`, `save`; best-effort wrapper `record_use_best_effort`. |
| `services/orchestrator/skill_router.py` | **Modify** | In `SkillRouter.run()`, after `execute()` returns its `ok/fail` dict, call `record_use_best_effort(...)` (wrapped, swallowing). Add a `telemetry_path` constructor parameter (defaults to `None` → resolved lazily). No change to return values or control flow. |
| `tests/services/orchestrator/test_skill_telemetry.py` | **Create** | Exhaustive unit tests for the pure store + atomic save + concurrent-writer test. |
| `tests/services/orchestrator/test_skill_router.py` | **Modify** | Add tests proving `run()` records use best-effort and that a telemetry failure does not break dispatch. |
| `tests/services/orchestrator/features/skill_usage_telemetry.feature` | **Create** | Gherkin contract (5 scenarios). |
| `tests/services/orchestrator/test_skill_usage_telemetry_bdd.py` | **Create** | pytest-bdd step defs binding the feature. |

**Store location decision (central JSON):** the sidecar lives at `default_store_path()` =
`Path(os.getenv("LABMATE_TELEMETRY_PATH"))` if set, else
`Path(__file__).resolve().parent.parent / "skills" / ".skill_telemetry.json"` (i.e. `services/skills/.skill_telemetry.json`, alongside the skills `SkillRunner` already roots there in `main.py:154`). One central file = one `os.replace` = inherently atomic; no cross-file partial-write window.

**Store shape (the JSON document):**
```json
{ "version": 1, "skills": { "<skill-name>": { ...entry... } } }
```
Each entry:
```json
{
  "use_count": 0, "success_count": 0, "fail_count": 0,
  "last_used_at": null, "created_by": "human",
  "state": "active", "pinned": false,
  "created_at": "2026-06-26T00:00:00+00:00"
}
```

---

## Task 1: Pure entry + record_use

**Files:**
- Create: `services/orchestrator/skill_telemetry.py`
- Test: `tests/services/orchestrator/test_skill_telemetry.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `STATE_ACTIVE = "active"`, `STATE_STALE = "stale"`, `STATE_ARCHIVED = "archived"` (str constants)
  - `new_entry(now: datetime, created_by: str = "human") -> dict` — fresh entry with all fields zeroed/defaulted, `created_at`/`state`/`pinned` set.
  - `record_use(store: dict, name: str, ok: bool, now: datetime) -> dict` — returns a **new** store dict (does not mutate input) with `name`'s entry created-if-absent then bumped: `use_count += 1`, `success_count += 1` if `ok` else `fail_count += 1`, `last_used_at = now.isoformat()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_skill_telemetry.py
"""Unit tests for the pure skill-telemetry store (no I/O unless noted)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.orchestrator import skill_telemetry as st

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_new_entry_defaults():
    e = st.new_entry(T0)
    assert e["use_count"] == 0
    assert e["success_count"] == 0
    assert e["fail_count"] == 0
    assert e["last_used_at"] is None
    assert e["created_by"] == "human"
    assert e["state"] == st.STATE_ACTIVE
    assert e["pinned"] is False
    assert e["created_at"] == T0.isoformat()


def test_record_use_success_bumps_use_and_success():
    store = {"version": 1, "skills": {}}
    out = st.record_use(store, "web-search", ok=True, now=T0)
    entry = out["skills"]["web-search"]
    assert entry["use_count"] == 1
    assert entry["success_count"] == 1
    assert entry["fail_count"] == 0
    assert entry["last_used_at"] == T0.isoformat()


def test_record_use_failure_bumps_fail_only():
    store = {"version": 1, "skills": {}}
    out = st.record_use(store, "web-search", ok=False, now=T0)
    entry = out["skills"]["web-search"]
    assert entry["use_count"] == 1
    assert entry["success_count"] == 0
    assert entry["fail_count"] == 1
    assert entry["last_used_at"] == T0.isoformat()


def test_record_use_accumulates_across_calls():
    store = {"version": 1, "skills": {}}
    store = st.record_use(store, "web-search", ok=True, now=T0)
    later = T0 + timedelta(days=1)
    store = st.record_use(store, "web-search", ok=False, now=later)
    entry = store["skills"]["web-search"]
    assert entry["use_count"] == 2
    assert entry["success_count"] == 1
    assert entry["fail_count"] == 1
    assert entry["last_used_at"] == later.isoformat()


def test_record_use_does_not_mutate_input_store():
    store = {"version": 1, "skills": {}}
    st.record_use(store, "web-search", ok=True, now=T0)
    assert store["skills"] == {}  # original untouched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_telemetry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.skill_telemetry'`.

- [ ] **Step 3: Write minimal implementation**

```python
# services/orchestrator/skill_telemetry.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_telemetry.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_telemetry.py tests/services/orchestrator/test_skill_telemetry.py
git commit -m "feat(telemetry): pure skill-telemetry entry + record_use"
```

---

## Task 2: Pure compute_state + apply_transitions

**Files:**
- Modify: `services/orchestrator/skill_telemetry.py`
- Test: `tests/services/orchestrator/test_skill_telemetry.py`

**Interfaces:**
- Consumes: `STATE_ACTIVE/STALE/ARCHIVED`, `new_entry`, `record_use` (Task 1).
- Produces:
  - `compute_state(entry: dict, now: datetime, stale_after_days: int = 30, archive_after_days: int = 90) -> str` — PURE. Returns the state the entry *should* have given idle time since `last_used_at`. `pinned` entries always return `STATE_ACTIVE`. An entry never used (`last_used_at is None`) measures idle from `created_at`. Thresholds are inclusive (`idle_days >= archive_after_days` → archived; else `>= stale_after_days` → stale; else active).
  - `apply_transitions(store: dict, now: datetime, stale_after_days: int = 30, archive_after_days: int = 90) -> dict` — returns a new store with every entry's `state` set to its `compute_state` result.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_skill_telemetry.py

def _entry_last_used(days_ago: int, *, pinned: bool = False) -> dict:
    e = st.new_entry(T0 - timedelta(days=days_ago))
    e["last_used_at"] = (T0 - timedelta(days=days_ago)).isoformat()
    e["pinned"] = pinned
    return e


def test_compute_state_recent_is_active():
    e = _entry_last_used(5)
    assert st.compute_state(e, now=T0) == st.STATE_ACTIVE


def test_compute_state_stale_at_threshold():
    e = _entry_last_used(30)  # exactly stale_after_days
    assert st.compute_state(e, now=T0) == st.STATE_STALE


def test_compute_state_just_below_stale_is_active():
    e = _entry_last_used(29)
    assert st.compute_state(e, now=T0) == st.STATE_ACTIVE


def test_compute_state_archived_at_threshold():
    e = _entry_last_used(90)  # exactly archive_after_days
    assert st.compute_state(e, now=T0) == st.STATE_ARCHIVED


def test_compute_state_between_thresholds_is_stale():
    e = _entry_last_used(60)
    assert st.compute_state(e, now=T0) == st.STATE_STALE


def test_pinned_skill_never_transitions():
    e = _entry_last_used(365, pinned=True)
    assert st.compute_state(e, now=T0) == st.STATE_ACTIVE


def test_never_used_entry_measures_idle_from_created_at():
    e = st.new_entry(T0 - timedelta(days=100))  # last_used_at is None
    assert st.compute_state(e, now=T0) == st.STATE_ARCHIVED


def test_custom_thresholds_are_honored():
    e = _entry_last_used(10)
    assert st.compute_state(e, now=T0, stale_after_days=7, archive_after_days=20) == st.STATE_STALE


def test_apply_transitions_updates_all_entries():
    store = {"version": 1, "skills": {
        "fresh": _entry_last_used(1),
        "old": _entry_last_used(45),
        "ancient": _entry_last_used(120),
    }}
    out = st.apply_transitions(store, now=T0)
    assert out["skills"]["fresh"]["state"] == st.STATE_ACTIVE
    assert out["skills"]["old"]["state"] == st.STATE_STALE
    assert out["skills"]["ancient"]["state"] == st.STATE_ARCHIVED
    # input store untouched
    assert store["skills"]["old"]["state"] == st.STATE_ACTIVE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_telemetry.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'compute_state'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to services/orchestrator/skill_telemetry.py

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_telemetry.py -q`
Expected: PASS (14 passed total).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_telemetry.py tests/services/orchestrator/test_skill_telemetry.py
git commit -m "feat(telemetry): pure compute_state + apply_transitions (pinned bypass)"
```

---

## Task 3: Atomic load/save + default path

**Files:**
- Modify: `services/orchestrator/skill_telemetry.py`
- Test: `tests/services/orchestrator/test_skill_telemetry.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `default_store_path() -> Path` — `LABMATE_TELEMETRY_PATH` env override, else `services/skills/.skill_telemetry.json`.
  - `load(path: Path) -> dict` — returns parsed store, or an empty `{"version": 1, "skills": {}}` when the file is missing/empty/corrupt (never raises).
  - `save(store: dict, path: Path) -> None` — atomic write (temp file in same dir + `os.replace`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_skill_telemetry.py
import json
import os
import threading
from pathlib import Path


def test_default_store_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "tele.json"
    monkeypatch.setenv("LABMATE_TELEMETRY_PATH", str(target))
    assert st.default_store_path() == target


def test_default_store_path_falls_back_to_skills_dir(monkeypatch):
    monkeypatch.delenv("LABMATE_TELEMETRY_PATH", raising=False)
    p = st.default_store_path()
    assert p.name == ".skill_telemetry.json"
    assert p.parent.name == "skills"


def test_load_missing_file_returns_empty_store(tmp_path):
    store = st.load(tmp_path / "nope.json")
    assert store == {"version": 1, "skills": {}}


def test_load_corrupt_file_returns_empty_store(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert st.load(p) == {"version": 1, "skills": {}}


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "sub" / "tele.json"  # parent dir does not exist yet
    store = st.record_use({"version": 1, "skills": {}}, "web-search", ok=True, now=T0)
    st.save(store, p)
    assert p.exists()
    assert st.load(p)["skills"]["web-search"]["use_count"] == 1


def test_save_is_atomic_no_tmp_left_behind(tmp_path):
    p = tmp_path / "tele.json"
    st.save({"version": 1, "skills": {}}, p)
    leftovers = [f for f in os.listdir(tmp_path) if f != "tele.json"]
    assert leftovers == []  # temp file was renamed, not orphaned


def test_concurrent_writers_leave_valid_json(tmp_path):
    """Many threads saving concurrently must never leave a torn file.

    os.replace is atomic, so every load() in the race sees a fully-written
    document — never a half-written one.
    """
    p = tmp_path / "tele.json"
    st.save({"version": 1, "skills": {}}, p)
    errors: list[Exception] = []

    def worker(i: int):
        try:
            store = st.load(p)
            store = st.record_use(store, f"skill-{i}", ok=True, now=T0)
            st.save(store, p)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # File is always parseable (atomicity guarantee); a last-writer-wins
    # race may drop some skills, but the document is never corrupt.
    final = st.load(p)
    assert isinstance(final["skills"], dict)
    json.loads(p.read_text(encoding="utf-8"))  # parses cleanly
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_telemetry.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'default_store_path'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add imports at the top of services/orchestrator/skill_telemetry.py
import json
import os
import tempfile
from pathlib import Path

# append functions

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_telemetry.py -q`
Expected: PASS (21 passed total).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_telemetry.py tests/services/orchestrator/test_skill_telemetry.py
git commit -m "feat(telemetry): atomic load/save + central default path"
```

---

## Task 4: Best-effort record_use wrapper

**Files:**
- Modify: `services/orchestrator/skill_telemetry.py`
- Test: `tests/services/orchestrator/test_skill_telemetry.py`

**Interfaces:**
- Consumes: `load`, `save`, `record_use`, `apply_transitions`, `default_store_path` (Tasks 1–3).
- Produces:
  - `record_use_best_effort(name: str, ok: bool, *, path: Path | None = None, now: datetime | None = None, stale_after_days: int | None = None, archive_after_days: int | None = None) -> None` — load → `record_use` → `apply_transitions` → `save`, reading `SKILL_STALE_AFTER_DAYS`/`SKILL_ARCHIVE_AFTER_DAYS` env knobs when thresholds not passed. **Never raises** — any exception is caught and logged to stderr.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_skill_telemetry.py
from unittest.mock import patch


def test_record_use_best_effort_persists(tmp_path):
    p = tmp_path / "tele.json"
    st.record_use_best_effort("web-search", ok=True, path=p, now=T0)
    loaded = st.load(p)
    assert loaded["skills"]["web-search"]["use_count"] == 1
    assert loaded["skills"]["web-search"]["success_count"] == 1


def test_record_use_best_effort_applies_state(tmp_path):
    p = tmp_path / "tele.json"
    old = T0 - timedelta(days=200)
    # First use long ago...
    st.record_use_best_effort("web-search", ok=True, path=p, now=old)
    # ...inspected "now" (T0) with no further use -> should be archived.
    store = st.load(p)
    archived = st.apply_transitions(store, now=T0)
    assert archived["skills"]["web-search"]["state"] == st.STATE_ARCHIVED


def test_record_use_best_effort_reads_env_thresholds(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_STALE_AFTER_DAYS", "5")
    monkeypatch.setenv("SKILL_ARCHIVE_AFTER_DAYS", "10")
    p = tmp_path / "tele.json"
    st.record_use_best_effort("web-search", ok=True, path=p, now=(T0 - timedelta(days=6)))
    # re-record now to trigger apply_transitions with env thresholds, but
    # keep last_used 6 days old by recording a *new* skill instead.
    st.record_use_best_effort("other", ok=True, path=p, now=T0)
    store = st.load(p)
    # web-search last used 6 days ago, stale threshold = 5 -> stale.
    assert store["skills"]["web-search"]["state"] == st.STATE_STALE


def test_record_use_best_effort_swallows_save_failure():
    # save() blows up; the call must NOT raise.
    with patch.object(st, "save", side_effect=OSError("disk full")):
        st.record_use_best_effort("web-search", ok=True, path=Path("/tmp/x.json"), now=T0)
    # no assertion needed: reaching here without an exception is the contract.


def test_record_use_best_effort_swallows_load_failure():
    with patch.object(st, "load", side_effect=RuntimeError("boom")):
        st.record_use_best_effort("web-search", ok=True, path=Path("/tmp/x.json"), now=T0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_telemetry.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'record_use_best_effort'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add import at the top of services/orchestrator/skill_telemetry.py
from datetime import datetime, timezone

# append function

def _env_int(name: str, default: int) -> int:
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
```

(Note: replace the `from datetime import datetime` line added in Task 4's import block by merging it with the existing `from datetime import datetime` from Task 1 — final import is `from datetime import datetime, timezone`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_telemetry.py -q`
Expected: PASS (26 passed total).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_telemetry.py tests/services/orchestrator/test_skill_telemetry.py
git commit -m "feat(telemetry): best-effort record_use wrapper (never raises)"
```

---

## Task 5: Wire record_use into SkillRouter.run()

**Files:**
- Modify: `services/orchestrator/skill_router.py`
- Test: `tests/services/orchestrator/test_skill_router.py`

**Interfaces:**
- Consumes: `record_use_best_effort` (Task 4); `RESULT_PREFIX` etc. unchanged.
- Produces: no new public signature on the *happy path* — `run()` still returns the same dict/`None`. Constructor gains an optional `telemetry_path: Path | None = None` (defaults to `None` → `default_store_path()` resolved inside `record_use_best_effort`). The wire-in records after `execute()` returns, using `bool(result.get("ok"))`.

**Seam:** in `SkillRouter.run()` (`services/orchestrator/skill_router.py`), `result = await self.execute(...)` then `ok = bool(result.get("ok"))` (already computed for the `tool.done` event). Insert the best-effort record right after `ok` is computed, before the `tool.done` emit — wrapped so a telemetry failure cannot affect the return value.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_skill_router.py

@pytest.mark.mocked
class TestSkillRouterTelemetry:
    """run() records skill use best-effort without changing dispatch behavior."""

    @pytest.fixture
    def router(self, tmp_path):
        runner = MagicMock(spec=SkillRunner)
        runner.catalog = {"web-search": MagicMock()}
        redis = AsyncMock()
        r = SkillRouter(
            runner=runner,
            redis=redis,
            gemma_api_base="http://localhost:8000/v1",
            telemetry_path=tmp_path / "tele.json",
        )
        return r

    @pytest.mark.asyncio
    async def test_run_records_success(self, router, tmp_path):
        from services.orchestrator import skill_telemetry as st

        router.select = AsyncMock(return_value="web-search")
        router.plan_tool_call = AsyncMock(return_value={"tool": "search", "arguments": {}})
        router.execute = AsyncMock(return_value={"ok": True, "result": "done"})

        result = await router.run("find papers")

        assert result["ok"] is True
        store = st.load(tmp_path / "tele.json")
        assert store["skills"]["web-search"]["use_count"] == 1
        assert store["skills"]["web-search"]["success_count"] == 1

    @pytest.mark.asyncio
    async def test_run_records_failure(self, router, tmp_path):
        from services.orchestrator import skill_telemetry as st

        router.select = AsyncMock(return_value="web-search")
        router.plan_tool_call = AsyncMock(return_value={"tool": "search", "arguments": {}})
        router.execute = AsyncMock(return_value={"ok": False, "error": "timeout"})

        await router.run("find papers")

        store = st.load(tmp_path / "tele.json")
        assert store["skills"]["web-search"]["fail_count"] == 1
        assert store["skills"]["web-search"]["success_count"] == 0

    @pytest.mark.asyncio
    async def test_run_unaffected_by_telemetry_failure(self, router):
        """A telemetry exception must NOT break the dispatch return value."""
        from services.orchestrator import skill_router as sr

        router.select = AsyncMock(return_value="web-search")
        router.plan_tool_call = AsyncMock(return_value={"tool": "search", "arguments": {}})
        router.execute = AsyncMock(return_value={"ok": True, "result": "done"})

        with patch.object(sr, "record_use_best_effort", side_effect=RuntimeError("telemetry boom")):
            # record_use_best_effort itself swallows, but even if a future
            # change made it raise, run() wraps the call defensively.
            result = await router.run("find papers")

        assert result is not None and result["ok"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_router.py::TestSkillRouterTelemetry -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'telemetry_path'`.

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/skill_router.py`, add the import near the other local imports (after `from services.skill_runner.skill_runner import SkillRunner`):

```python
from services.orchestrator.skill_telemetry import record_use_best_effort
```

Add `telemetry_path` to `__init__` (extend the existing signature and body):

```python
    def __init__(
        self,
        runner: SkillRunner,
        redis: aioredis.Redis,
        gemma_api_base: str,
        *,
        call_timeout: float = float(os.getenv("SKILL_CALL_TIMEOUT", "135")),
        telemetry_path: "Path | None" = None,
    ) -> None:
        # ... existing docstring + assignments unchanged ...
        self._runner = runner
        self._redis = redis
        self._gemma_base = gemma_api_base
        self._call_timeout = call_timeout
        self._telemetry_path = telemetry_path
        self._last_reasoning: str = ""
```

Add the `Path` import at the top of the file if not present:

```python
from pathlib import Path
```

In `run()`, immediately after `ok = bool(result.get("ok"))` and before the `tool.done` emit, insert the best-effort record (defensively wrapped — `record_use_best_effort` already swallows, but the wrap guards against any future signature/import surprise so dispatch is never affected):

```python
            ok = bool(result.get("ok"))
            try:
                record_use_best_effort(skill_name, ok, path=self._telemetry_path)
            except Exception:  # pragma: no cover - telemetry must never break dispatch
                _log.warning("skill telemetry wire-in failed for %s", skill_name, exc_info=True)
            await events.emit(
                "tool.done",
                # ... unchanged ...
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_router.py -q`
Expected: PASS (existing skill_router tests still green + 3 new telemetry tests pass).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_router.py tests/services/orchestrator/test_skill_router.py
git commit -m "feat(telemetry): wire best-effort record_use into SkillRouter.run"
```

---

## Task 6: BDD contract

**Files:**
- Create: `tests/services/orchestrator/features/skill_usage_telemetry.feature`
- Create: `tests/services/orchestrator/test_skill_usage_telemetry_bdd.py`

**Interfaces:**
- Consumes: `skill_telemetry` module (Tasks 1–4), `SkillRouter` (Task 5), `tests.conftest.run_async`.
- Produces: nothing (test-only).

- [ ] **Step 1: Write the failing feature + step defs**

```gherkin
# tests/services/orchestrator/features/skill_usage_telemetry.feature
@mocked
Feature: Skill usage telemetry (foundation for the curator)
  As the orchestrator
  I want every skill dispatch to bump a per-skill usage counter and timestamp
  So that an unused skill ages to stale/archived while pinned skills stay active,
  and so that a telemetry failure can never break a skill dispatch.

  Background:
    Given an empty telemetry store

  Scenario: A successful skill dispatch bumps use_count and success_count
    When the skill "web-search" is dispatched with result ok
    Then the telemetry use_count for "web-search" is 1
    And the telemetry success_count for "web-search" is 1
    And the telemetry fail_count for "web-search" is 0
    And the telemetry last_used_at for "web-search" is set

  Scenario: A failed skill dispatch bumps fail_count only
    When the skill "web-search" is dispatched with result fail
    Then the telemetry use_count for "web-search" is 1
    And the telemetry success_count for "web-search" is 0
    And the telemetry fail_count for "web-search" is 1

  Scenario: An unused skill computes to stale after the threshold
    Given the skill "ast-repo-map" was last used 40 days ago
    When the telemetry states are recomputed
    Then the telemetry state for "ast-repo-map" is "stale"

  Scenario: A pinned skill stays active past the archive threshold
    Given the skill "web-search" was last used 200 days ago and is pinned
    When the telemetry states are recomputed
    Then the telemetry state for "web-search" is "active"

  Scenario: A telemetry write failure does not break the dispatch
    Given telemetry persistence is broken
    When the skill "web-search" is dispatched through the router with result ok
    Then the router dispatch result is ok
```

```python
# tests/services/orchestrator/test_skill_usage_telemetry_bdd.py
"""Step definitions for the skill-usage-telemetry BDD contract.

Consumes: skill_telemetry (new_entry/record_use/compute_state/apply_transitions/
          load/save/record_use_best_effort) from skill_telemetry.py
          SkillRouter from skill_router.py
          run_async from tests.conftest
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator import skill_telemetry as st
from services.orchestrator import skill_router as sr
from services.orchestrator.skill_router import SkillRouter
from services.skill_runner.skill_runner import SkillRunner
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/skill_usage_telemetry.feature")

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx(tmp_path):
    """Shared mutable context for the scenario."""
    return {"path": tmp_path / "tele.json", "router_result": None}


@given("an empty telemetry store")
def _empty_store(ctx):
    st.save({"version": 1, "skills": {}}, ctx["path"])


@when(parsers.parse('the skill "{name}" is dispatched with result ok'))
def _dispatch_ok(ctx, name):
    st.record_use_best_effort(name, True, path=ctx["path"], now=NOW)


@when(parsers.parse('the skill "{name}" is dispatched with result fail'))
def _dispatch_fail(ctx, name):
    st.record_use_best_effort(name, False, path=ctx["path"], now=NOW)


@given(parsers.parse('the skill "{name}" was last used {days:d} days ago'))
def _seed_last_used(ctx, name, days):
    store = st.load(ctx["path"])
    entry = st.new_entry(NOW - timedelta(days=days))
    entry["last_used_at"] = (NOW - timedelta(days=days)).isoformat()
    store["skills"][name] = entry
    st.save(store, ctx["path"])


@given(parsers.parse('the skill "{name}" was last used {days:d} days ago and is pinned'))
def _seed_pinned(ctx, name, days):
    store = st.load(ctx["path"])
    entry = st.new_entry(NOW - timedelta(days=days))
    entry["last_used_at"] = (NOW - timedelta(days=days)).isoformat()
    entry["pinned"] = True
    store["skills"][name] = entry
    st.save(store, ctx["path"])


@when("the telemetry states are recomputed")
def _recompute(ctx):
    store = st.load(ctx["path"])
    store = st.apply_transitions(store, NOW)
    st.save(store, ctx["path"])


@given("telemetry persistence is broken")
def _break_persistence(ctx):
    ctx["_patch"] = patch.object(st, "save", side_effect=OSError("disk full"))
    ctx["_patch"].start()


@when(parsers.parse('the skill "{name}" is dispatched through the router with result ok'))
def _dispatch_via_router(ctx, name):
    runner = MagicMock(spec=SkillRunner)
    runner.catalog = {name: MagicMock()}
    router = SkillRouter(
        runner=runner,
        redis=AsyncMock(),
        gemma_api_base="http://localhost:8000/v1",
        telemetry_path=ctx["path"],
    )
    router.select = AsyncMock(return_value=name)
    router.plan_tool_call = AsyncMock(return_value={"tool": "t", "arguments": {}})
    router.execute = AsyncMock(return_value={"ok": True, "result": "done"})
    try:
        ctx["router_result"] = run_async(router.run("do it"))
    finally:
        if "_patch" in ctx:
            ctx["_patch"].stop()


@then(parsers.parse('the telemetry use_count for "{name}" is {n:d}'))
def _check_use(ctx, name, n):
    assert st.load(ctx["path"])["skills"][name]["use_count"] == n


@then(parsers.parse('the telemetry success_count for "{name}" is {n:d}'))
def _check_success(ctx, name, n):
    assert st.load(ctx["path"])["skills"][name]["success_count"] == n


@then(parsers.parse('the telemetry fail_count for "{name}" is {n:d}'))
def _check_fail(ctx, name, n):
    assert st.load(ctx["path"])["skills"][name]["fail_count"] == n


@then(parsers.parse('the telemetry last_used_at for "{name}" is set'))
def _check_last_used(ctx, name):
    assert st.load(ctx["path"])["skills"][name]["last_used_at"] is not None


@then(parsers.parse('the telemetry state for "{name}" is "{state}"'))
def _check_state(ctx, name, state):
    assert st.load(ctx["path"])["skills"][name]["state"] == state


@then("the router dispatch result is ok")
def _check_router_ok(ctx):
    assert ctx["router_result"] is not None
    assert ctx["router_result"]["ok"] is True
```

- [ ] **Step 2: Run to verify it fails (then passes once Tasks 1–5 are in)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_skill_usage_telemetry_bdd.py -q`
Expected before Tasks 1–5: collection/attribute errors. After Tasks 1–5: this is the final task, so expect PASS (5 scenarios).

- [ ] **Step 3: (no separate impl — feature is satisfied by Tasks 1–5)**

- [ ] **Step 4: Run the full orchestrator suite to confirm no regression**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS (all prior tests green + new telemetry unit/BDD tests).

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/skill_usage_telemetry.feature tests/services/orchestrator/test_skill_usage_telemetry_bdd.py
git commit -m "test(telemetry): pytest-bdd contract for skill usage telemetry"
```

---

## Self-Review

**1. Spec coverage:**
- Telemetry store, central JSON, justification → File Map + store-location note. ✓
- Fields `use_count/success_count/fail_count/last_used_at/created_by/state/pinned/created_at` → `new_entry` (Task 1). ✓
- `record_use(store, name, ok, now)` bumps counts + last_used → Task 1 tests. ✓
- `load`/`save` atomic (temp file + `os.replace`, like memory atomic writes) → Task 3 (`save` uses `mkstemp` in same dir + `os.replace`; concurrent-writer test proves no torn file). ✓
- Pure `compute_state(entry, now, stale_after_days, archive_after_days)` honoring `pinned`; transitions at thresholds → Task 2 (threshold-boundary + pinned-bypass tests). ✓
- Wire `record_use(name, ok)` at dispatch-completion seam returning `ok` → Task 5 wires into `SkillRouter.run()` right after `ok = bool(result.get("ok"))`. ✓
- Best-effort (telemetry failure never breaks dispatch, try/except → stderr) → Task 4 wrapper swallows + Task 5 defensive wrap + tests `*_swallows_*` / `*_unaffected_by_telemetry_failure`. ✓
- Env knobs `SKILL_STALE_AFTER_DAYS` (30), `SKILL_ARCHIVE_AFTER_DAYS` (90) → `_env_int` in Task 4 + test `*_reads_env_thresholds`. ✓
- BDD `.feature` with all five required scenarios (success bump, fail bump, unused→stale, pinned stays active, write-failure doesn't break dispatch) → Task 6. ✓
- CLAUDE.md: stdout-sacred (logger→stderr only), no tiktoken (none imported). ✓

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". Every code step shows full code; every command has expected output. ✓

**3. Type consistency:** `record_use(store, name, ok, now)`, `compute_state(entry, now, stale_after_days, archive_after_days)`, `apply_transitions(store, now, stale_after_days, archive_after_days)`, `load(path)`, `save(store, path)`, `default_store_path()`, `record_use_best_effort(name, ok, *, path, now, stale_after_days, archive_after_days)`, constructor `telemetry_path` — names used identically in unit tests, router wire-in, and BDD steps. State constants `STATE_ACTIVE/STALE/ARCHIVED` used consistently. ✓

**Note on one import detail:** Task 1 imports `from datetime import datetime`; Task 4 needs `timezone` too. The implementer must end with a single `from datetime import datetime, timezone` line (called out in Task 4 Step 3). Verify no duplicate import lines remain.
