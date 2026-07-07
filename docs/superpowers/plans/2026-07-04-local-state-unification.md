# Local State Unification (Mongo → SQLite) + Infra/Docs Local-ization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop MongoDB as a boot dependency of the local single-process harness by unifying all persistent state (auth users, sessions, turns, loop checkpoints) onto the existing SQLite `LocalStore` as the single source of truth, and bring the infra scripts + docs into line with the local single-process runtime.

**Architecture:** `LocalStore` (aiosqlite, one DB file, already WAL-shared with the LangGraph SqliteSaver) gains `auth_users` + `loop_checkpoints` tables and a `payload` column on `chat_turns`. New `SqliteUserStore` + `SqliteSessionStore` implement the existing gateway Protocol/interface seams over it. `chat_turns` becomes the single turn store; the gateway stays the rich writer, the orchestrator's `_persist_turns` becomes a fallback writer, coordinated by a per-task "relay owns persistence" flag on `SignalRegistry` (hermes `skip_db` pattern). Mongo classes + `db_indexes.py` + `motor`/`pymongo` are deleted; the infra scripts launch `services.local.main` and stop provisioning mongod/redis/chroma.

**Tech Stack:** Python 3.11 (CI) / 3.12 (local), aiosqlite, pytest + pytest-asyncio, FastAPI (ws_gateway), bash infra scripts.

**Spec:** `docs/superpowers/specs/2026-07-04-local-state-unification-design.md`

## Global Constraints

- Branch `exp/local-state-sqlite` (off `experimental`); pod version on `main` stays untouched.
- Full `tests/services/orchestrator` (CI scope) + `tests/services/ws_gateway` + `tests/services/local` + `tests/services/cli` suites stay GREEN after EVERY task. No live E2E (GPU/model powered off).
- Stage by exact path — never `git add -A`; never commit `services/frontend/src/config.ts`, `.codegraph/daemon.pid`, or `services/frontend/.claude/`.
- Every commit message ends with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- stdout is sacred in MCP/servers (log to stderr). No tiktoken. No `asyncio.run()` in async contexts. `redis>=5,<6` (unaffected here).
- `LOCAL_USER` = a fixed sentinel user id (`"u-local"`) that `SqliteSessionStore` uses for both `record_session` and `list_sessions` — the local harness is single-user, and sessions are keyed to this sentinel consistently (turns are keyed by `session_id`, not user), so it need NOT equal the auth admin's generated id.
- Keep the `InMemoryUserStore` / `InMemorySessionStore` test seams intact (tests inject them).

---

### Task 1: LocalStore `auth_users` table + methods

**Files:**
- Modify: `services/orchestrator/local_store.py` (add to `_SCHEMA`; add 3 methods)
- Test: `tests/services/orchestrator/test_local_store_auth_users.py` (new)

**Interfaces:**
- Produces: `LocalStore.auth_user_find_by_email(email:str) -> dict|None` (keys `id,email,displayName,passwordHash,role,createdAt`), `LocalStore.auth_user_create(*, id:str, email:str, display_name:str, password_hash:str, role:str, created_at:str) -> None`, `LocalStore.auth_user_count() -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_local_store_auth_users.py
import pytest
from services.orchestrator.local_store import LocalStore


@pytest.mark.asyncio
async def test_auth_user_create_find_count(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    assert await store.auth_user_count() == 0
    await store.auth_user_create(
        id="u-abc", email="Admin@X.io", display_name="Admin",
        password_hash="h", role="admin", created_at="2026-07-04T00:00:00Z",
    )
    assert await store.auth_user_count() == 1
    got = await store.auth_user_find_by_email("admin@x.io")  # case-insensitive
    assert got == {
        "id": "u-abc", "email": "admin@x.io", "displayName": "Admin",
        "passwordHash": "h", "role": "admin", "createdAt": "2026-07-04T00:00:00Z",
    }
    assert await store.auth_user_find_by_email("missing@x.io") is None
    await store.close()


@pytest.mark.asyncio
async def test_auth_user_create_duplicate_email_raises(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    await store.auth_user_create(id="u-1", email="a@x.io", display_name="A",
                                 password_hash="h", role="user", created_at="t")
    with pytest.raises(Exception):
        await store.auth_user_create(id="u-2", email="A@x.io", display_name="A2",
                                     password_hash="h2", role="user", created_at="t2")
    await store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store_auth_users.py -q`
Expected: FAIL (`AttributeError: 'LocalStore' object has no attribute 'auth_user_create'`).

- [ ] **Step 3: Add the table to `_SCHEMA`** (in `services/orchestrator/local_store.py`, inside the `_SCHEMA` string, after the existing `users` table):

```sql
CREATE TABLE IF NOT EXISTS auth_users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    display_name  TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
```

- [ ] **Step 4: Add the methods** (after the existing user methods, e.g. near `touch_user`):

```python
    # ── auth users (ws_gateway account store) ────────────────────────────
    async def auth_user_find_by_email(self, email: str) -> dict | None:
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT id, email, display_name, password_hash, role, created_at"
            " FROM auth_users WHERE email = ?",
            (email.lower(),),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "email": row[1], "displayName": row[2],
            "passwordHash": row[3], "role": row[4], "createdAt": row[5],
        }

    async def auth_user_create(
        self, *, id: str, email: str, display_name: str,
        password_hash: str, role: str, created_at: str,
    ) -> None:
        conn = await self._connected()
        await conn.execute(
            "INSERT INTO auth_users (id, email, display_name, password_hash, role, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (id, email.lower(), display_name, password_hash, role, created_at),
        )
        await conn.commit()

    async def auth_user_count(self) -> int:
        conn = await self._connected()
        cur = await conn.execute("SELECT COUNT(*) FROM auth_users")
        (n,) = await cur.fetchone()
        return int(n)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store_auth_users.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/local_store.py tests/services/orchestrator/test_local_store_auth_users.py
git commit -m "feat(local-store): auth_users table + accessors (state unification T1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: LocalStore `chat_turns.payload` column + rich-turn methods + `delete_session`

**Files:**
- Modify: `services/orchestrator/local_store.py` (`connect()` migration; 3 methods)
- Test: `tests/services/orchestrator/test_local_store_turns_payload.py` (new)

**Interfaces:**
- Produces: `LocalStore.append_turn_payload(session_id:str, role:str, text:str, payload:dict, *, created_at:str|None=None) -> int` (returns seq); `LocalStore.turns_with_payload(session_id:str) -> list[dict]` (each `{seq,role,text,created_at,payload}`, `payload` a dict or None); `LocalStore.delete_session(session_id:str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_local_store_turns_payload.py
import pytest
from services.orchestrator.local_store import LocalStore


@pytest.mark.asyncio
async def test_payload_round_trip_and_plain_null(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    rich = {"id": "t-1", "role": "assistant", "text": "hi",
            "reasoning": {"text": "r"}, "toolCalls": [{"name": "x"}]}
    seq = await store.append_turn_payload("s1", "assistant", "hi", rich)
    assert seq == 0
    await store.append_turn("s1", "user", "plain")  # legacy writer → payload NULL
    rows = await store.turns_with_payload("s1")
    assert rows[0]["payload"] == rich and rows[0]["role"] == "assistant"
    assert rows[1]["payload"] is None and rows[1]["text"] == "plain"
    # legacy readers unaffected
    assert [t["text"] for t in await store.all_turns("s1")] == ["hi", "plain"]
    await store.close()


@pytest.mark.asyncio
async def test_delete_session_removes_turns_and_kv(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    await store.record_session("s1", user_id="u")
    await store.append_turn("s1", "user", "hi")
    await store.session_kv_set("gw", "s1", '{"title":"T"}')
    await store.delete_session("s1")
    assert await store.all_turns("s1") == []
    assert await store.session_kv_get("gw", "s1") is None
    assert await store.list_sessions("u") == []
    await store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store_turns_payload.py -q`
Expected: FAIL (`AttributeError: ... append_turn_payload`).

- [ ] **Step 3: Add the `payload` migration to `connect()`** (in `services/orchestrator/local_store.py`, immediately after `await conn.executescript(_SCHEMA)` and before `await conn.commit()`):

```python
        # chat_turns.payload was added after initial ship; CREATE TABLE IF NOT
        # EXISTS won't alter an existing table, so add the column if missing.
        cur = await conn.execute("PRAGMA table_info(chat_turns)")
        cols = {row[1] for row in await cur.fetchall()}
        if "payload" not in cols:
            await conn.execute("ALTER TABLE chat_turns ADD COLUMN payload TEXT")
```

- [ ] **Step 4: Add the methods** (near `append_turn`/`all_turns`; requires `import json` at top — add if absent):

```python
    async def append_turn_payload(
        self, session_id: str, role: str, text: str, payload: dict,
        *, created_at: str | None = None,
    ) -> int:
        """Append a turn carrying the full rich turn dict as JSON (payload).
        Returns the per-session seq (0-based)."""
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chat_turns WHERE session_id = ?", (session_id,)
        )
        (count,) = await cur.fetchone()
        seq = int(count)
        ts = created_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        await conn.execute(
            "INSERT INTO chat_turns (session_id, seq, role, text, created_at, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, seq, role, text, ts, json.dumps(payload)),
        )
        await conn.commit()
        return seq

    async def turns_with_payload(self, session_id: str) -> list[dict]:
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT seq, role, text, created_at, payload FROM chat_turns"
            " WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            try:
                pl = json.loads(r[4]) if r[4] else None
            except (TypeError, ValueError):
                pl = None
            out.append({"seq": r[0], "role": r[1], "text": r[2],
                        "created_at": r[3], "payload": pl})
        return out

    async def delete_session(self, session_id: str) -> None:
        """Delete a session row + its chat_turns + its session_kv entries."""
        conn = await self._connected()
        await conn.execute("DELETE FROM chat_turns WHERE session_id = ?", (session_id,))
        await conn.execute("DELETE FROM session_kv WHERE session_id = ?", (session_id,))
        await conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await conn.commit()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store_turns_payload.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/local_store.py tests/services/orchestrator/test_local_store_turns_payload.py
git commit -m "feat(local-store): chat_turns payload column + rich-turn accessors + delete_session (T2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: LocalStore `loop_checkpoints` table + methods

**Files:**
- Modify: `services/orchestrator/local_store.py` (`_SCHEMA` + 3 methods)
- Test: `tests/services/orchestrator/test_local_store_checkpoints.py` (new)

**Interfaces:**
- Produces: `LocalStore.checkpoint_put(task_id:str, payload:dict) -> None` (upsert), `LocalStore.checkpoint_get(task_id:str) -> dict|None`, `LocalStore.checkpoint_delete(task_id:str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_local_store_checkpoints.py
import pytest
from services.orchestrator.local_store import LocalStore


@pytest.mark.asyncio
async def test_checkpoint_put_get_delete_upsert(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    assert await store.checkpoint_get("t1") is None
    await store.checkpoint_put("t1", {"turn": 1, "goal": "g"})
    assert await store.checkpoint_get("t1") == {"turn": 1, "goal": "g"}
    await store.checkpoint_put("t1", {"turn": 2, "goal": "g"})  # upsert
    assert (await store.checkpoint_get("t1"))["turn"] == 2
    await store.checkpoint_delete("t1")
    assert await store.checkpoint_get("t1") is None
    await store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store_checkpoints.py -q`
Expected: FAIL (`AttributeError: ... checkpoint_put`).

- [ ] **Step 3: Add the table to `_SCHEMA`**:

```sql
CREATE TABLE IF NOT EXISTS loop_checkpoints (
    task_id    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

- [ ] **Step 4: Add the methods**:

```python
    # ── inner-loop checkpoints (crash recovery) ──────────────────────────
    async def checkpoint_put(self, task_id: str, payload: dict) -> None:
        conn = await self._connected()
        await conn.execute(
            "INSERT OR REPLACE INTO loop_checkpoints (task_id, payload, updated_at)"
            " VALUES (?, ?, ?)",
            (task_id, json.dumps(payload), _iso_now()),
        )
        await conn.commit()

    async def checkpoint_get(self, task_id: str) -> dict | None:
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT payload FROM loop_checkpoints WHERE task_id = ?", (task_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return None

    async def checkpoint_delete(self, task_id: str) -> None:
        conn = await self._connected()
        await conn.execute("DELETE FROM loop_checkpoints WHERE task_id = ?", (task_id,))
        await conn.commit()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_store_checkpoints.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/local_store.py tests/services/orchestrator/test_local_store_checkpoints.py
git commit -m "feat(local-store): loop_checkpoints table + accessors (T3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `SqliteUserStore`

**Files:**
- Modify: `services/ws_gateway/user_store.py` (add `SqliteUserStore`; keep Protocol + `InMemoryUserStore`; `MongoUserStore` deleted in Task 10)
- Test: `tests/services/ws_gateway/test_sqlite_user_store.py` (new)

**Interfaces:**
- Consumes: `LocalStore.auth_user_*` (Task 1).
- Produces: `SqliteUserStore(store: LocalStore)` implementing `UserStore` (`find_by_email`, `create`, `count`).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/ws_gateway/test_sqlite_user_store.py
import pytest
from services.orchestrator.local_store import LocalStore
from services.ws_gateway.user_store import SqliteUserStore


@pytest.mark.asyncio
async def test_sqlite_user_store_create_find_count(tmp_path):
    us = SqliteUserStore(LocalStore(tmp_path / "state.db"))
    assert await us.count() == 0
    doc = await us.create(email="Admin@X.io", display_name="Admin",
                          password_hash="h", role="admin")
    assert doc["email"] == "admin@x.io" and doc["id"].startswith("u-")
    assert doc["role"] == "admin" and doc["displayName"] == "Admin"
    assert await us.count() == 1
    assert (await us.find_by_email("ADMIN@x.io"))["id"] == doc["id"]
    assert await us.find_by_email("nope@x.io") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/ws_gateway/test_sqlite_user_store.py -q`
Expected: FAIL (`ImportError: cannot import name 'SqliteUserStore'`).

- [ ] **Step 3: Add `SqliteUserStore`** to `services/ws_gateway/user_store.py` (after `MongoUserStore`; reuse the existing `import time, uuid`):

```python
class SqliteUserStore:
    """LocalStore(SQLite)-backed user store — the local-harness default."""

    def __init__(self, store) -> None:
        self._store = store  # services.orchestrator.local_store.LocalStore

    async def find_by_email(self, email: str) -> Optional[UserDoc]:
        return await self._store.auth_user_find_by_email(email)  # type: ignore[return-value]

    async def create(
        self, *, email: str, display_name: str, password_hash: str,
        role: Literal["admin", "user"] = "user",
    ) -> UserDoc:
        doc: UserDoc = {
            "id": "u-" + uuid.uuid4().hex[:12],
            "email": email.lower(),
            "displayName": display_name,
            "passwordHash": password_hash,
            "role": role,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        await self._store.auth_user_create(
            id=doc["id"], email=doc["email"], display_name=display_name,
            password_hash=password_hash, role=role, created_at=doc["createdAt"],
        )
        return doc

    async def count(self) -> int:
        return await self._store.auth_user_count()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/ws_gateway/test_sqlite_user_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/ws_gateway/user_store.py tests/services/ws_gateway/test_sqlite_user_store.py
git commit -m "feat(ws-gateway): SqliteUserStore over LocalStore auth_users (T4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `SqliteSessionStore`

**Files:**
- Create: `services/ws_gateway/sqlite_session_store.py`
- Test: `tests/services/ws_gateway/test_sqlite_session_store.py` (new)

**Interfaces:**
- Consumes: `LocalStore.record_session`, `list_sessions`, `session_kv_get/set`, `append_turn_payload`, `turns_with_payload`, `delete_session` (Tasks 2).
- Produces: `SqliteSessionStore(store: LocalStore, *, local_user: str = "u-local")` implementing the `InMemorySessionStore` interface (`create`, `list`, `get`, `rename`, `delete`, `turns`, `add_turn`, `set_debug`, `get_debug`). Session dict: `{id,title,mode,turnCount,contextTokens,createdAt,updatedAt}`. `turns()` returns each turn's stored `payload` dict verbatim (with `seq`).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/ws_gateway/test_sqlite_session_store.py
import pytest
from services.orchestrator.local_store import LocalStore
from services.ws_gateway.sqlite_session_store import SqliteSessionStore


@pytest.mark.asyncio
async def test_session_lifecycle_and_turn_payload_round_trip(tmp_path):
    ss = SqliteSessionStore(LocalStore(tmp_path / "state.db"))
    s = await ss.create(title="Hello", mode="chat", session_id="s1")
    assert s == {"id": "s1", "title": "Hello", "mode": "chat", "turnCount": 0,
                 "contextTokens": 0, "createdAt": s["createdAt"], "updatedAt": s["updatedAt"]}
    assert (await ss.get("s1"))["title"] == "Hello"
    assert [x["id"] for x in await ss.list()] == ["s1"]

    turn = {"id": "t-1", "sessionId": "s1", "role": "assistant", "text": "hi",
            "reasoning": {"text": "r"}, "toolCalls": [{"name": "x"}],
            "createdAt": "2026-07-04T00:00:00Z", "status": "complete"}
    await ss.add_turn("s1", turn)
    got = await ss.turns("s1")
    assert got[0]["reasoning"] == {"text": "r"} and got[0]["toolCalls"] == [{"name": "x"}]
    assert got[0]["id"] == "t-1" and got[0]["seq"] == 0
    assert (await ss.get("s1"))["turnCount"] == 1

    await ss.rename("s1", "Renamed")
    assert (await ss.get("s1"))["title"] == "Renamed"
    await ss.set_debug("s1", True)
    assert await ss.get_debug("s1") is True

    assert await ss.delete("s1") is True
    assert await ss.get("s1") is None
    assert await ss.delete("s1") is False


@pytest.mark.asyncio
async def test_turns_without_payload_reconstructed(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    ss = SqliteSessionStore(store)
    await ss.create(title="T", mode="chat", session_id="s1")
    await store.append_turn("s1", "user", "plain")  # orchestrator fallback writer
    got = await ss.turns("s1")
    assert got[0]["role"] == "user" and got[0]["text"] == "plain"
    assert got[0]["sessionId"] == "s1" and got[0]["seq"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/ws_gateway/test_sqlite_session_store.py -q`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Create `services/ws_gateway/sqlite_session_store.py`**:

```python
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

_KV_NS = "gw"


class SqliteSessionStore:
    """LocalStore(SQLite)-backed session store. chat_turns is the single turn
    store (frontend history + orchestrator continuity read the same rows).
    Session metadata (title/mode/debug) lives in session_kv under one JSON blob.
    """

    def __init__(self, store, *, local_user: str = "u-local") -> None:
        self._store = store
        self._user = local_user

    async def _meta(self, sid: str) -> dict:
        raw = await self._store.session_kv_get(_KV_NS, sid)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    async def _set_meta(self, sid: str, meta: dict) -> None:
        await self._store.session_kv_set(_KV_NS, sid, json.dumps(meta))

    async def _session_dict(self, sid: str, row: dict, meta: dict) -> dict:
        turns = await self._store.turns_with_payload(sid)
        return {
            "id": sid,
            "title": meta.get("title", ""),
            "mode": meta.get("mode", "chat"),
            "turnCount": len(turns),
            "contextTokens": meta.get("contextTokens", 0),
            "createdAt": row.get("created_at") or meta.get("createdAt", ""),
            "updatedAt": meta.get("updatedAt") or row.get("created_at", ""),
        }

    async def create(self, *, title: str, mode: str,
                     session_id: str | None = None, updated_at: str | None = None) -> dict:
        sid = session_id or "s-" + uuid.uuid4().hex[:12]
        now = updated_at or _now_iso()
        await self._store.record_session(sid, user_id=self._user, task_preview=title, created_at=now)
        await self._set_meta(sid, {"title": title, "mode": mode, "debug": False,
                                   "contextTokens": 0, "createdAt": now, "updatedAt": now})
        return {"id": sid, "title": title, "mode": mode, "turnCount": 0,
                "contextTokens": 0, "createdAt": now, "updatedAt": now}

    async def list(self) -> list[dict]:
        rows = await self._store.list_sessions(self._user, limit=200)
        out = [await self._session_dict(r["session_id"], r, await self._meta(r["session_id"]))
               for r in rows]
        return sorted(out, key=lambda s: s["updatedAt"], reverse=True)

    async def get(self, sid: str) -> dict | None:
        rows = await self._store.list_sessions(self._user, limit=1000)
        row = next((r for r in rows if r["session_id"] == sid), None)
        if row is None:
            return None
        return await self._session_dict(sid, row, await self._meta(sid))

    async def rename(self, sid: str, title: str) -> dict | None:
        meta = await self._meta(sid)
        if not meta:
            return None
        meta["title"] = title
        meta["updatedAt"] = _now_iso()
        await self._set_meta(sid, meta)
        return await self.get(sid)

    async def delete(self, sid: str) -> bool:
        existing = await self.get(sid)
        if existing is None:
            return False
        await self._store.delete_session(sid)
        return True

    async def turns(self, sid: str) -> list[dict]:
        out = []
        for r in await self._store.turns_with_payload(sid):
            if r["payload"] is not None:
                turn = dict(r["payload"])
                turn["seq"] = r["seq"]
            else:
                turn = {"id": f"{sid}-{r['seq']}", "sessionId": sid, "role": r["role"],
                        "text": r["text"], "createdAt": r["created_at"],
                        "status": "complete", "seq": r["seq"]}
            out.append(turn)
        return out

    async def add_turn(self, sid: str, turn: dict) -> None:
        await self._store.append_turn_payload(
            sid, role=turn.get("role", ""), text=turn.get("text", ""), payload=turn,
        )
        meta = await self._meta(sid)
        if meta:
            meta["updatedAt"] = _now_iso()
            await self._set_meta(sid, meta)

    async def set_debug(self, sid: str, enabled: bool) -> None:
        meta = await self._meta(sid)
        meta["debug"] = enabled
        await self._set_meta(sid, meta)

    async def get_debug(self, sid: str) -> bool:
        return bool((await self._meta(sid)).get("debug", False))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/ws_gateway/test_sqlite_session_store.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add services/ws_gateway/sqlite_session_store.py tests/services/ws_gateway/test_sqlite_session_store.py
git commit -m "feat(ws-gateway): SqliteSessionStore over LocalStore chat_turns (T5)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: `SignalRegistry` persistence-ownership flag

**Files:**
- Modify: `services/orchestrator/inproc_bus.py` (`SignalRegistry`)
- Test: `tests/services/orchestrator/test_inproc_bus.py` (append; file exists)

**Interfaces:**
- Produces: `SignalRegistry.mark_persistence_owned(task_id:str) -> None`, `SignalRegistry.is_persistence_owned(task_id:str) -> bool`. `clear_task` also clears the flag.

- [ ] **Step 1: Write the failing test** (append to `tests/services/orchestrator/test_inproc_bus.py`):

```python
def test_persistence_ownership_flag():
    from services.orchestrator.inproc_bus import SignalRegistry
    sig = SignalRegistry()
    assert sig.is_persistence_owned("t1") is False
    sig.mark_persistence_owned("t1")
    assert sig.is_persistence_owned("t1") is True
    sig.clear_task("t1")
    assert sig.is_persistence_owned("t1") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_inproc_bus.py::test_persistence_ownership_flag -q`
Expected: FAIL (`AttributeError: ... mark_persistence_owned`).

- [ ] **Step 3: Implement** in `services/orchestrator/inproc_bus.py` — `SignalRegistry.__init__` add `self._persist_owned: set[str] = set()`; add the two methods; and in `clear_task` add `self._persist_owned.discard(task_id)`:

```python
    def mark_persistence_owned(self, task_id: str) -> None:
        self._persist_owned.add(task_id)

    def is_persistence_owned(self, task_id: str) -> bool:
        return task_id in self._persist_owned
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_inproc_bus.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/inproc_bus.py tests/services/orchestrator/test_inproc_bus.py
git commit -m "feat(inproc-bus): per-task persistence-ownership flag on SignalRegistry (T6)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Writer coordination — gateway marks ownership, `_persist_turns` skips

**Files:**
- Modify: `services/ws_gateway/server.py` (`_handle_send` marks ownership after `submit_goal`)
- Modify: `services/orchestrator/main.py` (`_persist_turns` skips when relay-owned)
- Test: `tests/services/orchestrator/test_persist_turns_coordination.py` (new)

**Interfaces:**
- Consumes: `SignalRegistry.mark/is_persistence_owned` (Task 6).
- Produces: `_persist_turns` writes exactly when NOT relay-owned; the gateway (via `SqliteSessionStore.add_turn`, Task 5) is the writer when owned.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_persist_turns_coordination.py
import pytest
from services.orchestrator.inproc_bus import SignalRegistry
from services.orchestrator.local_store import LocalStore
from services.orchestrator.main import OrchestratorProcess


@pytest.mark.asyncio
async def test_persist_turns_skips_when_relay_owned(tmp_path, monkeypatch):
    proc = OrchestratorProcess()
    proc.signals = SignalRegistry()
    store = LocalStore(tmp_path / "state.db")

    class _Storage:
        local_store = store

    # relay owns persistence for this session → orchestrator must NOT write
    proc.signals.mark_persistence_owned("s-owned")
    await proc._persist_turns(_Storage(), "s-owned", "u", "a")
    assert await store.all_turns("s-owned") == []

    # not owned → orchestrator writes the fallback plain turns
    await proc._persist_turns(_Storage(), "s-free", "u", "a")
    assert [t["role"] for t in await store.all_turns("s-free")] == ["user", "assistant"]
    await store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_persist_turns_coordination.py -q`
Expected: FAIL (turns written for `s-owned` because the skip isn't implemented).

- [ ] **Step 3: Implement the skip** in `services/orchestrator/main.py::_persist_turns` — after the `if not session_id: return` guard, add:

```python
        signals = getattr(self, "signals", None)
        if signals is not None and signals.is_persistence_owned(session_id):
            return  # a WS relay owns persistence for this session (rich writer)
```

- [ ] **Step 4: Mark ownership in the gateway** — in `services/ws_gateway/server.py::_handle_send`, right after the goal is submitted (`submit_goal`) and only when a durable session store is in play, mark ownership. Locate the `submit_goal` call in `_handle_send` (it returns `task_id`/spawns the relay task); immediately after the session_id is known and a store exists, add:

```python
        # This relay is the rich turn-writer for this session; tell the
        # orchestrator's fallback writer (_persist_turns) to skip (hermes skip_db).
        sig = getattr(runtime, "signals", None)
        if sig is not None and session_id:
            sig.mark_persistence_owned(session_id)
```

(Place it where `runtime` and `session_id` are in scope, before/after `submit_goal` — ownership is idempotent, so exact ordering vs submit is not load-bearing.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_persist_turns_coordination.py tests/services/ws_gateway -q`
Expected: PASS (coordination test + ws_gateway suite green).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/main.py services/ws_gateway/server.py tests/services/orchestrator/test_persist_turns_coordination.py
git commit -m "feat: single turn-writer coordination (gateway owns, _persist_turns skips) (T7)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: CheckpointStore → SQLite

**Files:**
- Modify: `services/orchestrator/loop_checkpoint.py` (`CheckpointStore` over LocalStore)
- Modify: `services/orchestrator/main.py` (wire `CheckpointStore(_sm.local_store)`)
- Test: `tests/services/orchestrator/test_loop_checkpoint.py` (append; file exists)

**Interfaces:**
- Consumes: `LocalStore.checkpoint_put/get/delete` (Task 3).
- Produces: `CheckpointStore(store: LocalStore)` — same `save(cp)`/`load(task_id)`/`clear(task_id)` API, best-effort.

- [ ] **Step 1: Write the failing test** (append to `tests/services/orchestrator/test_loop_checkpoint.py`):

```python
@pytest.mark.asyncio
async def test_checkpoint_store_over_localstore(tmp_path):
    from services.orchestrator.local_store import LocalStore
    from services.orchestrator.loop_checkpoint import CheckpointStore, LoopCheckpoint
    store = LocalStore(tmp_path / "state.db")
    cs = CheckpointStore(store)
    cp = LoopCheckpoint(task_id="t1", goal="g", messages=[{"role": "user", "content": "x"}])
    await cs.save(cp)
    loaded = await cs.load("t1")
    assert loaded is not None and loaded.task_id == "t1" and loaded.goal == "g"
    await cs.clear("t1")
    assert await cs.load("t1") is None
    await store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_loop_checkpoint.py::test_checkpoint_store_over_localstore -q`
Expected: FAIL (`CheckpointStore` calls `self._col.update_one`, LocalStore has no such method).

- [ ] **Step 3: Reimplement `CheckpointStore`** in `services/orchestrator/loop_checkpoint.py` over LocalStore (keep `FakeCheckpointStore`, `to_dict`/`from_dict`, `LoopCheckpoint` unchanged; update the class docstring to say SQLite; `CHECKPOINT_COLLECTION` may be removed from `__all__` and the module — grep first, it was only used by StorageManager which Task 9 changes):

```python
class CheckpointStore:
    """Best-effort persistence of one inner-loop checkpoint per task_id, backed
    by the local SQLite LocalStore. EVERY method swallows + logs and never raises."""

    def __init__(self, store: Any) -> None:
        self._store = store  # services.orchestrator.local_store.LocalStore

    async def save(self, cp: LoopCheckpoint) -> None:
        try:
            await self._store.checkpoint_put(cp.task_id, to_dict(cp))
        except Exception as exc:
            _log.warning("checkpoint save failed for %s: %s", cp.task_id, exc)

    async def load(self, task_id: str) -> LoopCheckpoint | None:
        try:
            doc = await self._store.checkpoint_get(task_id)
        except Exception as exc:
            _log.warning("checkpoint load failed for %s: %s", task_id, exc)
            return None
        return from_dict(doc)

    async def clear(self, task_id: str) -> None:
        try:
            await self._store.checkpoint_delete(task_id)
        except Exception as exc:
            _log.warning("checkpoint clear failed for %s: %s", task_id, exc)
```

- [ ] **Step 4: Rewire `main.py`** — replace the checkpoint wiring block (`services/orchestrator/main.py`, the `CheckpointStore(_sm.loop_checkpoint_collection)` line) with:

```python
                async_orch.checkpoint_store = CheckpointStore(_sm.local_store)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_loop_checkpoint.py -q`
Expected: PASS (existing pure to_dict/from_dict + Fake tests + the new SQLite one).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/loop_checkpoint.py services/orchestrator/main.py tests/services/orchestrator/test_loop_checkpoint.py
git commit -m "feat(loop-checkpoint): back CheckpointStore with LocalStore SQLite (T8)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Strip Mongo from `StorageManager` + drop `ContextManager.mongo_db`

**Files:**
- Modify: `services/orchestrator/storage_manager.py`
- Modify: `services/memory/context_manager.py` (drop `mongo_db` param + `self.db`)
- Test: `tests/services/orchestrator/test_storage_manager.py` (adjust; file exists) + `tests/services/memory/` context tests

**Interfaces:**
- Produces: `StorageManager()` opens no Mongo; `context_manager`/`workspaces`/`local_store`/`search_turns` unchanged. `ContextManager.__init__` no longer takes `mongo_db`.

- [ ] **Step 1: Adjust/write the tests** — update any `test_storage_manager.py` test that asserts Mongo/`from_clients(mongo=...)`/`loop_checkpoint_collection`; add:

```python
@pytest.mark.asyncio
async def test_storage_manager_opens_without_mongo(monkeypatch, tmp_path):
    from services.orchestrator import storage_manager as sm_mod
    from services.orchestrator.local_store import LocalStore
    monkeypatch.setattr(sm_mod, "get_local_store", lambda: LocalStore(tmp_path / "state.db"))
    async with sm_mod.StorageManager() as sm:
        assert sm.local_store is not None
        assert sm.context_manager is not None  # constructs without mongo_db
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_storage_manager.py -q`
Expected: FAIL (constructor still builds `AsyncIOMotorClient`; `context_manager` passes `mongo_db`).

- [ ] **Step 3: Rewrite `storage_manager.py`** — remove `from motor.motor_asyncio import AsyncIOMotorClient`, `from .db_indexes import ensure_indexes`, `self._mongo`/`self._db`, `from_clients`, `loop_checkpoint_collection`, and the `DB_NAME`/`META` Mongo bits. `__init__` takes nothing Mongo. `__aenter__` returns self (optionally `await self.local_store.connect()`); `__aexit__` no-ops (or closes nothing). `context_manager` drops `mongo_db=self._db`:

```python
            self._context_manager = ContextManager(
                chroma_cols={},
                embedder=_embedder,
                local_store=self.local_store,
            )
```

- [ ] **Step 4: Update `ContextManager.__init__`** (`services/memory/context_manager.py`) — remove the `mongo_db` parameter and `self.db = mongo_db`. Keep `chroma_cols`, `embedder`, `budget`, `storage`, `local_store`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_storage_manager.py tests/services/memory -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/storage_manager.py services/memory/context_manager.py tests/services/orchestrator/test_storage_manager.py
git commit -m "refactor(storage): StorageManager is a pure LocalStore facade; drop ContextManager.mongo_db (T9)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: Flip gateway defaults to SQLite + delete Mongo classes + requirements

**Files:**
- Modify: `services/ws_gateway/server.py` (`_default_session_store`, `build_app` default `user_store`)
- Delete: `services/ws_gateway/mongo_session_store.py`, `services/orchestrator/db_indexes.py`
- Modify: `services/ws_gateway/user_store.py` (delete `MongoUserStore`)
- Modify: `requirements.txt` (remove `motor`, `pymongo`) + any service-local requirements referencing them
- Test: `tests/services/ws_gateway/test_server_defaults.py` (new) + existing server tests

**Interfaces:**
- Consumes: `SqliteUserStore` (T4), `SqliteSessionStore` (T5), `get_local_store` (LocalStore).
- Produces: `build_app` defaults to SQLite stores over the shared LocalStore; no Mongo import path remains.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/ws_gateway/test_server_defaults.py
def test_default_stores_are_sqlite(monkeypatch, tmp_path):
    from services.orchestrator.local_store import LocalStore
    from services.ws_gateway import server as srv
    from services.ws_gateway.sqlite_session_store import SqliteSessionStore
    from services.ws_gateway.user_store import SqliteUserStore
    monkeypatch.setattr(srv, "get_local_store", lambda: LocalStore(tmp_path / "s.db"), raising=False)
    cfg = srv.Config(jwt_secret="s", admin_email="a@b.c", admin_password="pw",
                     jwt_expiry_seconds=3600, cors_origins=(), mongo_url="")
    assert isinstance(srv._default_session_store(cfg), SqliteSessionStore)


def test_no_mongo_imports_remain():
    import subprocess
    out = subprocess.run(["grep", "-rn", "MongoUserStore\\|MongoSessionStore\\|db_indexes\\|motor\\|pymongo",
                          "services"], capture_output=True, text=True).stdout
    # only comments/docstrings allowed; no import or class references
    assert "import motor" not in out and "MongoSessionStore" not in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/ws_gateway/test_server_defaults.py -q`
Expected: FAIL (`_default_session_store` still returns Mongo/InMemory; Mongo refs present).

- [ ] **Step 3: Rewrite `_default_session_store`** (`services/ws_gateway/server.py`) to return the SQLite store (drop the pymongo ping + Mongo import + InMemory-on-failure):

```python
def _default_session_store(config: Config):
    """SQLite-backed session store over the shared LocalStore (local harness)."""
    from services.orchestrator.local_store import get_local_store
    from services.ws_gateway.sqlite_session_store import SqliteSessionStore
    return SqliteSessionStore(get_local_store())
```

- [ ] **Step 4: Flip the default `user_store`** in `build_app` — replace `from services.ws_gateway.user_store import MongoUserStore` and `user_store = user_store or MongoUserStore(config.mongo_url)` with:

```python
    from services.orchestrator.local_store import get_local_store
    from services.ws_gateway.user_store import SqliteUserStore
    user_store = user_store or SqliteUserStore(get_local_store())
```

- [ ] **Step 5: Delete Mongo code** — `git rm services/ws_gateway/mongo_session_store.py services/orchestrator/db_indexes.py`; delete the `MongoUserStore` class from `user_store.py`; remove `motor` + `pymongo` from `requirements.txt` (and grep for any other requirements file listing them). Grep the tree and delete/retarget any test that imported the deleted symbols.

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/ws_gateway tests/services/orchestrator tests/services/local -q`
Expected: PASS (all green).

- [ ] **Step 7: Commit**

```bash
git add -u services/ services/ws_gateway/server.py services/ws_gateway/user_store.py requirements.txt tests/services/ws_gateway/test_server_defaults.py
git commit -m "refactor(ws-gateway): default to SQLite stores; delete Mongo classes + motor/pymongo (T10)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
> NOTE: `git add -u services/` restages the deletions; verify `git status` shows ONLY intended paths (no config.ts / daemon.pid / frontend/.claude) before committing — if it does, unstage them with `git restore --staged <path>`.

---

### Task 11: Infra scripts — single-process launch, drop mongod/redis/chroma

**Files:**
- Modify: `infrastructure/local/start.sh`, `stop.sh`, `status.sh`, `install.sh`, `local.env`
- Modify: `docs/local-execution-prerequisites.md` (add ripgrep)

**Interfaces:** none (shell). Verified by shellcheck + structural review (no live boot — model off).

- [ ] **Step 1: `start.sh`** — replace the separate `python -m services.orchestrator.main` and `python -m services.ws_gateway.server` blocks with ONE:

```bash
# ─── Local harness (single process: gateway + orchestrator, one asyncio loop) ──
_local_alive() { [[ -f "$PIDS/local.pid" ]] && kill -0 "$(cat "$PIDS/local.pid")" 2>/dev/null; }
if _local_alive; then
  info "local harness already running (pid $(cat "$PIDS/local.pid"))"
else
  info "starting local harness (services.local.main) ..."
  nohup python -m services.local.main >"$LOGS/local.log" 2>&1 &
  echo $! >"$PIDS/local.pid"
  for i in $(seq 1 60); do
    _local_alive || { fail "local harness exited — see $LOGS/local.log"; }
    curl -fsS "http://127.0.0.1:${LOCAL_PORT:-8787}/healthz" >/dev/null 2>&1 && break
    sleep 1
  done
  pass "local harness running (pid $(cat "$PIDS/local.pid"))"
fi
```
Delete the mongod (replSet), Redis, and Chroma start blocks + their wait/health loops + the `$DATA/{mongo,redis,chroma}` `mkdir`. Keep the MCP-bridge `npm run build` step and the serve-model guidance. Remove the Discord connector block if present (deferred).

- [ ] **Step 2: `stop.sh` + `status.sh`** — stop/status the single `local.pid` (+ model server). Remove mongod/redis/chroma stop + health/status. E.g. in stop.sh replace the per-service kills with killing `local.pid`; in status.sh replace the mongod/redis/chroma/orchestrator/ws_gateway checks with a single `curl /healthz` + `local.pid` liveness.

- [ ] **Step 3: `install.sh`** — delete MongoDB/Redis/Chroma install + replica-set init steps. Keep Python deps, node/MCP-bridge build, model prerequisites. Add a ripgrep check/install (`command -v rg` → install hint per OS).

- [ ] **Step 4: `local.env`** — delete `MONGO_URI`, `MONGO_URL`, `CHROMA_HOST`, `CHROMA_PORT`, `CHROMA_URL`, `REDIS_URL`. Keep `GEMMA_BASE`/`QWEN_BASE`, `WORKSPACE_PATH`, `LABMATE_GATEWAY_URL`, `CORS_ORIGINS`, model/tokenizer paths, and add `LOCAL_HOST`/`LOCAL_PORT` if absent (defaults 127.0.0.1 / 8787). Update the header comment: single-process SQLite; no Mongo/Redis/Chroma. **Set the dev seed credentials** so the admin auto-seeds on first boot: `export ADMIN_EMAIL="${ADMIN_EMAIL:-zach.stallbohm@gmail.com}"` and `export ADMIN_PASSWORD="${ADMIN_PASSWORD:-labmate-dev}"` (dev-only throwaway creds; Piece 7 should prompt for real ones + document rotation). Add a comment that these are dev defaults.

- [ ] **Step 5: `docs/local-execution-prerequisites.md`** — add a ripgrep (`rg`) entry (used by `search_files`; Python fallback exists).

- [ ] **Step 6: Verify (structural, no live boot)**

Run:
```bash
shellcheck infrastructure/local/*.sh
grep -nEi "mongod|mongo_uri|redis|chroma|services\.orchestrator\.main|services\.ws_gateway\.server" infrastructure/local/*.sh infrastructure/local/local.env
```
Expected: shellcheck clean (no NEW warnings); the grep returns NO hits (single-process launch, no dead services).

- [ ] **Step 7: Commit**

```bash
git add infrastructure/local/start.sh infrastructure/local/stop.sh infrastructure/local/status.sh infrastructure/local/install.sh infrastructure/local/local.env docs/local-execution-prerequisites.md
git commit -m "chore(infra): launch services.local.main single process; drop mongod/redis/chroma (T11)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 12: Docs — local topology + stale-comment sweep

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `infrastructure/local/README.md`, `infrastructure/local/INSTALL.md`
- Modify: `eval/seq_ab/local_tool_responder.py` (docstring), `tests/services/orchestrator/test_tool_manifest.py:707` (comment) — Mongo/round-trip stale prose

**Interfaces:** none (docs).

- [ ] **Step 1: `README.md`** — rewrite the architecture diagram + "required services" to the local single-process SQLite topology: drop MongoDB/Redis/Chroma from MEMORY/queues, drop vLLM-only + RunPod-required framing (llama.cpp `serve-model.sh` + `services.local.main` + SQLite). Keep the Brain→Nervous System→Hands framing.

- [ ] **Step 2: `CLAUDE.md`** — update the Architecture Map (MongoDB/Chroma/Redis block → "SQLite LocalStore (sessions, turns, auth, checkpoints)"), the Service URLs section (drop MONGO_URI/CHROMA_URL/REDIS_URL; keep GEMMA_BASE), and the "Memory / queues" lines. Targeted edits only — leave the harness-robustness / agentic-fix-loop / eval sections intact.

- [ ] **Step 3: `infrastructure/local/{README,INSTALL}.md`** — rewrite the service list + start/stop instructions to `services.local.main` + serve-model; drop Mongo/Redis/Chroma provisioning and the Docker `lm-<name>` container language.

- [ ] **Step 4: Stale-comment sweep** — fix the Mongo/round-trip prose in `eval/seq_ab/local_tool_responder.py` (docstring) and the `request_local_tool`/Mongo comment at `tests/services/orchestrator/test_tool_manifest.py:707` to reflect direct local execution + SQLite.

- [ ] **Step 4b: Document the auth model** — in `infrastructure/local/INSTALL.md` (and a short note in `local.env` near ADMIN_EMAIL/ADMIN_PASSWORD), document: registration is CLOSED (no signup UI); the bootstrap **admin is auto-seeded on first boot** from `ADMIN_EMAIL`/`ADMIN_PASSWORD` (login is impossible if `ADMIN_PASSWORD` is unset — it must be set); **additional users** are created by an admin via `POST /auth/users` with the admin's Bearer token (admin-only, `services/ws_gateway/auth.py`), since the auth store now lives in the SQLite `auth_users` table. (A convenience add-user CLI / frontend affordance is deferred to Piece 7.)

- [ ] **Step 5: Verify**

Run:
```bash
grep -rnEi "mongodb|mongo_uri|:27017|redis|chroma|:6379|:8765" README.md CLAUDE.md infrastructure/local/README.md infrastructure/local/INSTALL.md
```
Expected: NO hits describing them as required local services (any remaining mention is explicitly historical/"removed").

- [ ] **Step 6: Commit**

```bash
git add README.md CLAUDE.md infrastructure/local/README.md infrastructure/local/INSTALL.md eval/seq_ab/local_tool_responder.py tests/services/orchestrator/test_tool_manifest.py
git commit -m "docs: local single-process SQLite topology; Mongo/Redis/Chroma stale-comment sweep (T12)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final Verification (after all tasks)

```bash
# CI-scope + touched suites all green
PYTHONPATH=. python -m pytest tests/services/orchestrator tests/services/ws_gateway tests/services/local tests/services/cli -q
# No Mongo anywhere in code (comments/docs may mention it as removed)
grep -rnEi "import motor|from motor|pymongo|MongoUserStore|MongoSessionStore|AsyncIOMotorClient|db_indexes" services | grep -v "#"
# Full-tree collection clean
PYTHONPATH=. python -m pytest tests --co -q 2>&1 | tail -2
```
Expected: all suites pass; grep returns nothing; tree collects with 0 errors. Then open the PR into `experimental` and gate merge on green CI (`gh pr checks`).

## Notes for the executor

- Tasks 1-3 are additive LocalStore schema (safe, independent). Tasks 4-5 build the stores. Task 6-7 wire the single-writer coordination. Task 8-10 remove Mongo. Tasks 11-12 are infra/docs. Order matters: do NOT start Task 10 (delete Mongo) before 4/5/8/9 (the SQLite replacements exist).
- After Task 10, the `mongo_url` Config field may be unused — leave it (harmless) unless a test requires its removal; Piece 7 can drop it.
- `get_local_store()` returns the process-wide singleton LocalStore, so the gateway stores + the orchestrator share ONE DB file (already WAL-shared with the LangGraph SqliteSaver).
