from __future__ import annotations

import asyncio
import time
from motor.motor_asyncio import AsyncIOMotorClient
import chromadb
import redis.asyncio as aioredis
from bson import ObjectId

EMBED_MODEL = "BAAI/bge-small-en-v1.5"


class StorageManager:
    """Async context manager owning one shared client of each storage backend.

    Usage:
        async with StorageManager(mongo_uri, chroma_host, chroma_port, redis_url) as sm:
            msg_id = await sm.write_message(...)
    """

    def __init__(
        self,
        mongo_uri: str,
        chroma_host: str,
        chroma_port: int,
        redis_url: str,
    ) -> None:
        self._mongo_uri    = mongo_uri
        self._chroma_host  = chroma_host
        self._chroma_port  = chroma_port
        self._redis_url    = redis_url
        self._outbox_task: asyncio.Task | None = None
        self._outbox_ready: asyncio.Event = asyncio.Event()

    async def __aenter__(self) -> "StorageManager":
        self.mongo = AsyncIOMotorClient(
            self._mongo_uri,
            maxPoolSize=50,
            minPoolSize=5,
            waitQueueTimeoutMS=5_000,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
            socketTimeoutMS=30_000,
        )
        self.db = self.mongo.labmate

        self.chroma = await chromadb.AsyncHttpClient(
            host=self._chroma_host,
            port=self._chroma_port,
        )
        self.vectors: dict = {
            col: await self.chroma.get_or_create_collection(
                col,
                metadata={"embed_model": EMBED_MODEL, "hnsw:space": "cosine"},
            )
            for col in ("episodic", "semantic", "procedural", "code_symbols")
        }

        pool = aioredis.ConnectionPool.from_url(
            self._redis_url,
            max_connections=50,
            decode_responses=True,
        )
        self.redis = aioredis.Redis(connection_pool=pool)

        await self._ensure_indexes()
        self._outbox_task = asyncio.create_task(self._run_outbox())

        # Wait for the change-stream cursor to open (no race on first write)
        # Timeout guards against unreachable MongoDB or dead worker task
        try:
            await asyncio.wait_for(self._outbox_ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            # Cursor never opened — cancel the task and fail fast
            if self._outbox_task:
                self._outbox_task.cancel()
                try:
                    await self._outbox_task
                except asyncio.CancelledError:
                    pass
            # Clean up already-opened connections before raising (no __aexit__ on failure)
            if hasattr(self, "mongo"):
                self.mongo.close()
            if hasattr(self, "redis"):
                try:
                    await self.redis.aclose()
                except Exception:
                    pass
            raise RuntimeError(
                "MongoDB change-stream cursor did not open within 10s. "
                "Check MongoDB replica set connectivity and logs."
            )

        return self

    async def __aexit__(self, *_exc) -> None:
        if self._outbox_task:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
        self.mongo.close()
        await self.redis.aclose()

    async def _ensure_indexes(self) -> None:
        """Idempotent index creation — safe to call on every startup."""
        await self.db.messages.create_index(
            [("session_id", 1), ("seq", 1)], unique=True
        )
        await self.db.messages.create_index(
            [("session_id", 1), ("created_at", -1)]
        )
        await self.db.messages.create_index([("outbox.processed", 1)])
        await self.db.tool_calls.create_index(
            [("session_id", 1), ("message_seq", 1)]
        )
        await self.db.tool_calls.create_index([("outbox.processed", 1)])
        await self.db.sessions.create_index("expire_at", expireAfterSeconds=0)

    async def write_message(
        self,
        session_id: str,
        seq: int,
        role: str,
        content: str,
        embedding: list[float],
        importance: float = 0.5,
    ) -> ObjectId:
        """Insert message + outbox marker in ONE atomic MongoDB write.

        The outbox worker picks up the marker and projects the vector to Chroma.
        Cross-store atomicity is guaranteed by this single-document write.
        """
        _id = ObjectId()
        await self.db.messages.insert_one({
            "_id":        _id,
            "session_id": session_id,
            "seq":        seq,
            "role":       role,
            "content":    content,
            "created_at": time.time(),
            "importance": importance,
            "outbox": {
                "kind":         "vector",
                "embedding":    embedding,
                "processed":    False,
                "processed_at": None,
            },
        })
        return _id

    async def search_memory(
        self,
        query_embedding: list[float],
        collection: str = "semantic",
        k: int = 10,
        where: dict | None = None,
    ) -> list[dict]:
        """Vector search in Chroma, resolved to full MongoDB records.

        MongoDB _id IS the Chroma point ID — results always consistent with
        source of truth. Orphan vectors (stale Chroma points with no matching
        MongoDB doc) are silently dropped.
        """
        col = self.vectors[collection]
        kwargs: dict = dict(
            query_embeddings=[query_embedding],
            n_results=k,
            include=["metadatas", "distances"],
        )
        if where:
            kwargs["where"] = where
        res = await col.query(**kwargs)

        chroma_ids = res["ids"][0]
        if not chroma_ids:
            return []

        object_ids = [ObjectId(cid) for cid in chroma_ids]
        cursor = self.db.messages.find({"_id": {"$in": object_ids}})
        docs = {doc["_id"]: doc async for doc in cursor}

        results = []
        for cid, dist in zip(chroma_ids, res["distances"][0]):
            oid = ObjectId(cid)
            if oid not in docs:
                continue  # orphan vector — skip
            doc = docs[oid]
            doc["_similarity"] = 1.0 - dist
            results.append(doc)
        return results

    async def enqueue_task(
        self,
        stream: str,
        fields: dict,
        maxlen: int = 10_000,
    ) -> str:
        """Enqueue a task onto a Redis Stream. Returns the entry ID.

        Always use Streams (XADD/XREADGROUP/XACK), never LIST+BRPOP.
        Unacked entries survive crashes and are recovered via XAUTOCLAIM.
        """
        return await self.redis.xadd(stream, fields, maxlen=maxlen, approximate=True)

    async def _run_outbox(self) -> None:
        """Tail the MongoDB change stream; project unprocessed outbox entries to
        Chroma and Redis. Persists resume token after each event so restarts never
        drop events that occurred during downtime."""
        tok_doc = await self.db.meta.find_one({"_id": "outbox_token"})
        resume_token = tok_doc["token"] if tok_doc else None

        pipeline = [{"$match": {
            "operationType": "insert",
            "fullDocument.outbox.processed": False,
        }}]

        watch_kwargs: dict = dict(
            pipeline=pipeline,
            full_document="updateLookup",
        )
        if resume_token:
            watch_kwargs["resume_after"] = resume_token

        try:
            async with self.db.messages.watch(**watch_kwargs) as stream:
                # Signal that the cursor is now open and listening
                self._outbox_ready.set()

                async for change in stream:
                    doc  = change["fullDocument"]
                    _id  = doc["_id"]
                    emb  = doc["outbox"]["embedding"]

                    # Idempotent upsert: MongoDB _id IS the Chroma point ID
                    await self.vectors["episodic"].upsert(
                        ids=[str(_id)],
                        embeddings=[emb],
                        documents=[doc["content"]],
                        metadatas=[{
                            "session_id": doc["session_id"],
                            "seq":        doc["seq"],
                            "embed_model": EMBED_MODEL,
                            "importance": doc.get("importance", 0.5),
                        }],
                    )

                    await self.enqueue_task(
                        "consolidate",
                        {"msg_id": str(_id), "session_id": doc["session_id"]},
                    )

                    await self.db.messages.update_one(
                        {"_id": _id},
                        {"$set": {
                            "outbox.processed":    True,
                            "outbox.processed_at": time.time(),
                        }},
                    )

                    # Persist resume token AFTER successful processing
                    await self.db.meta.update_one(
                        {"_id": "outbox_token"},
                        {"$set": {"token": change["_id"]}},
                        upsert=True,
                    )
        except asyncio.CancelledError:
            return
