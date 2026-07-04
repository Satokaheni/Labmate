"""Piece 2b: end-to-end continuity — turns written by _persist_turns are read
back by ContextManager through the shared LocalStore (fix-A)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.memory.context_manager import ContextManager
from services.orchestrator.local_store import LocalStore
from services.orchestrator.main import OrchestratorProcess


async def _noop_embed(texts):
    return [[0.0] for _ in texts]


@pytest.mark.asyncio
async def test_second_turn_sees_first_turn(tmp_path):
    store = LocalStore(tmp_path / "state.sqlite")
    await store.connect()

    # Turn 1 completes: the orchestrator self-persists the user+assistant turn.
    storage = MagicMock()
    storage.local_store = store
    proc = OrchestratorProcess.__new__(OrchestratorProcess)
    await proc._persist_turns(storage, "sess-1", "my name is Zed", "Nice to meet you, Zed!")

    # Turn 2 begins: the context manager assembles recent turns for the same session.
    cm = ContextManager(
        chroma_cols={},
        embedder=_noop_embed,
        local_store=store,
    )
    recent = await cm._recent_turns("sess-1", budget=1000)

    assert "my name is Zed" in recent
    assert "Nice to meet you, Zed!" in recent
    assert "USER:" in recent and "ASSISTANT:" in recent
    # chronological: the user turn appears before the assistant turn
    assert recent.index("my name is Zed") < recent.index("Nice to meet you")


@pytest.mark.asyncio
async def test_other_session_is_isolated(tmp_path):
    store = LocalStore(tmp_path / "state.sqlite")
    await store.connect()
    storage = MagicMock()
    storage.local_store = store
    proc = OrchestratorProcess.__new__(OrchestratorProcess)
    await proc._persist_turns(storage, "sess-A", "secret A", "reply A")

    cm = ContextManager(chroma_cols={}, embedder=_noop_embed, local_store=store)
    # A different session sees none of sess-A's turns.
    assert await cm._recent_turns("sess-B", budget=1000) == ""
