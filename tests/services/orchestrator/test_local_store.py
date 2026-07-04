"""Piece 2a: LocalStore SQLite persistence — connection + schema."""

from __future__ import annotations

import pytest

from services.orchestrator.local_store import LocalStore, get_local_store


@pytest.mark.asyncio
async def test_connect_creates_chat_turns_table(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        conn = store._conn  # connected handle
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_turns'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "chat_turns"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_connect_is_idempotent(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    await store.connect()  # must not raise / must not re-create
    await store.close()


@pytest.mark.asyncio
async def test_connect_creates_parent_dir(tmp_path):
    db = tmp_path / "nested" / "deep" / "s.sqlite"
    store = LocalStore(db)
    await store.connect()
    try:
        assert db.exists()
    finally:
        await store.close()


def test_get_local_store_uses_state_db_path(monkeypatch, tmp_path):
    db = tmp_path / "state.sqlite"
    monkeypatch.setenv("LABMATE_STATE_DB", str(db))
    s1 = get_local_store()
    s2 = get_local_store()
    assert s1 is s2  # process-cached singleton
    assert str(s1.db_path) == str(db)
