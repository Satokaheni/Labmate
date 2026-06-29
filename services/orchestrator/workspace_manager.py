from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorDatabase

from .models import SessionMeta, User, Workspace

logger = logging.getLogger(__name__)

USERS = "users"
WORKSPACES = "workspaces"
SESSIONS = "sessions"

# Project-instruction files read from each workspace root, in preference order.
# AGENTS.md (the cross-tool standard, https://agents.md) wins over the legacy AGENT.md.
AGENT_INSTRUCTION_FILES = ("AGENTS.md", "AGENT.md")
# Cap the concatenated instructions so a large file can't crowd out the context.
AGENT_INSTRUCTIONS_MAX_CHARS = int(os.getenv("AGENT_INSTRUCTIONS_MAX_CHARS", "16000"))


def _strip(doc: dict | None) -> dict | None:
    """Remove MongoDB's _id before passing to Pydantic."""
    if doc is None:
        return None
    doc.pop("_id", None)
    return doc


class WorkspaceManager:
    """CRUD layer for users, workspaces, and session metadata."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    # ── users ─────────────────────────────────────────────────────────────

    async def create_user(self, display_name: str) -> User:
        user = User(display_name=display_name)
        await self._db[USERS].insert_one(user.model_dump())
        logger.info("created user %s (%s)", user.user_id, display_name)
        return user

    async def get_user(self, user_id: str) -> User | None:
        doc = await self._db[USERS].find_one({"user_id": user_id})
        return User(**_strip(doc)) if doc else None

    async def touch_user(self, user_id: str) -> None:
        await self._db[USERS].update_one(
            {"user_id": user_id},
            {"$set": {"last_active": datetime.now(UTC)}},
        )

    # ── workspaces ────────────────────────────────────────────────────────

    async def create_workspace(
        self,
        user_id: str,
        name: str,
        paths: list[str] | None = None,
        sources: list[str] | None = None,
        description: str | None = None,
        instructions: str | None = None,
    ) -> Workspace:
        ws = Workspace(
            name=name,
            user_id=user_id,
            paths=paths or [],
            sources=sources or [],
            description=description,
            instructions=instructions,
        )
        await self._db[WORKSPACES].insert_one(ws.model_dump())
        logger.info("created workspace %s (%s)", ws.workspace_id, name)
        return ws

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        doc = await self._db[WORKSPACES].find_one({"workspace_id": workspace_id})
        return Workspace(**_strip(doc)) if doc else None

    async def list_workspaces(self, user_id: str, limit: int = 100) -> list[Workspace]:
        cursor = self._db[WORKSPACES].find({"user_id": user_id}).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [Workspace(**_strip(d)) for d in docs]

    async def update_workspace(self, workspace_id: str, **fields) -> None:
        _IMMUTABLE = {"workspace_id", "user_id", "created_at"}
        fields = {k: v for k, v in fields.items() if k not in _IMMUTABLE}
        fields["updated_at"] = datetime.now(UTC)
        await self._db[WORKSPACES].update_one(
            {"workspace_id": workspace_id},
            {"$set": fields},
        )

    async def upsert_workspace(self, workspace_id: str, user_id: str) -> None:
        """Persist a CLI-minted workspace on first sight. No-op if it already exists."""
        now = datetime.now(UTC)
        await self._db[WORKSPACES].update_one(
            {"workspace_id": workspace_id},
            {
                "$setOnInsert": {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "name": f"workspace-{workspace_id[:8]}",
                    "paths": [],
                    "sources": [],
                    "description": None,
                    "instructions": None,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )

    # ── session metadata ──────────────────────────────────────────────────

    async def record_session(self, meta: SessionMeta) -> None:
        await self._db[SESSIONS].insert_one(meta.model_dump())

    async def complete_session(self, session_id: str, ok: bool = True) -> None:
        await self._db[SESSIONS].update_one(
            {"session_id": session_id},
            {"$set": {"completed_at": datetime.now(UTC), "ok": ok}},
        )

    async def list_sessions(
        self,
        user_id: str,
        workspace_id: str | None = None,
        limit: int = 20,
    ) -> list[SessionMeta]:
        q: dict = {"user_id": user_id}
        if workspace_id:
            q["workspace_id"] = workspace_id
        cursor = self._db[SESSIONS].find(q).sort("created_at", -1).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [SessionMeta(**_strip(d)) for d in docs]

    async def load_agent_instructions(self, workspace_id: str) -> str:
        """Project instructions for the agent, pinned for this session.

        Reads AGENTS.md (preferred; the cross-tool standard) or the legacy
        AGENT.md from EACH workspace root and concatenates them (one section per
        root, under a header), capped at AGENT_INSTRUCTIONS_MAX_CHARS. Falls back
        to the workspace.instructions DB field when no file is found.
        """
        if not workspace_id:
            return ""
        ws_doc = await self._db[WORKSPACES].find_one({"workspace_id": workspace_id})
        if not ws_doc:
            return ""

        sections: list[str] = []
        for p in ws_doc.get("paths", []):
            root = Path(p)
            for name in AGENT_INSTRUCTION_FILES:
                candidate = root / name
                if candidate.is_file():
                    try:
                        text = candidate.read_text(encoding="utf-8").strip()
                    except OSError:
                        text = ""
                    if text:
                        sections.append(f"# {root.name}/{name}\n\n{text}")
                    break  # one instruction file per root (AGENTS.md preferred)

        if sections:
            combined = "\n\n".join(sections)
            if len(combined) > AGENT_INSTRUCTIONS_MAX_CHARS:
                combined = combined[:AGENT_INSTRUCTIONS_MAX_CHARS].rstrip() + "\n\n[… truncated]"
            return combined
        return ws_doc.get("instructions") or ""
