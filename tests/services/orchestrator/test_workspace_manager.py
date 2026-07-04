from __future__ import annotations

import pytest

from services.orchestrator.local_store import LocalStore
from services.orchestrator.workspace_manager import WorkspaceManager

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


@pytest.fixture
async def store(tmp_path):
    s = LocalStore(tmp_path / "s.sqlite")
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
def mgr(store):
    return WorkspaceManager(store)


async def test_create_user(mgr):
    user = await mgr.create_user("Alice")
    assert user.display_name == "Alice"
    assert user.user_id

    fetched = await mgr.get_user(user.user_id)
    assert fetched is not None
    assert fetched.display_name == "Alice"


async def test_get_user_not_found(mgr):
    assert await mgr.get_user("nonexistent") is None


async def test_touch_user_updates_last_active(mgr):
    user = await mgr.create_user("Bob")
    before = await mgr.get_user(user.user_id)
    await mgr.touch_user(user.user_id)
    after = await mgr.get_user(user.user_id)
    assert after is not None
    assert after.last_active >= before.last_active


async def test_create_workspace(mgr):
    ws = await mgr.create_workspace(
        user_id="u-1",
        name="my-lab",
        paths=["/workspace/myrepo"],
        instructions="Be concise.",
    )
    assert ws.name == "my-lab"
    assert ws.user_id == "u-1"
    assert ws.paths == ["/workspace/myrepo"]

    fetched = await mgr.get_workspace(ws.workspace_id)
    assert fetched is not None
    assert fetched.name == "my-lab"
    assert fetched.paths == ["/workspace/myrepo"]


async def test_get_workspace_not_found(mgr):
    result = await mgr.get_workspace("nonexistent")
    assert result is None


async def test_list_workspaces(mgr):
    await mgr.create_workspace(user_id="u-1", name="proj-a")
    result = await mgr.list_workspaces("u-1")
    assert len(result) == 1
    assert result[0].name == "proj-a"


async def test_update_workspace_changes_field_and_ignores_immutable(mgr):
    ws = await mgr.create_workspace(user_id="u-1", name="original")
    await mgr.update_workspace(
        ws.workspace_id,
        name="renamed",
        user_id="should-not-change",
    )
    fetched = await mgr.get_workspace(ws.workspace_id)
    assert fetched.name == "renamed"
    assert fetched.user_id == "u-1"
    assert fetched.workspace_id == ws.workspace_id


async def test_complete_session(mgr):
    from services.orchestrator.models import SessionMeta

    meta = SessionMeta(session_id="s-xyz", user_id="u-1", workspace_id="w-1")
    await mgr.record_session(meta)
    await mgr.complete_session("s-xyz", ok=False)

    sessions = await mgr.list_sessions("u-1")
    assert len(sessions) == 1
    assert sessions[0].session_id == "s-xyz"
    assert sessions[0].ok is False
    assert sessions[0].completed_at is not None


async def test_upsert_workspace_creates_default_then_noop(mgr):
    await mgr.upsert_workspace("ws-123", "user-456")
    ws = await mgr.get_workspace("ws-123")
    assert ws is not None
    assert ws.workspace_id == "ws-123"
    assert ws.user_id == "user-456"

    # Second upsert with a different user is a no-op (already exists)
    await mgr.upsert_workspace("ws-123", "other-user")
    ws2 = await mgr.get_workspace("ws-123")
    assert ws2.user_id == "user-456"


async def test_load_agent_instructions_empty_id(mgr):
    assert await mgr.load_agent_instructions("") == ""


async def test_load_agent_instructions_missing_workspace(mgr):
    assert await mgr.load_agent_instructions("nonexistent") == ""


async def test_load_agent_instructions_prefers_agents_md(mgr, tmp_path):
    (tmp_path / "AGENTS.md").write_text("use ruff", encoding="utf-8")
    (tmp_path / "AGENT.md").write_text("legacy", encoding="utf-8")
    ws = await mgr.create_workspace(user_id="u-1", name="w", paths=[str(tmp_path)])

    out = await mgr.load_agent_instructions(ws.workspace_id)
    assert "use ruff" in out
    assert "legacy" not in out  # AGENTS.md wins over AGENT.md


async def test_load_agent_instructions_concatenates_all_roots(mgr, tmp_path):
    a = tmp_path / "repo-a"
    b = tmp_path / "repo-b"
    a.mkdir()
    b.mkdir()
    (a / "AGENTS.md").write_text("rules A", encoding="utf-8")
    (b / "AGENT.md").write_text("rules B", encoding="utf-8")  # legacy still picked up
    ws = await mgr.create_workspace(user_id="u-1", name="w", paths=[str(a), str(b)])

    out = await mgr.load_agent_instructions(ws.workspace_id)
    assert "rules A" in out and "rules B" in out
    assert "repo-a/AGENTS.md" in out and "repo-b/AGENT.md" in out


async def test_load_agent_instructions_falls_back_to_db_field(mgr, tmp_path):
    ws = await mgr.create_workspace(
        user_id="u-1", name="w", paths=[str(tmp_path)], instructions="db rules"
    )
    assert await mgr.load_agent_instructions(ws.workspace_id) == "db rules"


async def test_load_agent_instructions_caps_size(mgr, tmp_path):
    from services.orchestrator.workspace_manager import AGENT_INSTRUCTIONS_MAX_CHARS

    (tmp_path / "AGENTS.md").write_text(
        "x" * (AGENT_INSTRUCTIONS_MAX_CHARS + 5000), encoding="utf-8"
    )
    ws = await mgr.create_workspace(user_id="u-1", name="w", paths=[str(tmp_path)])

    out = await mgr.load_agent_instructions(ws.workspace_id)
    assert len(out) <= AGENT_INSTRUCTIONS_MAX_CHARS + 32  # cap + truncation marker
    assert out.endswith("[… truncated]")
