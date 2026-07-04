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

import json
import logging
import os
import re
import weakref
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from .local_mode import local_state_db_path

logger = logging.getLogger(__name__)

# Every connected LocalStore registers here so the harness can close them all on
# shutdown (and tests can drain them between cases — an unclosed aiosqlite
# connection outliving its event loop raises "Event loop is closed" in its worker
# thread). WeakSet: a garbage-collected store drops out on its own.
_LIVE_STORES: weakref.WeakSet = weakref.WeakSet()


async def close_all_local_stores() -> None:
    """Close every open LocalStore connection (graceful shutdown / test teardown)."""
    for store in list(_LIVE_STORES):
        try:
            await store.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT,
    workspace_id TEXT,
    task_preview TEXT,
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    ok           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_created ON sessions (user_id, created_at);
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    user_id      TEXT,
    name         TEXT,
    paths        TEXT,
    sources      TEXT,
    description  TEXT,
    instructions TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT,
    created_at   TEXT,
    last_active  TEXT
);
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
        # WAL + a busy timeout so this aiosqlite connection coexists with the
        # separate sync sqlite3 connection the LangGraph SqliteSaver holds on the
        # SAME file: WAL lets a reader and a writer proceed concurrently, and the
        # busy timeout absorbs the brief exclusive lock a checkpoint write takes
        # instead of raising SQLITE_BUSY.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        self._conn = conn
        _LIVE_STORES.add(self)

    async def _connected(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.connect()
        assert self._conn is not None
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

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
                # Literal-substring match: escape the LIKE metacharacters (\, %, _)
                # so a query like "50%" matches "50%" literally, not "50<anything>".
                # The ESCAPE '\' clause below makes '\' the escape character.
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                like = f"%{escaped}%"
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

    # ── sessions ─────────────────────────────────────────────────────────
    async def record_session(
        self,
        session_id: str,
        *,
        user_id: str = "",
        workspace_id: str = "",
        task_preview: str = "",
        created_at: str | None = None,
    ) -> None:
        """Insert (or replace) a session metadata row."""
        conn = await self._connected()
        await conn.execute(
            "INSERT OR REPLACE INTO sessions"
            " (session_id, user_id, workspace_id, task_preview, created_at, completed_at, ok)"
            " VALUES (?, ?, ?, ?, ?,"
            "   (SELECT completed_at FROM sessions WHERE session_id = ?),"
            "   (SELECT ok FROM sessions WHERE session_id = ?))",
            (
                session_id,
                user_id,
                workspace_id,
                task_preview,
                created_at or _iso_now(),
                session_id,
                session_id,
            ),
        )
        await conn.commit()

    async def complete_session(self, session_id: str, ok: bool = True) -> None:
        conn = await self._connected()
        await conn.execute(
            "UPDATE sessions SET completed_at = ?, ok = ? WHERE session_id = ?",
            (_iso_now(), 1 if ok else 0, session_id),
        )
        await conn.commit()

    async def list_sessions(
        self, user_id: str, *, workspace_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        """Newest-first session rows for a user (optionally scoped to a workspace)."""
        conn = await self._connected()
        if workspace_id:
            cur = await conn.execute(
                "SELECT session_id, user_id, workspace_id, task_preview, created_at, completed_at, ok"
                " FROM sessions WHERE user_id = ? AND workspace_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (user_id, workspace_id, limit),
            )
        else:
            cur = await conn.execute(
                "SELECT session_id, user_id, workspace_id, task_preview, created_at, completed_at, ok"
                " FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
        rows = await cur.fetchall()
        return [
            {
                "session_id": r[0],
                "user_id": r[1],
                "workspace_id": r[2],
                "task_preview": r[3],
                "created_at": r[4],
                "completed_at": r[5],
                "ok": (None if r[6] is None else bool(r[6])),
            }
            for r in rows
        ]

    # ── workspaces ───────────────────────────────────────────────────────
    async def upsert_workspace(self, workspace_id: str, user_id: str) -> None:
        """Insert a workspace on first sight; no-op if it already exists."""
        conn = await self._connected()
        now = _iso_now()
        await conn.execute(
            "INSERT OR IGNORE INTO workspaces"
            " (workspace_id, user_id, name, paths, sources, description, instructions, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                user_id,
                f"workspace-{workspace_id[:8]}",
                json.dumps([]),
                json.dumps([]),
                None,
                None,
                now,
                now,
            ),
        )
        await conn.commit()

    async def get_workspace(self, workspace_id: str) -> dict | None:
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT workspace_id, user_id, name, paths, sources, description, instructions,"
            " created_at, updated_at FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        r = await cur.fetchone()
        if r is None:
            return None
        return {
            "workspace_id": r[0],
            "user_id": r[1],
            "name": r[2],
            "paths": json.loads(r[3]) if r[3] else [],
            "sources": json.loads(r[4]) if r[4] else [],
            "description": r[5],
            "instructions": r[6],
            "created_at": r[7],
            "updated_at": r[8],
        }

    async def create_workspace(
        self,
        workspace_id: str,
        user_id: str,
        *,
        name: str,
        paths: list[str] | None = None,
        sources: list[str] | None = None,
        description: str | None = None,
        instructions: str | None = None,
    ) -> None:
        """Insert a fully-specified workspace (INSERT OR REPLACE)."""
        conn = await self._connected()
        now = _iso_now()
        await conn.execute(
            "INSERT OR REPLACE INTO workspaces"
            " (workspace_id, user_id, name, paths, sources, description, instructions, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?,"
            "   COALESCE((SELECT created_at FROM workspaces WHERE workspace_id = ?), ?), ?)",
            (
                workspace_id,
                user_id,
                name,
                json.dumps(paths or []),
                json.dumps(sources or []),
                description,
                instructions,
                workspace_id,
                now,
                now,
            ),
        )
        await conn.commit()

    async def list_workspaces(self, user_id: str, *, limit: int = 100) -> list[dict]:
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT workspace_id, user_id, name, paths, sources, description, instructions,"
            " created_at, updated_at FROM workspaces WHERE user_id = ?"
            " ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cur.fetchall()
        return [
            {
                "workspace_id": r[0],
                "user_id": r[1],
                "name": r[2],
                "paths": json.loads(r[3]) if r[3] else [],
                "sources": json.loads(r[4]) if r[4] else [],
                "description": r[5],
                "instructions": r[6],
                "created_at": r[7],
                "updated_at": r[8],
            }
            for r in rows
        ]

    async def update_workspace(self, workspace_id: str, **fields) -> None:
        """Set arbitrary workspace columns (immutable: workspace_id/user_id/created_at).
        paths/sources are JSON-encoded if passed as lists. Always bumps updated_at."""
        immutable = {"workspace_id", "user_id", "created_at"}
        cols, vals = [], []
        for k, v in fields.items():
            if k in immutable:
                continue
            if k in ("paths", "sources") and isinstance(v, list):
                v = json.dumps(v)
            cols.append(f"{k} = ?")
            vals.append(v)
        cols.append("updated_at = ?")
        vals.append(_iso_now())
        if not cols:
            return
        vals.append(workspace_id)
        conn = await self._connected()
        await conn.execute(f"UPDATE workspaces SET {', '.join(cols)} WHERE workspace_id = ?", vals)
        await conn.commit()

    # ── users ────────────────────────────────────────────────────────────
    async def upsert_user(self, user_id: str, display_name: str = "") -> None:
        conn = await self._connected()
        now = _iso_now()
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id, display_name, created_at, last_active)"
            " VALUES (?, ?, ?, ?)",
            (user_id, display_name, now, now),
        )
        await conn.commit()

    async def get_user(self, user_id: str) -> dict | None:
        conn = await self._connected()
        cur = await conn.execute(
            "SELECT user_id, display_name, created_at, last_active FROM users WHERE user_id = ?",
            (user_id,),
        )
        r = await cur.fetchone()
        if r is None:
            return None
        return {"user_id": r[0], "display_name": r[1], "created_at": r[2], "last_active": r[3]}

    async def touch_user(self, user_id: str) -> None:
        conn = await self._connected()
        await conn.execute(
            "UPDATE users SET last_active = ? WHERE user_id = ?", (_iso_now(), user_id)
        )
        await conn.commit()


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
