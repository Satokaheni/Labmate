from __future__ import annotations
import asyncio
import json
import os
import time
import redis.asyncio as aioredis

GOALS_STREAM = "labmate:goals"
RESULT_PREFIX = "labmate:result:"


class LabmateRedisClient:
    def __init__(self, redis_url: str | None = None) -> None:
        url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis = aioredis.from_url(url, decode_responses=False)

    async def push_task(
        self,
        task_id: str,
        task: str,
        session_id: str,
        user_id: str = "",
        workspace_id: str = "",
    ) -> None:
        payload = json.dumps({
            "task_id": task_id,
            "task": task,
            "session_id": session_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
        })
        await self._redis.xadd(GOALS_STREAM, {"payload": payload})

    async def get_result(
        self,
        task_id: str,
        timeout: float = 300.0,
    ) -> dict:
        key = f"{RESULT_PREFIX}{task_id}"
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(key)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
                if msg and msg.get("type") == "message":
                    break
                await asyncio.sleep(0.1)
            else:
                return {"ok": False, "error": "timeout"}
        finally:
            await pubsub.unsubscribe(key)
            await pubsub.aclose()

        raw = await self._redis.get(key)
        if raw is None:
            return {"ok": False, "error": "result_missing"}
        return json.loads(raw)

    async def aclose(self) -> None:
        await self._redis.aclose()
