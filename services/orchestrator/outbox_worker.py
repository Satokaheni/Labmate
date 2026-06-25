from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Mongo outbox.kind -> Chroma collection name
_KIND_TO_COLLECTION = {
    "episode_vector": "episodic",
    "memory_vector": "semantic",
}


class OutboxWorker:
    """Reads unprocessed outbox markers from MongoDB and projects to Chroma+Redis.

    The ONLY component that writes projected vectors to Chroma and pushes the
    tasks stream. Idempotent: Chroma point id == Mongo _id, so retries upsert
    the same row.
    """

    def __init__(self, storage, poll_interval: float = 1.0) -> None:
        self._s = storage
        self._poll = poll_interval
        self._running = False

    async def process_once(self, limit: int = 100) -> int:
        """Project one batch of unprocessed outbox docs. Returns count handled."""
        handled = 0
        chroma = await self._s._get_chroma()
        for coll_name in (self._s._db["episodes"], self._s._db["memories"]):
            cursor = coll_name.find({"outbox.processed": False}).limit(limit)
            async for doc in cursor:
                kind = doc["outbox"]["kind"]
                target = _KIND_TO_COLLECTION[kind]
                col = await chroma.get_or_create_collection(target)
                text = doc.get("fact") or doc.get("content") or ""
                meta = {
                    "session_id": doc.get("session_id"),
                    "valid_to": (doc.get("valid_to").isoformat()
                                 if isinstance(doc.get("valid_to"), datetime)
                                 else doc.get("valid_to")),
                    "source": doc.get("source"),
                    "importance": doc.get("importance"),
                }
                meta = {k: v for k, v in meta.items() if v is not None}
                await col.upsert(
                    ids=[str(doc["_id"])],
                    documents=[text],
                    metadatas=[meta],
                )
                await self._s._redis.xadd(
                    "tasks",
                    {
                        "payload": json.dumps({
                            "kind": kind,
                            "id": str(doc["_id"]),
                            "session_id": doc.get("session_id"),
                        }),
                    },
                )
                await coll_name.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {
                        "outbox.processed": True,
                        "outbox.processed_at": datetime.now(timezone.utc),
                    }},
                )
                handled += 1
        return handled

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.process_once()
            except Exception:  # pragma: no cover - defensive loop
                logger.exception("outbox projection batch failed")
            await asyncio.sleep(self._poll)

    def stop(self) -> None:
        self._running = False
