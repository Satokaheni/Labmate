from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

import redis.asyncio as aioredis
from motor.motor_asyncio import AsyncIOMotorClient

from .db_indexes import ensure_indexes
from .local_store import LocalStore, get_local_store
from .workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)

DB_NAME = "labmate"
META = "meta"
TASKS_STREAM = "tasks"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StorageManager:
    """MongoDB (source of truth) + Redis (cache/queue)."""

    def __init__(self) -> None:
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/labmate")
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

        self._mongo = AsyncIOMotorClient(mongo_uri)
        self._redis = aioredis.from_url(redis_url)
        self._db = self._mongo[DB_NAME]

    @classmethod
    def from_clients(cls, *, mongo, redis) -> StorageManager:
        """Build with injected clients (tests). Bypasses env/network setup."""
        self = cls.__new__(cls)
        self._mongo = mongo
        self._redis = redis
        self._db = mongo[DB_NAME]
        return self

    async def __aenter__(self) -> StorageManager:
        await ensure_indexes(self._db)
        return self

    async def __aexit__(self, *exc) -> None:
        """Close Redis/Mongo connections on exit."""
        await self._redis.aclose()
        self._mongo.close()

    @property
    def context_manager(self):
        """Lazy ContextManager wired to this StorageManager's LocalStore.

        chroma_cols is empty and Chroma was removed in Piece 3, so RAG retrieval
        is inert (hybrid_retrieve returns []). Continuity (recent turns + summary/
        anchor/watermark, all from the LocalStore's session_kv table) is what this
        provides.
        """
        if not hasattr(self, "_context_manager"):
            from services.memory.context_manager import ContextManager
            from services.memory.embedder import embed as _embed_fn

            _redis = self._redis

            async def _embedder(texts: list[str]) -> list[list[float]]:
                return await _embed_fn(texts, redis=_redis)

            self._context_manager = ContextManager(
                mongo_db=self._db,
                chroma_cols={},
                embedder=_embedder,
                local_store=self.local_store,
            )
        return self._context_manager

    @property
    def workspaces(self) -> WorkspaceManager:
        """Lazy WorkspaceManager sharing this StorageManager's LocalStore.

        Lazy (like local_store/context_manager) so merely constructing a
        StorageManager doesn't eagerly resolve/cache the process-wide LocalStore
        singleton — tests that never touch .workspaces or .local_store shouldn't
        pay for (or pollute) that side effect.
        """
        if getattr(self, "_workspaces", None) is None:
            self._workspaces = WorkspaceManager(self.local_store)
        return self._workspaces

    @property
    def local_store(self) -> LocalStore:
        """The local SQLite store (process-cached). Lazy — the connection opens
        on first store call."""
        if getattr(self, "_local_store", None) is None:
            self._local_store = get_local_store()
        return self._local_store

    @property
    def loop_checkpoint_collection(self):
        """The Motor collection holding inner-loop checkpoints (one per task_id).

        Used by CheckpointStore (services/orchestrator/loop_checkpoint.py) to
        persist the per-turn ReAct-loop snapshot. No outbox: these are transient
        crash-recovery records, cleared when a goal finishes.
        """
        from .loop_checkpoint import CHECKPOINT_COLLECTION

        return self._db[CHECKPOINT_COLLECTION]

    # --- full-text / regex search over raw transcript (session_search) ----------
    async def search_turns(
        self,
        query: str,
        top_k: int = 8,
        *,
        mode: str = "text",
        session_id: str | None = None,
    ) -> list[dict]:
        """Keyword/regex search over local chat turns.

        Returns ``[{"sessionId": ..., "seq": ..., "text": ...}]``. Empty query →
        ``[]``. ``mode="text"`` = case-insensitive substring; ``mode="regex"`` =
        Python regex. ``session_id`` scopes to one session. Best-effort (any error
        → ``[]``) — delegated to the local SQLite store.
        """
        return await self.local_store.search_turns(
            query, mode=mode, session_id=session_id, limit=top_k
        )

    # --- task queue (Redis Streams, rule #5) ----------------------------
    async def enqueue_task(self, stream: str, payload: dict) -> None:
        """XADD — never RPUSH. Values must be str/bytes for the stream."""
        await self._redis.xadd(stream, {"payload": json.dumps(payload)})
