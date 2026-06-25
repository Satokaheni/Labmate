from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import chromadb
import redis.asyncio as aioredis
from motor.motor_asyncio import AsyncIOMotorClient

from .db_indexes import ensure_indexes
from .workspace_manager import WorkspaceManager
from .memory_consolidator import MemoryConsolidator

logger = logging.getLogger(__name__)

DB_NAME = "labmate"
EPISODES = "episodes"
MEMORIES = "memories"
META = "meta"
TASKS_STREAM = "tasks"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Importance -> days-until-expiry. importance >= 5 never expires.
_TTL_DAYS = {1: 30, 2: 90, 3: 365, 4: 1095}


def _expires_at(importance, now: datetime | None = None) -> datetime | None:
    """Compute expiry from importance. Returns None for never-expiring (>=5)."""
    try:
        imp = int(round(float(importance)))
    except (TypeError, ValueError):
        imp = 3
    days = _TTL_DAYS.get(imp)
    if days is None:  # importance >= 5 (or <1 clamped); 5 = permanent
        if imp >= 5:
            return None
        days = _TTL_DAYS[1]  # clamp anything below 1 to the shortest TTL
    return (now or _utcnow()) + timedelta(days=days)


class StorageManager:
    """MongoDB (source of truth) + Chroma (vectors) + Redis (cache/queue).

    All cross-store writes go through MongoDB with an outbox marker. The
    OutboxWorker is the ONLY writer to Chroma and the tasks Redis stream
    for projected records.
    """

    def __init__(self) -> None:
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/labmate")
        chroma_url = os.getenv("CHROMA_URL", "http://chroma:8000")
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

        parsed = urlparse(chroma_url)
        chroma_host = parsed.hostname or "chroma"
        chroma_port = parsed.port or 8000

        self._mongo = AsyncIOMotorClient(mongo_uri)
        # NOTE: AsyncHttpClient is a coroutine factory in newer chromadb; we
        # build a thin lazy wrapper. Here we hold the call args and create on
        # first use to avoid awaiting in __init__.
        self._chroma_args = {"host": chroma_host, "port": int(chroma_port)}
        self._chroma = None  # set lazily via _get_chroma()
        self._redis = aioredis.from_url(redis_url)
        self._db = self._mongo[DB_NAME]
        self._workspaces = WorkspaceManager(self._db)

    @classmethod
    def from_clients(cls, *, mongo, chroma, redis) -> "StorageManager":
        """Build with injected clients (tests). Bypasses env/network setup."""
        self = cls.__new__(cls)
        self._mongo = mongo
        self._chroma = chroma
        self._chroma_args = {}
        self._redis = redis
        self._db = mongo[DB_NAME]
        self._workspaces = WorkspaceManager(self._db)
        return self

    async def __aenter__(self) -> "StorageManager":
        """Start the OutboxWorker background task when entering the context."""
        await ensure_indexes(self._db)
        from .outbox_worker import OutboxWorker
        self._outbox_worker = OutboxWorker(self)
        self._outbox_task = asyncio.create_task(self._outbox_worker.run(), name="outbox-worker")
        return self

    async def __aexit__(self, *exc) -> None:
        """Cancel the OutboxWorker task and close Redis/Mongo connections on exit."""
        if hasattr(self, "_outbox_task"):
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
        await self._redis.aclose()
        self._mongo.close()

    @property
    def consolidator(self) -> MemoryConsolidator:
        if not hasattr(self, "_consolidator"):
            self._consolidator = MemoryConsolidator(
                storage=self,
                lm_base_url=os.getenv("GEMMA_BASE", "http://localhost:8000/v1"),
            )
        return self._consolidator

    @property
    def workspaces(self) -> WorkspaceManager:
        return self._workspaces

    async def _get_chroma(self):
        if self._chroma is None:
            self._chroma = await chromadb.AsyncHttpClient(**self._chroma_args)
        return self._chroma

    # --- episodic write (transactional outbox) ---------------------------
    async def store_episode(self, session_id: str, content: str, metadata: dict) -> str:
        """Insert one episode + outbox marker in a SINGLE MongoDB write.

        Does NOT touch Chroma or Redis — the OutboxWorker projects later.
        """
        seq = await self._db[EPISODES].count_documents({"session_id": session_id})
        doc = {
            "session_id": session_id,
            "seq": seq,
            "content": content,
            "metadata": metadata or {},
            "created_at": _utcnow(),
            "outbox": {
                "kind": "episode_vector",
                "processed": False,
                "processed_at": None,
            },
        }
        res = await self._db[EPISODES].insert_one(doc)
        logger.debug("stored episode %s seq=%s", res.inserted_id, seq)
        return str(res.inserted_id)

    # --- semantic memory write (transactional outbox) -------------------
    async def store_memory(self, memory: dict) -> str:
        """Insert one semantic fact + outbox marker in a single Mongo write.

        memory: {session_id, fact, valid_from?, valid_to?, supersedes?}
        """
        doc = {
            "session_id": memory["session_id"],
            "fact": memory["fact"],
            "embedding_text": memory.get("embedding_text", memory["fact"]),
            "importance": memory.get("importance", 3),
            "source": memory.get("source", "agent_generated"),
            "valid_from": memory.get("valid_from") or _utcnow(),
            "valid_to": memory.get("valid_to"),
            "supersedes": memory.get("supersedes"),
            "created_at": _utcnow(),
            "expires_at": _expires_at(memory.get("importance", 3)),
            "outbox": {
                "kind": "memory_vector",
                "processed": False,
                "processed_at": None,
            },
        }
        res = await self._db[MEMORIES].insert_one(doc)
        return str(res.inserted_id)

    async def close_memory(self, memory_id: str, valid_to=None) -> None:
        """Temporal close (Zep): set valid_to so the fact is no longer current.

        Re-projects (re-opens outbox) so the OutboxWorker updates Chroma metadata.
        """
        from bson import ObjectId
        await self._db[MEMORIES].update_one(
            {"_id": ObjectId(memory_id)},
            {"$set": {
                "valid_to": valid_to or _utcnow(),
                "outbox.processed": False,
                "outbox.processed_at": None,
            }},
        )

    async def boost_memory_importance(self, memory_id: str, delta: float = 0.1) -> None:
        """Increment a memory's importance (capped at 5.0) and re-project to Chroma.

        Called when a memory is retrieved into context: frequently-used memories
        become more durable. Best-effort — bad/missing ids are ignored. Re-opens
        the outbox so the OutboxWorker refreshes Chroma metadata + the TTL on the
        next sweep (see decay task).
        """
        from bson import ObjectId
        try:
            oid = ObjectId(memory_id)
        except Exception:
            return
        doc = await self._db[MEMORIES].find_one({"_id": oid}, {"importance": 1})
        if not doc:
            return
        current = doc.get("importance", 3)
        try:
            new_importance = min(5.0, float(current) + float(delta))
        except (TypeError, ValueError):
            return
        await self._db[MEMORIES].update_one(
            {"_id": oid},
            {"$set": {
                "importance": new_importance,
                "outbox.processed": False,
                "outbox.processed_at": None,
            }},
        )

    async def decay_expired_memories(self, session_id: str, now: datetime | None = None) -> int:
        """Close all currently-valid memories for a session whose expires_at is past.

        Returns the number of memories closed. Calls close_memory() per hit so the
        outbox re-projection marks them closed in Chroma. Idempotent: already-closed
        memories (valid_to set) are excluded by the query.
        """
        cutoff = now or _utcnow()
        cursor = self._db[MEMORIES].find(
            {
                "session_id": session_id,
                "valid_to": None,
                "expires_at": {"$ne": None, "$lte": cutoff},
            },
            {"_id": 1},
        )
        ids = [doc["_id"] async for doc in cursor]
        for _id in ids:
            await self.close_memory(str(_id), valid_to=cutoff)
        if ids:
            logger.info("decayed %d expired memories for session=%s", len(ids), session_id)
        return len(ids)

    # --- search ----------------------------------------------------------
    async def search_memories(self, query: str, top_k: int = 5) -> list[dict]:
        """Vector search over the `semantic` Chroma collection.

        Returns only currently-valid facts (valid_to is None) ranked by Chroma.
        """
        chroma = await self._get_chroma()
        col = await chroma.get_or_create_collection("semantic")
        res = await col.query(query_texts=[query], n_results=top_k)
        out: list[dict] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, _id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            if meta.get("valid_to"):  # skip closed facts
                continue
            src = meta.get("source")
            fact_text = docs[i] if i < len(docs) else ""
            display = f"[{src}] {fact_text}" if src else fact_text
            out.append({
                "id": _id,
                "fact": display,
                "raw_fact": fact_text,
                "metadata": meta,
                "distance": dists[i] if i < len(dists) else None,
            })
        return out

    # --- working cache (Redis KV) ---------------------------------------
    async def cache_set(self, key: str, value: str, ttl: int = 3600) -> None:
        await self._redis.set(f"cache:{key}", value, ex=ttl)

    async def cache_get(self, key: str) -> str | None:
        v = await self._redis.get(f"cache:{key}")
        if v is None:
            return None
        return v.decode() if isinstance(v, (bytes, bytearray)) else v

    # --- task queue (Redis Streams, rule #5) ----------------------------
    async def enqueue_task(self, stream: str, payload: dict) -> None:
        """XADD — never RPUSH. Values must be str/bytes for the stream."""
        await self._redis.xadd(stream, {"payload": json.dumps(payload)})
