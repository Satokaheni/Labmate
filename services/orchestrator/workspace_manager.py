from __future__ import annotations

import logging
import os
from pathlib import Path

from .local_store import LocalStore
from .models import SessionMeta, User, Workspace

logger = logging.getLogger(__name__)

# Project-instruction files read from each workspace root, in preference order.
# AGENTS.md (the cross-tool standard, https://agents.md) wins over the legacy AGENT.md.
AGENT_INSTRUCTION_FILES = ("AGENTS.md", "AGENT.md")
# Cap the concatenated instructions so a large file can't crowd out the context.
AGENT_INSTRUCTIONS_MAX_CHARS = int(os.getenv("AGENT_INSTRUCTIONS_MAX_CHARS", "16000"))

_WORKSPACE_MODEL_FIELDS = (
    "workspace_id",
    "name",
    "user_id",
    "description",
    "paths",
    "sources",
    "created_at",
    "updated_at",
)


class WorkspaceManager:
    """CRUD layer for users, workspaces, and session metadata (local SQLite store)."""

    def __init__(self, store: LocalStore) -> None:
        self._store = store

    # ── users ─────────────────────────────────────────────────────────────

    async def create_user(self, display_name: str) -> User:
        user = User(display_name=display_name)
        await self._store.upsert_user(user.user_id, display_name)
        logger.info("created user %s (%s)", user.user_id, display_name)
        return user

    async def get_user(self, user_id: str) -> User | None:
        doc = await self._store.get_user(user_id)
        return User(**doc) if doc else None

    async def touch_user(self, user_id: str) -> None:
        await self._store.touch_user(user_id)

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
        )
        await self._store.create_workspace(
            ws.workspace_id,
            user_id,
            name=name,
            paths=ws.paths,
            sources=ws.sources,
            description=description,
            instructions=instructions,
        )
        logger.info("created workspace %s (%s)", ws.workspace_id, name)
        return ws

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        doc = await self._store.get_workspace(workspace_id)
        if not doc:
            return None
        return Workspace(**{k: doc[k] for k in _WORKSPACE_MODEL_FIELDS})

    async def list_workspaces(self, user_id: str, limit: int = 100) -> list[Workspace]:
        docs = await self._store.list_workspaces(user_id, limit=limit)
        return [Workspace(**{k: d[k] for k in _WORKSPACE_MODEL_FIELDS}) for d in docs]

    async def update_workspace(self, workspace_id: str, **fields) -> None:
        await self._store.update_workspace(workspace_id, **fields)

    async def upsert_workspace(self, workspace_id: str, user_id: str) -> None:
        """Persist a CLI-minted workspace on first sight. No-op if it already exists."""
        await self._store.upsert_workspace(workspace_id, user_id)

    # ── session metadata ──────────────────────────────────────────────────

    async def record_session(self, meta: SessionMeta) -> None:
        await self._store.record_session(
            meta.session_id,
            user_id=meta.user_id,
            workspace_id=meta.workspace_id,
            task_preview=meta.task_preview,
        )

    async def complete_session(self, session_id: str, ok: bool = True) -> None:
        await self._store.complete_session(session_id, ok)

    async def list_sessions(
        self,
        user_id: str,
        workspace_id: str | None = None,
        limit: int = 20,
    ) -> list[SessionMeta]:
        rows = await self._store.list_sessions(user_id, workspace_id=workspace_id, limit=limit)
        result = []
        for r in rows:
            kwargs = {
                "session_id": r["session_id"],
                "user_id": r["user_id"],
                "workspace_id": r["workspace_id"],
                "task_preview": r["task_preview"] or "",
            }
            if r.get("created_at"):
                kwargs["created_at"] = r["created_at"]
            if r.get("completed_at"):
                kwargs["completed_at"] = r["completed_at"]
            if r.get("ok") is not None:
                kwargs["ok"] = r["ok"]
            result.append(SessionMeta(**kwargs))
        return result

    async def load_agent_instructions(self, workspace_id: str) -> str:
        """Project instructions for the agent, pinned for this session.

        Reads AGENTS.md (preferred; the cross-tool standard) or the legacy
        AGENT.md from EACH workspace root and concatenates them (one section per
        root, under a header), capped at AGENT_INSTRUCTIONS_MAX_CHARS. Falls back
        to the workspace.instructions DB field when no file is found.
        """
        if not workspace_id:
            return ""
        ws_doc = await self._store.get_workspace(workspace_id)
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
