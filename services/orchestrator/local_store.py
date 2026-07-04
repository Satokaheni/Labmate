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
import re
from datetime import UTC, datetime
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
