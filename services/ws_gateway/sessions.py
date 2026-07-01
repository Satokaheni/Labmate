from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class InMemorySessionStore:
    """Minimal session index. Swap for a Mongo-backed store behind this API.

    All methods are async to match the MongoSessionStore interface.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._turns: dict[str, list[dict]] = {}
        self._debug: dict[str, bool] = {}

    async def create(
        self,
        *,
        title: str,
        mode: str,
        session_id: str | None = None,
        updated_at: str | None = None,
    ) -> dict:
        sid = session_id or "s-" + uuid.uuid4().hex[:12]
        now = updated_at or _now_iso()
        session = {
            "id": sid,
            "title": title,
            "mode": mode,
            "turnCount": 0,
            "contextTokens": 0,
            "createdAt": now,
            "updatedAt": now,
        }
        self._sessions[sid] = session
        self._turns.setdefault(sid, [])
        return session

    async def list(self) -> list[dict]:
        return sorted(self._sessions.values(), key=lambda s: s["updatedAt"], reverse=True)

    async def get(self, sid: str) -> dict | None:
        return self._sessions.get(sid)

    async def rename(self, sid: str, title: str) -> dict | None:
        s = self._sessions.get(sid)
        if s is None:
            return None
        s["title"] = title
        s["updatedAt"] = _now_iso()
        return s

    async def delete(self, sid: str) -> bool:
        if sid in self._sessions:
            del self._sessions[sid]
            self._turns.pop(sid, None)
            self._debug.pop(sid, None)
            return True
        return False

    async def turns(self, sid: str) -> list[dict]:
        return self._turns.get(sid, [])

    async def add_turn(self, sid: str, turn: dict) -> None:
        # Compute seq (0-based, monotonic per session) = count of existing turns
        existing_turns = self._turns.get(sid, [])
        seq = len(existing_turns)

        # Append turn with seq set
        turn_copy = dict(turn)
        turn_copy["seq"] = seq
        self._turns.setdefault(sid, []).append(turn_copy)

        s = self._sessions.get(sid)
        if s is not None:
            s["turnCount"] = len(self._turns[sid])
            s["updatedAt"] = _now_iso()

    async def set_debug(self, sid: str, enabled: bool) -> None:
        self._debug[sid] = enabled

    async def get_debug(self, sid: str) -> bool:
        return self._debug.get(sid, False)


class CreateBody(BaseModel):
    mode: str
    title: str = "New session"


class PatchBody(BaseModel):
    title: str


def build_sessions_router(store: InMemorySessionStore) -> APIRouter:
    router = APIRouter()

    @router.get("/sessions")
    async def list_sessions() -> list[dict]:
        return await store.list()

    @router.post("/sessions", status_code=201)
    async def create_session(body: CreateBody) -> dict:
        return await store.create(title=body.title, mode=body.mode)

    @router.get("/sessions/{sid}/turns")
    async def get_turns(sid: str) -> list[dict]:
        if await store.get(sid) is None:
            raise HTTPException(status_code=404, detail="not_found")
        return await store.turns(sid)

    @router.patch("/sessions/{sid}")
    async def patch_session(sid: str, body: PatchBody) -> dict:
        s = await store.rename(sid, body.title)
        if s is None:
            raise HTTPException(status_code=404, detail="not_found")
        return s

    return router
