# Local-Harness Piece 1 — Checkpointer → SqliteSaver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In `LABMATE_LOCAL_MODE`, `build_graph()` constructs a local `SqliteSaver` LangGraph checkpointer (embedded, single file per user) instead of the networked `MongoDBSaver` — behind the flag, with the pod path byte-for-byte unchanged and the full 1431-test orchestrator suite green.

**Architecture:** Piece 1 of the strangler migration (`docs/superpowers/specs/2026-07-03-local-harness-rearchitecture-design.md`, seam map Seam 1). Adds a local state-path helper to `local_mode.py` and branches the checkpointer construction inside `build_graph()` on `local_mode_enabled()`. The pod branch keeps the exact `from pymongo import MongoClient` / `from langgraph.checkpoint.mongodb import MongoDBSaver` / `MongoDBSaver(client, db_name=db_name)` lines so the existing tests that patch those symbols still pass. The SQLite import stays lazy (inside a helper) so a pod-only deploy never imports `langgraph-checkpoint-sqlite`.

**Tech Stack:** Python 3.12, LangGraph, `langgraph-checkpoint-sqlite==3.1.0` (new dep; `SqliteSaver(sqlite3.Connection)`), pytest.

## Global Constraints

- Full orchestrator suite `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q` must stay green (baseline **1431 passed** at branch point) after every task.
- Pod mode (flag OFF, the default) must remain **byte-for-byte behavior-preserving**: `build_graph` still constructs `MongoDBSaver` via `from pymongo import MongoClient` + `from langgraph.checkpoint.mongodb import MongoDBSaver` + `MongoDBSaver(client, db_name=db_name)`, so the existing tests' `patch("pymongo.MongoClient", ...)` and `patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=<MemorySaver>)` continue to intercept construction. Do NOT change the pod-branch symbol names or import paths.
- `build_graph(...)` keeps its signature and its `(compiled_graph, checkpointer)` return contract. The caller `main.py` (`graph, _cp = build_graph(...)`) is unchanged.
- Flag is `local_mode_enabled()` from `services/orchestrator/local_mode.py` (read at call time).
- The SQLite import must be **lazy** (inside the local-branch helper), so `import services.orchestrator.graph` does not require `langgraph-checkpoint-sqlite` in a pod deploy.
- SqliteSaver is constructed with a persistent connection: `sqlite3.connect(path, check_same_thread=False)` (the graph runs under async and LangGraph drives the sync saver from a threadpool — `check_same_thread=False` is required). Parent directory of the DB path is created if missing.
- State-path env contract: `LABMATE_STATE_DB` (full file path) overrides; else `LABMATE_STATE_DIR` (default `.data`, matching the existing `CURATOR_STATE_DIR` convention) `/ labmate_state.sqlite`.
- Repo conventions: never `git add -A` (stage exact paths); never commit `services/frontend/src/config.ts` or `.codegraph/daemon.pid`; commits end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`; Python `snake_case`; stdout sacred (this is not an MCP server, but no stray `print`).

---

## File Structure

- `services/orchestrator/local_mode.py` (**modify**) — add two leaf path helpers (`local_state_dir()`, `local_state_db_path()`). Stays a leaf (stdlib `os` + `pathlib` only) so any later piece can import it freely.
- `services/orchestrator/graph.py` (**modify**) — `build_graph()`: branch the checkpointer construction on `local_mode_enabled()`; add a lazy `_make_sqlite_checkpointer()` module helper.
- `services/orchestrator/requirements.txt` (**modify**) — add `langgraph-checkpoint-sqlite`.
- `tests/services/orchestrator/test_local_mode.py` (**modify**) — tests for the new path helpers.
- `tests/services/orchestrator/test_checkpointer_local_mode.py` (**new**) — tests: local mode builds a functional `SqliteSaver` (real put/get round-trip + file created + parent dir made); pod mode still constructs `MongoDBSaver` (behavior-preserving).

---

### Task 1: Local state-path helpers in `local_mode.py`

**Files:**
- Modify: `services/orchestrator/local_mode.py`
- Test: `tests/services/orchestrator/test_local_mode.py`

**Interfaces:**
- Consumes: nothing (leaf; `os` + `pathlib`).
- Produces:
  - `local_state_dir() -> pathlib.Path` — `Path(os.getenv("LABMATE_STATE_DIR", ".data"))`, read at call time.
  - `local_state_db_path() -> pathlib.Path` — `Path(LABMATE_STATE_DB)` if that env is set (non-empty), else `local_state_dir() / "labmate_state.sqlite"`. Read at call time. Task 2 imports `local_state_db_path`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_local_mode.py`:

```python
from pathlib import Path

from services.orchestrator.local_mode import local_state_dir, local_state_db_path


def test_local_state_dir_default(monkeypatch):
    monkeypatch.delenv("LABMATE_STATE_DIR", raising=False)
    assert local_state_dir() == Path(".data")


def test_local_state_dir_env_override(monkeypatch):
    monkeypatch.setenv("LABMATE_STATE_DIR", "/tmp/lm-state")
    assert local_state_dir() == Path("/tmp/lm-state")


def test_local_state_db_path_default(monkeypatch):
    monkeypatch.delenv("LABMATE_STATE_DB", raising=False)
    monkeypatch.delenv("LABMATE_STATE_DIR", raising=False)
    assert local_state_db_path() == Path(".data") / "labmate_state.sqlite"


def test_local_state_db_path_follows_state_dir(monkeypatch):
    monkeypatch.delenv("LABMATE_STATE_DB", raising=False)
    monkeypatch.setenv("LABMATE_STATE_DIR", "/tmp/lm-state")
    assert local_state_db_path() == Path("/tmp/lm-state") / "labmate_state.sqlite"


def test_local_state_db_path_full_override(monkeypatch):
    monkeypatch.setenv("LABMATE_STATE_DB", "/tmp/custom/my.sqlite")
    monkeypatch.setenv("LABMATE_STATE_DIR", "/tmp/lm-state")  # ignored when DB override set
    assert local_state_db_path() == Path("/tmp/custom/my.sqlite")


def test_local_state_db_path_empty_override_falls_back(monkeypatch):
    monkeypatch.setenv("LABMATE_STATE_DB", "")  # empty = unset
    monkeypatch.delenv("LABMATE_STATE_DIR", raising=False)
    assert local_state_db_path() == Path(".data") / "labmate_state.sqlite"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_mode.py -q -k "state_dir or state_db"`
Expected: FAIL — `ImportError: cannot import name 'local_state_dir'`.

- [ ] **Step 3: Implement the helpers**

In `services/orchestrator/local_mode.py`, add `from pathlib import Path` to the imports (below `import os`), and append these two functions after `local_mode_enabled()`:

```python
def local_state_dir() -> Path:
    """Directory holding the per-user local state (SQLite DB + local files).

    ``LABMATE_STATE_DIR`` (default ``.data`` — matches the ``CURATOR_STATE_DIR``
    convention). Read at call time. Relative paths resolve against the process
    CWD, as the rest of the ``.data`` usage does.
    """
    return Path(os.getenv("LABMATE_STATE_DIR", ".data"))


def local_state_db_path() -> Path:
    """Path to the local SQLite state DB (LangGraph checkpoints; later: sessions).

    ``LABMATE_STATE_DB`` (a full file path) overrides everything; otherwise
    ``local_state_dir() / "labmate_state.sqlite"``. An empty ``LABMATE_STATE_DB``
    is treated as unset. Read at call time.
    """
    override = os.getenv("LABMATE_STATE_DB")
    if override:
        return Path(override)
    return local_state_dir() / "labmate_state.sqlite"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_mode.py -q`
Expected: PASS (all local_mode tests, old + new).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/local_mode.py tests/services/orchestrator/test_local_mode.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): local state-path helpers (local-harness Piece 1)

local_state_dir()/local_state_db_path() resolve the per-user SQLite state
location (LABMATE_STATE_DB override, else LABMATE_STATE_DIR/.data). Leaf
module, read at call time. Consumed by the SQLite checkpointer next.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Branch `build_graph()` to a SQLite checkpointer in local mode

**Files:**
- Modify: `services/orchestrator/graph.py`
- Modify: `services/orchestrator/requirements.txt`
- Test: `tests/services/orchestrator/test_checkpointer_local_mode.py` (new)

**Interfaces:**
- Consumes: `local_mode_enabled`, `local_state_db_path` (Task 1).
- Produces: unchanged public contract — `build_graph(orch, async_orch, mongo_uri=MONGO_URI, db_name="labmate") -> (compiled_graph, checkpointer)`. In local mode `checkpointer` is a `langgraph.checkpoint.sqlite.SqliteSaver`; in pod mode it stays a `MongoDBSaver`. New module-private helper `_make_sqlite_checkpointer() -> SqliteSaver`.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_checkpointer_local_mode.py`:

```python
"""Piece 1: build_graph selects a local SqliteSaver in LABMATE_LOCAL_MODE,
and keeps the MongoDBSaver pod path unchanged when the flag is off."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from langgraph.checkpoint.memory import MemorySaver

from services.orchestrator.coding_orchestrator import AsyncOrchestrator, CodingOrchestrator
from services.orchestrator.graph import build_graph


def _mocks():
    return MagicMock(spec=CodingOrchestrator), MagicMock(spec=AsyncOrchestrator)


def test_local_mode_builds_sqlite_checkpointer(monkeypatch, tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = tmp_path / "nested" / "state.sqlite"  # parent dir does not exist yet
    monkeypatch.setenv("LABMATE_LOCAL_MODE", "1")
    monkeypatch.setenv("LABMATE_STATE_DB", str(db))

    mock_orch, mock_async = _mocks()
    graph, cp = build_graph(mock_orch, mock_async)

    assert isinstance(cp, SqliteSaver)
    assert db.exists()  # parent dir created + file opened
    assert graph is not None


def test_local_mode_checkpointer_round_trips(monkeypatch, tmp_path):
    """The returned SqliteSaver actually persists and reloads a checkpoint."""
    db = tmp_path / "state.sqlite"
    monkeypatch.setenv("LABMATE_LOCAL_MODE", "1")
    monkeypatch.setenv("LABMATE_STATE_DB", str(db))

    mock_orch, mock_async = _mocks()
    _graph, cp = build_graph(mock_orch, mock_async)

    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    checkpoint = {
        "v": 1,
        "id": "c1",
        "ts": "2026-07-03T00:00:00+00:00",
        "channel_values": {"n": 7},
        "channel_versions": {},
        "versions_seen": {},
    }
    cp.put(cfg, checkpoint, {}, {})
    loaded = cp.get(cfg)
    assert loaded is not None
    assert loaded["channel_values"]["n"] == 7


def test_pod_mode_still_builds_mongodb_saver(monkeypatch):
    """Flag OFF (default) -> pod path: MongoDBSaver constructed via the patched
    symbols, SqliteSaver branch NOT taken. Behavior-preserving."""
    monkeypatch.delenv("LABMATE_LOCAL_MODE", raising=False)
    monkeypatch.delenv("LABMATE_STATE_DB", raising=False)

    mock_orch, mock_async = _mocks()
    sentinel = MemorySaver()
    with patch("pymongo.MongoClient", return_value=MagicMock()):
        with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=sentinel):
            graph, cp = build_graph(mock_orch, mock_async)
    assert cp is sentinel  # the pod construction path ran
    assert graph is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_checkpointer_local_mode.py -q`
Expected: FAIL — the two local-mode tests fail because `build_graph` currently always constructs `MongoDBSaver` (which, unpatched, tries to reach Mongo / returns a non-SqliteSaver). `test_pod_mode_still_builds_mongodb_saver` should already PASS (pod path unchanged) — that's fine; it guards the branch you're about to add.

- [ ] **Step 3: Add the dependency**

In `services/orchestrator/requirements.txt`, add a line immediately after `langgraph-checkpoint-mongodb`:

```
langgraph-checkpoint-sqlite>=3.1
```

Confirm it is importable in this environment (already installed during planning; if not: `pip install "langgraph-checkpoint-sqlite>=3.1"`):

Run: `python -c "from langgraph.checkpoint.sqlite import SqliteSaver; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Implement the branch + helper**

In `services/orchestrator/graph.py`:

(a) Add imports near the top of the module (with the other `from services.orchestrator...` / local imports — match existing import placement), so both are available at call time:

```python
from services.orchestrator.local_mode import local_mode_enabled, local_state_db_path
```

(b) Add this module-level helper above `build_graph` (its sqlite imports are lazy — kept inside the function body so pod deploys never import `langgraph-checkpoint-sqlite`):

```python
def _make_sqlite_checkpointer():
    """Construct a local SqliteSaver at the per-user state DB path.

    Persistent connection (check_same_thread=False) because LangGraph drives
    the sync saver from a threadpool under async graph execution. Parent dir
    is created if missing. Imports are lazy so pod deploys don't require
    langgraph-checkpoint-sqlite. Held for the graph's lifetime by the caller.
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = local_state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)
```

(c) In `build_graph`, replace the checkpointer-construction tail. The current code (around lines 1015–1020) is:

```python
    from pymongo import MongoClient

    client = MongoClient(mongo_uri)
    cp = MongoDBSaver(client, db_name=db_name)
    graph = b.compile(checkpointer=cp)
    return graph, cp
```

Replace it with:

```python
    if local_mode_enabled():
        cp = _make_sqlite_checkpointer()
    else:
        from pymongo import MongoClient

        client = MongoClient(mongo_uri)
        cp = MongoDBSaver(client, db_name=db_name)
    graph = b.compile(checkpointer=cp)
    return graph, cp
```

(d) The pod-branch depends on the `MongoDBSaver` name. It is currently imported near the top of `build_graph` (the line `from langgraph.checkpoint.mongodb import MongoDBSaver`). Move that import **into the `else` branch**, immediately above `from pymongo import MongoClient`, so local mode does not import Mongo:

```python
    else:
        from langgraph.checkpoint.mongodb import MongoDBSaver
        from pymongo import MongoClient

        client = MongoClient(mongo_uri)
        cp = MongoDBSaver(client, db_name=db_name)
```

Delete the now-unused top-of-function `from langgraph.checkpoint.mongodb import MongoDBSaver`. (The existing tests patch `langgraph.checkpoint.mongodb.MongoDBSaver` — the source module attribute — so the moved import still resolves the patched object.)

- [ ] **Step 5: Run the new tests + the previously-patched build_graph tests**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_checkpointer_local_mode.py tests/services/orchestrator/test_graph.py -q`
Expected: PASS — the 3 new tests plus every existing `test_graph.py` build_graph test (they patch `MongoDBSaver`/`MongoClient`, still intercepted).

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q 2>&1 | tail -3`
Expected: all green — `1431 passed` baseline + new tests (Task 1's 6 + Task 2's 3), 0 failures.

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/graph.py services/orchestrator/requirements.txt tests/services/orchestrator/test_checkpointer_local_mode.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): SqliteSaver checkpointer in LABMATE_LOCAL_MODE (Piece 1)

build_graph() branches on local_mode_enabled(): local mode builds a
SqliteSaver at the per-user state DB (lazy sqlite import; parent dir
created; check_same_thread=False for the async threadpool). Pod path
keeps the exact MongoClient/MongoDBSaver lines so existing patched tests
still intercept construction — behavior-preserving. Adds the
langgraph-checkpoint-sqlite dependency.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Piece-completion gate

- [ ] Full suite green: `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q 2>&1 | tail -3` → `1431`+9 new passed, 0 failed.
- [ ] `git status` shows no unintended staged files (never `config.ts` / `daemon.pid`).
- [ ] Commit the plan doc onto the branch (`docs/superpowers/plans/2026-07-03-local-harness-piece1-checkpointer.md`).
- [ ] Open ONE PR: base `feat/local-harness-rearch`, head `feat/lh-piece1-checkpointer` (stacked on Piece 0). PR body ends with the 🤖 Generated with Claude Code footer.

## Self-review notes (author)

- **Spec coverage:** Piece 1 = "Checkpointer: `AsyncMongoDBSaver → SqliteSaver` behind the flag." The spec names `SqliteSaver` from `langgraph-checkpoint-sqlite`; the current code uses the sync `MongoDBSaver` (not Async), so the faithful mirror is the sync `SqliteSaver` (LangGraph drives it from a threadpool under async, exactly as it does the sync Mongo saver today). Covered by Task 2.
- **Behavior preservation:** the pod branch keeps the identical `from pymongo import MongoClient` / `from langgraph.checkpoint.mongodb import MongoDBSaver` / `MongoDBSaver(client, db_name=db_name)` lines; `test_pod_mode_still_builds_mongodb_saver` + the existing patched `test_graph.py` tests are the guard.
- **YAGNI:** no AsyncSqliteSaver, no connection-pool, no explicit `close()` (matches the current Mongo lifetime — caller holds `_cp`). `local_state_dir()` is added now because Piece 2 (sessions→SQLite) reuses the same state dir; `local_state_db_path()` is the only new symbol Task 2 strictly needs.
- **Type consistency:** `local_state_db_path` produced by Task 1 is the exact name imported by Task 2; `_make_sqlite_checkpointer` returns the `SqliteSaver` the tests assert.
