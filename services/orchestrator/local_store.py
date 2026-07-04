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
