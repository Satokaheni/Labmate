"""Create MongoDB indexes on first run. Safe to call repeatedly (idempotent)."""
from __future__ import annotations
import logging
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db["users"].create_index("user_id", unique=True)
    await db["workspaces"].create_index("workspace_id", unique=True)
    await db["workspaces"].create_index([("user_id", 1), ("name", 1)])
    await db["sessions"].create_index("session_id", unique=True)
    await db["sessions"].create_index([("user_id", 1), ("workspace_id", 1), ("created_at", -1)])
    await db["episodes"].create_index([("session_id", 1), ("seq", 1)])
    await db["memories"].create_index([("session_id", 1), ("valid_to", 1)])
    await db["memories"].create_index([("session_id", 1), ("valid_to", 1), ("expires_at", 1)])
    logger.info("MongoDB indexes ensured")
