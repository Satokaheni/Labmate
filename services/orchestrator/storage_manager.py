from __future__ import annotations

import logging
from datetime import UTC, datetime

from .local_store import LocalStore, get_local_store
from .workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StorageManager:
    """Local SQLite store (source of truth) facade."""

    def __init__(self) -> None:
        pass

    async def __aenter__(self) -> StorageManager:
        await self.local_store.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        """Nothing to close — the local store connection is process-cached."""
        return None

    @property
    def context_manager(self):
        """Lazy ContextManager wired to this StorageManager's LocalStore.

        chroma_cols is empty and Chroma was removed in Piece 3, so RAG retrieval
        is inert (hybrid_retrieve returns []). Continuity (recent turns + summary/
        anchor/watermark, all from the LocalStore's session_kv table) is what this
        provides.
        """
        if not hasattr(self, "_context_manager"):
            from services.memory.context_manager import ContextManager
            from services.memory.embedder import embed as _embed_fn

            async def _embedder(texts: list[str]) -> list[list[float]]:
                return await _embed_fn(texts)

            self._context_manager = ContextManager(
                chroma_cols={},
                embedder=_embedder,
                local_store=self.local_store,
            )
        return self._context_manager

    @property
    def workspaces(self) -> WorkspaceManager:
        """Lazy WorkspaceManager sharing this StorageManager's LocalStore.

        Lazy (like local_store/context_manager) so merely constructing a
        StorageManager doesn't eagerly resolve/cache the process-wide LocalStore
        singleton — tests that never touch .workspaces or .local_store shouldn't
        pay for (or pollute) that side effect.
        """
        if getattr(self, "_workspaces", None) is None:
            self._workspaces = WorkspaceManager(self.local_store)
        return self._workspaces

    @property
    def local_store(self) -> LocalStore:
        """The local SQLite store (process-cached). Lazy — the connection opens
        on first store call."""
        if getattr(self, "_local_store", None) is None:
            self._local_store = get_local_store()
        return self._local_store

    # --- full-text / regex search over raw transcript (session_search) ----------
    async def search_turns(
        self,
        query: str,
        top_k: int = 8,
        *,
        mode: str = "text",
        session_id: str | None = None,
    ) -> list[dict]:
        """Keyword/regex search over local chat turns.

        Returns ``[{"sessionId": ..., "seq": ..., "text": ...}]``. Empty query →
        ``[]``. ``mode="text"`` = case-insensitive substring; ``mode="regex"`` =
        Python regex. ``session_id`` scopes to one session. Best-effort (any error
        → ``[]``) — delegated to the local SQLite store.
        """
        return await self.local_store.search_turns(
            query, mode=mode, session_id=session_id, limit=top_k
        )
