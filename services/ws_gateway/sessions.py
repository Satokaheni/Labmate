from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class InMemorySessionStore:
    """Minimal session index. Swap for a Mongo-backed store behind this API."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._turns: dict[str, list[dict]] = {}

    def create(
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

    def list(self) -> list[dict]:
        return sorted(self._sessions.values(), key=lambda s: s["updatedAt"], reverse=True)

    def get(self, sid: str) -> dict | None:
        return self._sessions.get(sid)

    def rename(self, sid: str, title: str) -> dict | None:
        s = self._sessions.get(sid)
        if s is None:
            return None
        s["title"] = title
        s["updatedAt"] = _now_iso()
        return s

    def turns(self, sid: str) -> list[dict]:
        return self._turns.get(sid, [])

    def add_turn(self, sid: str, turn: dict) -> None:
        self._turns.setdefault(sid, []).append(turn)
        s = self._sessions.get(sid)
        if s is not None:
            s["turnCount"] = len(self._turns[sid])
            s["updatedAt"] = _now_iso()


class CreateBody(BaseModel):
    mode: str
    title: str = "New session"


class PatchBody(BaseModel):
    title: str


def build_sessions_router(store: InMemorySessionStore) -> APIRouter:
    router = APIRouter()

    @router.get("/sessions")
    def list_sessions() -> list[dict]:
        return store.list()

    @router.post("/sessions", status_code=201)
    def create_session(body: CreateBody) -> dict:
        return store.create(title=body.title, mode=body.mode)

    @router.get("/sessions/{sid}/turns")
    def get_turns(sid: str) -> list[dict]:
        if store.get(sid) is None:
            raise HTTPException(status_code=404, detail="not_found")
        return store.turns(sid)

    @router.patch("/sessions/{sid}")
    def patch_session(sid: str, body: PatchBody) -> dict:
        s = store.rename(sid, body.title)
        if s is None:
            raise HTTPException(status_code=404, detail="not_found")
        return s

    return router
