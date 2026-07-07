"""Piece 2b: _handle persists user+assistant turns to the local store (continuity write)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.orchestrator.local_store import LocalStore
from services.orchestrator.main import OrchestratorProcess


@pytest.mark.asyncio
async def test_persist_turns_writes_user_then_assistant(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    storage = MagicMock()
    storage.local_store = store

    proc = OrchestratorProcess()
    await proc._persist_turns(storage, "sess1", "what is 2+2?", "4")

    turns = await store.all_turns("sess1")
    assert [(t["seq"], t["role"], t["text"]) for t in turns] == [
        (0, "user", "what is 2+2?"),
        (1, "assistant", "4"),
    ]


@pytest.mark.asyncio
async def test_persist_turns_accumulates_across_calls(tmp_path):
    """A second goal in the same session appends after the first (continuity)."""
    store = LocalStore(tmp_path / "s.sqlite")
    storage = MagicMock()
    storage.local_store = store
    proc = OrchestratorProcess()

    await proc._persist_turns(storage, "s", "turn one", "reply one")
    await proc._persist_turns(storage, "s", "turn two", "reply two")

    turns = await store.all_turns("s")
    assert [t["text"] for t in turns] == ["turn one", "reply one", "turn two", "reply two"]


@pytest.mark.asyncio
async def test_persist_turns_empty_session_id_is_noop(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    storage = MagicMock()
    storage.local_store = store
    proc = OrchestratorProcess()
    await proc._persist_turns(storage, "", "u", "a")  # no session -> nothing written
    assert await store.all_turns("s") == []


@pytest.mark.asyncio
async def test_persist_turns_swallows_store_errors(tmp_path):
    """A store failure must not raise into the caller (best-effort)."""
    storage = MagicMock()
    failing = MagicMock()

    async def _boom(*a, **k):
        raise RuntimeError("disk full")

    failing.append_turn = _boom
    storage.local_store = failing
    proc = OrchestratorProcess()
    # must not raise
    await proc._persist_turns(storage, "s", "u", "a")
