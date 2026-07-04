> **NOTE (superseded mid-piece):** `experimental` became **local-mode only** partway through this piece. Tasks 1–2 (the `LocalStore` module) landed as written. Task 3 shipped **local-only** instead of flag-gated: `StorageManager.search_turns` now *always* delegates to `LocalStore` and the Mongo `$text`/`$regex` path was deleted (no `LABMATE_LOCAL_MODE` branch). See the SDD ledger for the local-only sequencing.

# Local-Harness Piece 2a — LocalStore (SQLite turn store) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Introduce a local, async SQLite persistence module (`LocalStore`) that stores chat turns per session, and route `StorageManager.search_turns` to it when `LABMATE_LOCAL_MODE` is on — the foundation the continuity wiring (Piece 2b) builds on. Behavior-preserving in pod mode; full orchestrator suite green.

**Architecture:** Piece 2 of the strangler migration (design spec + seam-map Seam 2). Piece 2 is split into **2a** (this plan: the `LocalStore` turn store + its first consumer `search_turns`) and **2b** (the continuity wiring: orchestrator self-persists turns in `main._handle`, `ContextManager` turn-reads + `WorkspaceManager` branch on the store, continuity integration test). Splitting keeps each PR small and lets the low-risk foundation land independently of the sensitive `services/memory/` wiring. `LocalStore` uses `aiosqlite` (already present — a transitive dep of `langgraph-checkpoint-sqlite`) and the **same DB file** as the Piece 1 checkpointer (`local_state_db_path()`, additional tables). The research recommended **method-level branching** (`if local_mode ...`) over a Motor-emulation adapter; this plan follows that.

**Tech Stack:** Python 3.12, asyncio, `aiosqlite==0.22.1`, pytest + pytest-asyncio.

## Global Constraints

- Full orchestrator suite `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q` must stay green (baseline **1440 passed** at this branch point) after every task.
- Pod mode (flag OFF, default) strictly behavior-preserving: `StorageManager.search_turns` keeps its exact current Mongo `$text`/`$regex` path and its `[{"sessionId","seq","text"}]` return contract; `search_turns` remains best-effort (any error → `[]`, never raises).
- `LocalStore` reuses `local_state_db_path()` (Piece 1) — the SAME single SQLite file per user, additional `chat_turns` table (the LangGraph checkpointer's tables coexist in the same file). Parent dir created if missing.
- `aiosqlite` is async; never block the event loop with the stdlib `sqlite3` for store I/O. One long-lived connection per `LocalStore`, opened lazily, schema created idempotently on first use.
- Turn ordering contract (must match the existing Mongo path the continuity code depends on): turns are keyed by `(session_id, seq)`, `seq` is a per-session 0-based monotonic counter (the Mongo writer used `seq = len(existing_turns)`); `recent_turns` returns turns with `seq > watermark`, newest-capped to `limit`, returned **sorted ascending by seq**; `search_turns` returns `[{"sessionId","seq","text"}]` (camelCase `sessionId`, matching the Mongo contract).
- Flag read via `local_mode_enabled()` (call time). Repo conventions: never `git add -A`; never commit `services/frontend/src/config.ts` or `.codegraph/daemon.pid`; commits end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`; Python `snake_case`; stdout sacred.

---

## File Structure

- `services/orchestrator/local_store.py` (**new**) — the `LocalStore` class (async SQLite; connection + schema + turn read/write/search) and a `get_local_store()` cached accessor. One responsibility: local persistence. Piece 2b adds session/workspace methods here.
- `services/orchestrator/storage_manager.py` (**modify**) — a `local_store` property (a `LocalStore` in local mode, `None` in pod) and a `local_mode_enabled()` branch in `search_turns`.
- `services/orchestrator/requirements.txt` (**modify**) — pin `aiosqlite` explicitly (it is currently only a transitive dep).
- `tests/services/orchestrator/test_local_store.py` (**new**) — LocalStore behavior tests (real SQLite round-trips).
- `tests/services/orchestrator/test_search_turns_local_mode.py` (**new**) — `search_turns` routes to LocalStore in local mode; pod path unchanged.

---

### Task 1: `LocalStore` — connection, schema, and the turn table

**Files:**
- Create: `services/orchestrator/local_store.py`
- Modify: `services/orchestrator/requirements.txt`
- Test: `tests/services/orchestrator/test_local_store.py`

**Interfaces:**
- Consumes: `local_state_db_path` (Piece 1, `services/orchestrator/local_mode.py`).
- Produces:
  - `class LocalStore` with `__init__(self, db_path: str | os.PathLike)`, `async def connect(self) -> None` (idempotent; opens the aiosqlite connection, creates schema), `async def close(self) -> None`.
  - `get_local_store() -> LocalStore` — module-level accessor returning a process-cached `LocalStore` at `local_state_db_path()`. Task 3 imports both `LocalStore` and `get_local_store`.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_local_store.py`:

```python
"""Piece 2a: LocalStore SQLite persistence — connection + schema."""
from __future__ import annotations

import pytest

from services.orchestrator.local_store import LocalStore, get_local_store


@pytest.mark.asyncio
async def test_connect_creates_chat_turns_table(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        conn = store._conn  # connected handle
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_turns'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "chat_turns"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_connect_is_idempotent(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    await store.connect()  # must not raise / must not re-create
    await store.close()


@pytest.mark.asyncio
async def test_connect_creates_parent_dir(tmp_path):
    db = tmp_path / "nested" / "deep" / "s.sqlite"
    store = LocalStore(db)
    await store.connect()
    try:
        assert db.exists()
    finally:
        await store.close()


def test_get_local_store_uses_state_db_path(monkeypatch, tmp_path):
    db = tmp_path / "state.sqlite"
    monkeypatch.setenv("LABMATE_STATE_DB", str(db))
    s1 = get_local_store()
    s2 = get_local_store()
    assert s1 is s2  # process-cached singleton
    assert str(s1.db_path) == str(db)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.local_store'`.

- [ ] **Step 3: Add the explicit dependency**

In `services/orchestrator/requirements.txt`, add (it is currently only transitive via langgraph-checkpoint-sqlite):

```
aiosqlite>=0.20
```

- [ ] **Step 4: Implement `LocalStore` connection + schema**

Create `services/orchestrator/local_store.py`:

```python
"""Local, async SQLite persistence for the local-harness (LABMATE_LOCAL_MODE).

Piece 2 of the local-harness re-architecture. Stores chat turns (Piece 2a) and,
in Piece 2b, session/workspace metadata — the local replacement for the
Mongo-backed session/turn store. Uses aiosqlite so store I/O never blocks the
event loop, and the SAME per-user DB file as the Piece 1 LangGraph checkpointer
(local_state_db_path(); the checkpointer's tables coexist in the file).

Read/write contracts mirror the Mongo paths the continuity code depends on:
turns are keyed by (session_id, seq) with seq a per-session 0-based monotonic
counter; recent_turns returns seq>watermark newest-capped, ascending.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import aiosqlite

from .local_mode import local_state_db_path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_turns (
    session_id  TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    reasoning   TEXT,
    tool_calls  TEXT,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_session_seq
    ON chat_turns (session_id, seq);
"""


class LocalStore:
    """Async SQLite store for local-mode session/turn persistence."""

    def __init__(self, db_path: str | os.PathLike) -> None:
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the connection and create the schema. Idempotent."""
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self.db_path))
        await conn.executescript(_SCHEMA)
        await conn.commit()
        self._conn = conn

    async def _connected(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.connect()
        assert self._conn is not None
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


_STORE: LocalStore | None = None


def get_local_store() -> LocalStore:
    """Process-cached LocalStore at local_state_db_path().

    Cached because the connection is long-lived. Keyed by the resolved path at
    first call; tests that need a different path construct LocalStore directly.
    """
    global _STORE
    if _STORE is None:
        _STORE = LocalStore(local_state_db_path())
    return _STORE
```

Note: the test reads `store._conn` after `connect()`; that attribute is the live handle, so it is not None post-connect.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/local_store.py services/orchestrator/requirements.txt tests/services/orchestrator/test_local_store.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): LocalStore SQLite connection + chat_turns schema (Piece 2a)

Async aiosqlite store at the per-user state DB (shared with the Piece 1
checkpointer). connect() is idempotent and creates the chat_turns table +
index. get_local_store() is the process-cached accessor. Turn read/write
methods land next. Pins aiosqlite explicitly.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: LocalStore turn read/write/search methods

**Files:**
- Modify: `services/orchestrator/local_store.py`
- Test: `tests/services/orchestrator/test_local_store.py`

**Interfaces:**
- Consumes: `LocalStore.connect`/`_connected` (Task 1).
- Produces (all `async`, all on `LocalStore`):
  - `append_turn(session_id: str, role: str, text: str, *, created_at: str | None = None, reasoning: str | None = None, tool_calls: str | None = None) -> int` — inserts a turn with the next per-session `seq` (0-based; `seq = current_count`); returns the assigned `seq`. `created_at` defaults to an ISO-8601 UTC string.
  - `recent_turns(session_id: str, *, watermark: int = -1, limit: int = 50) -> list[dict]` — turns with `seq > watermark`, newest `limit`, returned **ascending by seq**; each dict `{"role","text","seq"}`.
  - `all_turns(session_id: str) -> list[dict]` — every turn ascending; each `{"seq","role","text"}`.
  - `last_activity_iso(session_id: str) -> str | None` — the newest turn's `created_at` (or None if no turns).
  - `search_turns(query: str, *, mode: str = "text", session_id: str | None = None, limit: int = 8) -> list[dict]` — `[{"sessionId","seq","text"}]`. Empty/whitespace query → `[]`. `mode="text"` → case-insensitive substring (`LIKE`) over `text`, newest-first. `mode="regex"` → Python `re` (case-insensitive) filter over the session's/all rows, newest-first. Best-effort: any error → `[]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_local_store.py`:

```python
import re


@pytest.mark.asyncio
async def test_append_turn_assigns_monotonic_seq_per_session(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        assert await store.append_turn("sess-A", "user", "hello") == 0
        assert await store.append_turn("sess-A", "assistant", "hi") == 1
        # A different session has its own seq counter.
        assert await store.append_turn("sess-B", "user", "yo") == 0
        assert await store.append_turn("sess-A", "user", "again") == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recent_turns_respects_watermark_and_returns_ascending(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        for i, txt in enumerate(["a", "b", "c", "d"]):
            await store.append_turn("s", "user", txt)
        # watermark=1 → only seq 2,3 ; ascending
        turns = await store.recent_turns("s", watermark=1)
        assert [t["seq"] for t in turns] == [2, 3]
        assert [t["text"] for t in turns] == ["c", "d"]
        assert turns[0]["role"] == "user"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recent_turns_limit_keeps_newest_tail_ascending(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        for i in range(5):
            await store.append_turn("s", "user", f"m{i}")
        turns = await store.recent_turns("s", watermark=-1, limit=2)
        assert [t["text"] for t in turns] == ["m3", "m4"]  # newest 2, ascending
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_all_turns_ascending(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        await store.append_turn("s", "user", "one")
        await store.append_turn("s", "assistant", "two")
        rows = await store.all_turns("s")
        assert [(r["seq"], r["role"], r["text"]) for r in rows] == [
            (0, "user", "one"),
            (1, "assistant", "two"),
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_last_activity_iso(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        assert await store.last_activity_iso("s") is None
        await store.append_turn("s", "user", "x", created_at="2026-07-03T00:00:00Z")
        await store.append_turn("s", "user", "y", created_at="2026-07-03T01:00:00Z")
        assert await store.last_activity_iso("s") == "2026-07-03T01:00:00Z"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_search_turns_text_and_regex(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        await store.append_turn("s", "user", "the quick brown fox")
        await store.append_turn("s", "assistant", "a lazy dog sleeps")
        await store.append_turn("other", "user", "quick unrelated")
        # text mode: case-insensitive substring, scoped to session
        hits = await store.search_turns("QUICK", mode="text", session_id="s")
        assert [h["text"] for h in hits] == ["the quick brown fox"]
        assert hits[0]["sessionId"] == "s"
        # regex mode
        rhits = await store.search_turns(r"la.y", mode="regex", session_id="s")
        assert [h["text"] for h in rhits] == ["a lazy dog sleeps"]
        # empty query → []
        assert await store.search_turns("  ", session_id="s") == []
        # no session scope → searches all
        allhits = await store.search_turns("quick", mode="text")
        assert len(allhits) == 2
    finally:
        await store.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store.py -q -k "turn or search or activity"`
Expected: FAIL — `AttributeError: 'LocalStore' object has no attribute 'append_turn'`.

- [ ] **Step 3: Implement the turn methods**

Add to `services/orchestrator/local_store.py`. First extend the imports at the top:

```python
import re
from datetime import UTC, datetime
```

Then add these methods to the `LocalStore` class (after `close`):

```python
    # ── turns ────────────────────────────────────────────────────────────
    async def append_turn(
        self,
        session_id: str,
        role: str,
        text: str,
        *,
        created_at: str | None = None,
        reasoning: str | None = None,
        tool_calls: str | None = None,
    ) -> int:
        """Insert a turn with the next per-session seq (0-based). Returns the seq."""
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chat_turns WHERE session_id = ?", (session_id,)
        )
        (count,) = await cur.fetchone()
        seq = int(count)
        ts = created_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        await conn.execute(
            "INSERT INTO chat_turns (session_id, seq, role, text, created_at, reasoning, tool_calls)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, seq, role, text, ts, reasoning, tool_calls),
        )
        await conn.commit()
        return seq

    async def recent_turns(
        self, session_id: str, *, watermark: int = -1, limit: int = 50
    ) -> list[dict]:
        """Turns with seq > watermark, newest `limit`, returned ascending by seq."""
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT seq, role, text FROM chat_turns"
            " WHERE session_id = ? AND seq > ?"
            " ORDER BY seq DESC LIMIT ?",
            (session_id, watermark, limit),
        )
        rows = await cur.fetchall()
        rows = list(reversed(rows))  # DESC+limit kept the newest tail; flip to ascending
        return [{"seq": r[0], "role": r[1], "text": r[2]} for r in rows]

    async def all_turns(self, session_id: str) -> list[dict]:
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT seq, role, text FROM chat_turns WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        return [{"seq": r[0], "role": r[1], "text": r[2]} for r in rows]

    async def last_activity_iso(self, session_id: str) -> str | None:
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT created_at FROM chat_turns WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
            (session_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def search_turns(
        self,
        query: str,
        *,
        mode: str = "text",
        session_id: str | None = None,
        limit: int = 8,
    ) -> list[dict]:
        """Keyword/regex search over chat_turns. Returns [{"sessionId","seq","text"}].

        Best-effort: any error returns []. Empty/whitespace query returns [].
        """
        if not (query or "").strip():
            return []
        try:
            conn = await self._connected()
            if mode == "regex":
                # Fetch candidate rows (session-scoped if given), filter in Python.
                if session_id:
                    cur = await conn.execute(
                        "SELECT session_id, seq, text FROM chat_turns"
                        " WHERE session_id = ? ORDER BY seq DESC",
                        (session_id,),
                    )
                else:
                    cur = await conn.execute(
                        "SELECT session_id, seq, text FROM chat_turns ORDER BY seq DESC",
                    )
                rows = await cur.fetchall()
                pat = re.compile(query, re.IGNORECASE)
                hits = [r for r in rows if pat.search(r[2] or "")][:limit]
            else:
                like = f"%{query}%"
                if session_id:
                    cur = await conn.execute(
                        "SELECT session_id, seq, text FROM chat_turns"
                        " WHERE session_id = ? AND text LIKE ? ESCAPE '\\'"
                        " ORDER BY seq DESC LIMIT ?",
                        (session_id, like, limit),
                    )
                else:
                    cur = await conn.execute(
                        "SELECT session_id, seq, text FROM chat_turns"
                        " WHERE text LIKE ? ESCAPE '\\' ORDER BY seq DESC LIMIT ?",
                        (like, limit),
                    )
                hits = await cur.fetchall()
            return [{"sessionId": r[0], "seq": r[1], "text": r[2]} for r in hits]
        except Exception as exc:  # best-effort — mirror the Mongo search_turns contract
            logger.error("LocalStore.search_turns failed (mode=%s): %s", mode, exc)
            return []
```

Note on `LIKE ... ESCAPE '\\'`: SQLite `LIKE` treats `%`/`_` in the query as wildcards; this is acceptable for a best-effort keyword search (the Mongo `$text` path also has its own tokenization semantics — exact parity is not required, only "finds matching turns"). The regex mode gives precise matching when needed.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store.py -q`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/local_store.py tests/services/orchestrator/test_local_store.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): LocalStore turn read/write/search methods (Piece 2a)

append_turn (per-session monotonic seq), recent_turns (seq>watermark,
newest-capped, ascending), all_turns, last_activity_iso, and best-effort
search_turns (text LIKE / regex) returning the {sessionId,seq,text} shape
the Mongo path uses. Real-SQLite round-trip tests.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Route `StorageManager.search_turns` to LocalStore in local mode

**Files:**
- Modify: `services/orchestrator/storage_manager.py`
- Test: `tests/services/orchestrator/test_search_turns_local_mode.py` (new)

**Interfaces:**
- Consumes: `LocalStore`, `get_local_store` (Tasks 1–2); `local_mode_enabled` (Piece 0).
- Produces: `StorageManager.local_store` property (a `LocalStore` in local mode via `get_local_store()`, else `None`); `search_turns` returns LocalStore results in local mode, unchanged Mongo results in pod mode.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_search_turns_local_mode.py`:

```python
"""Piece 2a: StorageManager.search_turns routes to LocalStore in local mode."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator.storage_manager import StorageManager


def _storage_with_mocks():
    # Mongo/Chroma/Redis are all MagicMock — no network. from_clients bypasses env.
    return StorageManager.from_clients(mongo=MagicMock(), chroma=MagicMock(), redis=MagicMock())


@pytest.mark.asyncio
async def test_local_mode_search_turns_uses_local_store(monkeypatch, tmp_path):
    monkeypatch.setenv("LABMATE_LOCAL_MODE", "1")
    monkeypatch.setenv("LABMATE_STATE_DB", str(tmp_path / "s.sqlite"))

    sm = _storage_with_mocks()
    # Seed via the same LocalStore the property resolves to.
    store = sm.local_store
    assert store is not None
    await store.append_turn("sess", "user", "find the alpha marker")

    hits = await sm.search_turns("alpha", session_id="sess")
    assert [h["text"] for h in hits] == ["find the alpha marker"]
    assert hits[0]["sessionId"] == "sess"


@pytest.mark.asyncio
async def test_pod_mode_search_turns_unchanged(monkeypatch):
    monkeypatch.delenv("LABMATE_LOCAL_MODE", raising=False)
    sm = _storage_with_mocks()
    assert sm.local_store is None  # no store constructed in pod mode

    # Pod path hits Mongo via self._db["chat_turns"]; stub the cursor chain.
    async def _aiter():
        for d in [{"sessionId": "s", "seq": 0, "text": "pod hit"}]:
            yield d

    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.__aiter__ = lambda self: _aiter()
    col = MagicMock()
    col.find.return_value = cursor
    sm._db = MagicMock()
    sm._db.__getitem__.return_value = col

    hits = await sm.search_turns("pod", session_id="s")
    assert hits == [{"sessionId": "s", "seq": 0, "text": "pod hit"}]
    col.find.assert_called()  # Mongo path taken, not LocalStore
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_search_turns_local_mode.py -q`
Expected: FAIL — `AttributeError: 'StorageManager' object has no attribute 'local_store'`.

- [ ] **Step 3: Implement the property + branch**

In `services/orchestrator/storage_manager.py`:

(a) Add imports near the other local imports (top of file, after `from .workspace_manager import WorkspaceManager`):

```python
from .local_mode import local_mode_enabled
from .local_store import LocalStore, get_local_store
```

(b) Add a `local_store` property (place it near the other properties, e.g. after the `workspaces` property):

```python
    @property
    def local_store(self) -> LocalStore | None:
        """The local SQLite store in LABMATE_LOCAL_MODE, else None (pod mode).

        Process-cached via get_local_store(). Lazy — constructed on first access
        in local mode; the connection opens on first store call.
        """
        if not local_mode_enabled():
            return None
        if not hasattr(self, "_local_store") or self._local_store is None:
            self._local_store = get_local_store()
        return self._local_store
```

(c) At the very top of `search_turns` (before the existing `if not (query or "").strip(): return []`), add the local-mode branch:

```python
        store = self.local_store
        if store is not None:
            return await store.search_turns(
                query, mode=mode, session_id=session_id, limit=top_k
            )
```

Leave the entire existing Mongo body unchanged below it.

- [ ] **Step 4: Run the new tests + existing session-search tests**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_search_turns_local_mode.py tests/services/orchestrator/test_session_search.py tests/services/orchestrator/test_storage_manager.py -q`
Expected: PASS — new local-mode tests + the existing pod `search_turns`/storage tests (still hit Mongo, `local_store is None` when the flag is unset).

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q 2>&1 | tail -3`
Expected: all green — `1440` baseline + the new tests (Task1 4 + Task2 6 + Task3 2), 0 failures.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/storage_manager.py tests/services/orchestrator/test_search_turns_local_mode.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): route search_turns to LocalStore in LABMATE_LOCAL_MODE (Piece 2a)

StorageManager gains a local_store property (LocalStore in local mode, None
in pod) and search_turns returns local results in local mode. Pod path
(Mongo $text/$regex) unchanged — existing session-search tests still green.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Piece-completion gate

- [ ] Full suite green: `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q 2>&1 | tail -3` → `1440`+12 new passed, 0 failed.
- [ ] `git status` shows no unintended staged files (never `config.ts` / `daemon.pid`).
- [ ] Commit the plan doc onto the branch.
- [ ] Open ONE PR: base `feat/lh-piece1-checkpointer`, head `feat/lh-piece2-sessions` (stacked). PR body ends with the 🤖 footer, and notes Piece 2b (continuity wiring) follows.

## Self-review notes (author)

- **Scope discipline:** 2a delivers only the turn store + its one real consumer (`search_turns`). Sessions/workspaces tables + methods, the `main._handle` turn-write, and the `ContextManager`/`WorkspaceManager` continuity branches are Piece **2b** — deferred so this PR stays small and every added symbol has a consumer (no YAGNI surface).
- **Behavior preservation:** `search_turns` gains a leading `if self.local_store is not None` branch; pod mode has `local_store is None`, so the exact Mongo body runs — guarded by `test_pod_mode_search_turns_unchanged` + the existing session-search suite.
- **Contract fidelity:** `recent_turns` (seq>watermark, newest-capped, ascending) and `search_turns` (`{sessionId,seq,text}`) match the Mongo shapes the continuity code (Piece 2b) will consume, so 2b needs no shape adaptation.
- **Type consistency:** `LocalStore`, `get_local_store`, `append_turn`, `recent_turns`, `all_turns`, `last_activity_iso`, `search_turns`, and `StorageManager.local_store` are the exact names 2b and later pieces import.
