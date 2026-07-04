"""Piece 2a: LocalStore SQLite persistence — connection + schema."""

from __future__ import annotations

import asyncio

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


@pytest.mark.asyncio
async def test_append_turn_assigns_monotonic_seq_per_session(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        assert await store.append_turn("sess-A", "user", "hello") == 0
        assert await store.append_turn("sess-A", "assistant", "hi") == 1
        # A different session has its own seq counter.
        assert await store.append_turn("sess-B", "user", "yo") == 0
        assert await store.append_turn("sess-A", "user", "again") == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recent_turns_respects_watermark_and_returns_ascending(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        for _i, txt in enumerate(["a", "b", "c", "d"]):
            await store.append_turn("s", "user", txt)
        # watermark=1 → only seq 2,3 ; ascending
        turns = await store.recent_turns("s", watermark=1)
        assert [t["seq"] for t in turns] == [2, 3]
        assert [t["text"] for t in turns] == ["c", "d"]
        assert turns[0]["role"] == "user"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recent_turns_limit_keeps_newest_tail_ascending(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        for i in range(5):
            await store.append_turn("s", "user", f"m{i}")
        turns = await store.recent_turns("s", watermark=-1, limit=2)
        assert [t["text"] for t in turns] == ["m3", "m4"]  # newest 2, ascending
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_all_turns_ascending(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        await store.append_turn("s", "user", "one")
        await store.append_turn("s", "assistant", "two")
        rows = await store.all_turns("s")
        assert [(r["seq"], r["role"], r["text"]) for r in rows] == [
            (0, "user", "one"),
            (1, "assistant", "two"),
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_last_activity_iso(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        assert await store.last_activity_iso("s") is None
        await store.append_turn("s", "user", "x", created_at="2026-07-03T00:00:00Z")
        await store.append_turn("s", "user", "y", created_at="2026-07-03T01:00:00Z")
        assert await store.last_activity_iso("s") == "2026-07-03T01:00:00Z"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_search_turns_text_and_regex(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        await store.append_turn("s", "user", "the quick brown fox")
        await store.append_turn("s", "assistant", "a lazy dog sleeps")
        await store.append_turn("other", "user", "quick unrelated")
        # text mode: case-insensitive substring, scoped to session
        hits = await store.search_turns("QUICK", mode="text", session_id="s")
        assert [h["text"] for h in hits] == ["the quick brown fox"]
        assert hits[0]["sessionId"] == "s"
        # regex mode
        rhits = await store.search_turns(r"la.y", mode="regex", session_id="s")
        assert [h["text"] for h in rhits] == ["a lazy dog sleeps"]
        # empty query → []
        assert await store.search_turns("  ", session_id="s") == []
        # no session scope → searches all
        allhits = await store.search_turns("quick", mode="text")
        assert len(allhits) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_search_turns_text_mode_treats_wildcards_literally(tmp_path):
    """LIKE metacharacters in the query match literally (escaped), not as wildcards."""
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        await store.append_turn("s", "user", "discount is 50% today")
        await store.append_turn("s", "user", "the number 500 appears")
        # "50%" must match only the literal "50%" turn, not "500".
        hits = await store.search_turns("50%", mode="text", session_id="s")
        assert [h["text"] for h in hits] == ["discount is 50% today"]
        # "_" is literal too: matches nothing when no underscore present.
        assert await store.search_turns("5_0", mode="text", session_id="s") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_and_list_sessions(tmp_path):
    """Record two sessions for a user (one in a workspace), then list them."""
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        # Record first session (no workspace)
        await store.record_session("sid-1", user_id="u", task_preview="task 1")
        # Record second session (in workspace "w")
        await store.record_session("sid-2", user_id="u", workspace_id="w", task_preview="task 2")

        # List all sessions for user "u" (both, newest first)
        all_sessions = await store.list_sessions("u")
        assert len(all_sessions) == 2
        assert all_sessions[0]["session_id"] == "sid-2"  # newest first
        assert all_sessions[1]["session_id"] == "sid-1"

        # List sessions only in workspace "w"
        w_sessions = await store.list_sessions("u", workspace_id="w")
        assert len(w_sessions) == 1
        assert w_sessions[0]["session_id"] == "sid-2"

        # Before completion, ok should be None
        assert all_sessions[0]["ok"] is None
        assert all_sessions[0]["completed_at"] is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_complete_session_sets_ok(tmp_path):
    """Record a session, complete it with ok=True, then verify the fields."""
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        await store.record_session("sid-1", user_id="u")
        await store.complete_session("sid-1", ok=True)

        sessions = await store.list_sessions("u")
        assert len(sessions) == 1
        assert sessions[0]["ok"] is True
        assert sessions[0]["completed_at"] is not None

        # Complete again with ok=False
        await store.complete_session("sid-1", ok=False)
        sessions = await store.list_sessions("u")
        assert sessions[0]["ok"] is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_session_preserves_completion_on_re_record(tmp_path):
    """Re-recording a session should preserve its completion status."""
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        await store.record_session("sid-1", user_id="u", task_preview="task 1")
        await store.complete_session("sid-1", ok=True)

        sessions = await store.list_sessions("u")
        assert sessions[0]["ok"] is True
        completed_at_1 = sessions[0]["completed_at"]

        # Re-record the same session with different task_preview
        await store.record_session("sid-1", user_id="u", task_preview="task 1 updated")

        sessions = await store.list_sessions("u")
        assert sessions[0]["ok"] is True  # preserved
        assert sessions[0]["completed_at"] == completed_at_1  # preserved
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_upsert_workspace_idempotent_and_get(tmp_path):
    """Upsert workspace twice (idempotent); get should return the row with parsed JSON."""
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        await store.upsert_workspace("w", "u")
        ws = await store.get_workspace("w")

        assert ws is not None
        assert ws["workspace_id"] == "w"
        assert ws["user_id"] == "u"
        assert ws["name"] == "workspace-w"  # default name from first 8 chars
        assert ws["paths"] == []
        assert ws["sources"] == []

        # Second upsert with different user should be ignored
        await store.upsert_workspace("w", "other-user")
        ws = await store.get_workspace("w")
        assert ws["user_id"] == "u"  # unchanged

        # Missing workspace returns None
        assert await store.get_workspace("missing") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_upsert_user_and_touch(tmp_path):
    """Upsert user, get it back, touch it, and verify last_active updates."""
    store = LocalStore(tmp_path / "s.sqlite")
    await store.connect()
    try:
        await store.upsert_user("u", "Zed")
        user = await store.get_user("u")

        assert user is not None
        assert user["user_id"] == "u"
        assert user["display_name"] == "Zed"
        assert user["created_at"] is not None
        assert user["last_active"] is not None
        created_at = user["created_at"]
        last_active_1 = user["last_active"]

        # Second upsert should not overwrite
        await store.upsert_user("u", "Someone Else")
        user = await store.get_user("u")
        assert user["display_name"] == "Zed"  # unchanged

        # Sleep to ensure timestamp changes (1-second granularity)
        await asyncio.sleep(1.1)

        # Touch the user
        await store.touch_user("u")
        user = await store.get_user("u")
        assert user["last_active"] != last_active_1  # updated
        assert user["created_at"] == created_at  # unchanged

        # Missing user returns None
        assert await store.get_user("missing") is None
    finally:
        await store.close()
