"""Motor-backed persistent session store for chat sessions and turns."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class MongoSessionStore:
    """Production Motor-backed session store with durable Mongo persistence.

    Collections: chat_sessions (session docs) and chat_turns (turn docs).
    All methods are async and strip the MongoDB _id field from returned dicts.
    """

    def __init__(self, mongo_url: str, db_name: str = "labmate") -> None:
        """Initialize Motor client and get collection references.

        Args:
            mongo_url: MongoDB connection string (e.g., "mongodb://localhost:27017")
            db_name: Database name (default "labmate")
        """
        import motor.motor_asyncio  # Lazy import so tests don't need Motor

        self._client = motor.motor_asyncio.AsyncIOMotorClient(mongo_url)
        db = self._client[db_name]
        self._sessions = db["chat_sessions"]
        self._turns = db["chat_turns"]

        # Best-effort index creation (wrap in try/except; may fail if Mongo is down at init)
        self._ensure_indexes_scheduled = False

    async def _ensure_indexes(self) -> None:
        """Create indexes lazily on first use. Best-effort; failures are logged but not fatal."""
        if self._ensure_indexes_scheduled:
            return
        self._ensure_indexes_scheduled = True

        try:
            # Unique index on session id
            await self._sessions.create_index("id", unique=True)
            # Compound index on (sessionId, createdAt) for efficient turns queries
            await self._turns.create_index([("sessionId", 1), ("createdAt", 1)])
        except Exception as e:
            logger.warning(f"Failed to create indexes: {e}")

    async def create(
        self,
        *,
        title: str,
        mode: str,
        session_id: str | None = None,
        updated_at: str | None = None,
    ) -> dict:
        """Create a new session document.

        Args:
            title: Session title
            mode: Session mode (e.g., "chat", "code")
            session_id: Optional explicit session ID (auto-generated if omitted)
            updated_at: Optional explicit updatedAt timestamp (defaults to now)

        Returns:
            Session dict without the MongoDB _id field
        """
        await self._ensure_indexes()

        sid = session_id or "s-" + uuid.uuid4().hex[:12]
        now = updated_at or _now_iso()

        doc = {
            "id": sid,
            "title": title,
            "mode": mode,
            "turnCount": 0,
            "contextTokens": 0,
            "createdAt": now,
            "updatedAt": now,
            "debug": False,
        }

        try:
            await self._sessions.insert_one(doc)
        except Exception as e:
            logger.warning("Failed to insert session document: %s; returning unsaved session", e)

        # Return doc regardless of whether insert succeeded (best-effort)
        return {k: v for k, v in doc.items() if k != "_id"}

    async def list(self) -> list[dict]:
        """List all sessions sorted by updatedAt descending (most recent first).

        Returns:
            List of session dicts (without _id)
        """
        await self._ensure_indexes()

        try:
            cursor = self._sessions.find().sort("updatedAt", -1)
            sessions = []
            async for doc in cursor:
                sessions.append({k: v for k, v in doc.items() if k != "_id"})
            return sessions
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            return []

    async def get(self, sid: str) -> dict | None:
        """Retrieve a session by ID.

        Args:
            sid: Session ID

        Returns:
            Session dict (without _id) or None if not found
        """
        await self._ensure_indexes()

        try:
            doc = await self._sessions.find_one({"id": sid}, {"_id": 0})
            return doc  # type: ignore[return-value]
        except Exception as e:
            logger.error(f"Failed to get session {sid}: {e}")
            return None

    async def rename(self, sid: str, title: str) -> dict | None:
        """Rename a session and update its updatedAt timestamp.

        Args:
            sid: Session ID
            title: New title

        Returns:
            Updated session dict (without _id) or None if not found
        """
        await self._ensure_indexes()

        try:
            result = await self._sessions.update_one(
                {"id": sid},
                {"$set": {"title": title, "updatedAt": _now_iso()}},
            )
            if result.modified_count > 0:
                return await self.get(sid)
            return None
        except Exception as e:
            logger.error(f"Failed to rename session {sid}: {e}")
            return None

    async def delete(self, sid: str) -> bool:
        """Delete a session and all its turns.

        Args:
            sid: Session ID

        Returns:
            True if the session was deleted, False if it didn't exist
        """
        await self._ensure_indexes()

        try:
            # Delete the session document
            session_result = await self._sessions.delete_one({"id": sid})

            # Delete all turns for this session
            await self._turns.delete_many({"sessionId": sid})

            return session_result.deleted_count > 0
        except Exception as e:
            logger.error(f"Failed to delete session {sid}: {e}")
            return False

    async def turns(self, sid: str) -> list[dict]:
        """Get all turns for a session, ordered by createdAt ascending.

        Args:
            sid: Session ID

        Returns:
            List of turn dicts (without _id), ordered by createdAt ascending
        """
        await self._ensure_indexes()

        try:
            cursor = self._turns.find({"sessionId": sid}).sort("createdAt", 1)
            turns = []
            async for doc in cursor:
                turns.append({k: v for k, v in doc.items() if k != "_id"})
            return turns
        except Exception as e:
            logger.error(f"Failed to get turns for session {sid}: {e}")
            return []

    async def add_turn(self, sid: str, turn: dict) -> None:
        """Add a turn to a session and bump the turnCount/updatedAt.

        Args:
            sid: Session ID
            turn: Turn dict (will be inserted as a shallow copy to avoid mutating the caller's dict)
        """
        await self._ensure_indexes()

        try:
            # Compute seq (0-based, monotonic per session) = count of existing turns
            existing_turns = await self.turns(sid)
            seq = len(existing_turns)

            # Insert a shallow copy of the turn dict with seq set
            turn_copy = dict(turn)
            turn_copy["seq"] = seq
            await self._turns.insert_one(turn_copy)

            # Update session turnCount and updatedAt
            turns = await self.turns(sid)
            await self._sessions.update_one(
                {"id": sid},
                {
                    "$set": {
                        "turnCount": len(turns),
                        "updatedAt": _now_iso(),
                    }
                },
            )
        except Exception as e:
            logger.error(f"Failed to add turn to session {sid}: {e}")

    async def set_debug(self, sid: str, enabled: bool) -> None:
        """Set debug mode for a session.

        Args:
            sid: Session ID
            enabled: Debug enabled flag
        """
        await self._ensure_indexes()

        try:
            await self._sessions.update_one(
                {"id": sid},
                {"$set": {"debug": enabled}},
            )
        except Exception as e:
            logger.error(f"Failed to set debug for session {sid}: {e}")

    async def get_debug(self, sid: str) -> bool:
        """Get debug mode for a session.

        Args:
            sid: Session ID

        Returns:
            Debug enabled flag (False if session not found)
        """
        await self._ensure_indexes()

        try:
            doc = await self._sessions.find_one({"id": sid}, {"_id": 0})
            return bool(doc.get("debug", False)) if doc else False
        except Exception as e:
            logger.error(f"Failed to get debug for session {sid}: {e}")
            return False
