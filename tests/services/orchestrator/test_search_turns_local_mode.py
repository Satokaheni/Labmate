"""Piece 2a: StorageManager.search_turns delegates to the local SQLite store."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.orchestrator.storage_manager import StorageManager


def _storage():
    return StorageManager.from_clients(mongo=MagicMock())


@pytest.fixture(autouse=True)
def _reset_local_store():
    import services.orchestrator.local_store as ls

    ls._STORE = None
    yield
    ls._STORE = None


@pytest.mark.asyncio
async def test_search_turns_returns_local_hits(monkeypatch, tmp_path):
    monkeypatch.setenv("LABMATE_STATE_DB", str(tmp_path / "s.sqlite"))
    sm = _storage()
    store = sm.local_store
    await store.append_turn("sess", "user", "find the alpha marker")
    await store.append_turn("sess", "assistant", "unrelated reply")

    hits = await sm.search_turns("alpha", session_id="sess")
    assert [h["text"] for h in hits] == ["find the alpha marker"]
    assert hits[0]["sessionId"] == "sess"


@pytest.mark.asyncio
async def test_search_turns_empty_query_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("LABMATE_STATE_DB", str(tmp_path / "s.sqlite"))
    sm = _storage()
    assert await sm.search_turns("   ", session_id="sess") == []
