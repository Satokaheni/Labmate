from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator.workspace_manager import WorkspaceManager

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


@pytest.fixture
def mock_db():
    collections = {}

    def _get(key):
        if key not in collections:
            collections[key] = MagicMock()
        return collections[key]

    db = MagicMock()
    db.__getitem__ = MagicMock(side_effect=_get)
    return db


@pytest.fixture
def mgr(mock_db):
    return WorkspaceManager(mock_db)


async def test_create_user(mgr, mock_db):
    mock_db["users"].insert_one = AsyncMock(return_value=MagicMock(inserted_id="abc"))
    user = await mgr.create_user("Alice")
    assert user.display_name == "Alice"
    assert user.user_id
    mock_db["users"].insert_one.assert_called_once()


async def test_create_workspace(mgr, mock_db):
    mock_db["workspaces"].insert_one = AsyncMock(return_value=MagicMock(inserted_id="abc"))
    ws = await mgr.create_workspace(
        user_id="u-1",
        name="my-lab",
        paths=["/workspace/myrepo"],
        instructions="Be concise.",
    )
    assert ws.name == "my-lab"
    assert ws.user_id == "u-1"
    assert ws.paths == ["/workspace/myrepo"]
    mock_db["workspaces"].insert_one.assert_called_once()


async def test_get_workspace_not_found(mgr, mock_db):
    mock_db["workspaces"].find_one = AsyncMock(return_value=None)
    result = await mgr.get_workspace("nonexistent")
    assert result is None


async def test_list_workspaces(mgr, mock_db):
    now = datetime.now(UTC)
    cursor = MagicMock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "workspace_id": "ws-1",
                "name": "proj-a",
                "user_id": "u-1",
                "paths": [],
                "sources": [],
                "instructions": None,
                "description": None,
                "created_at": now,
                "updated_at": now,
            },
        ]
    )
    cursor.limit = MagicMock(return_value=cursor)
    mock_db["workspaces"].find = MagicMock(return_value=cursor)
    result = await mgr.list_workspaces("u-1")
    assert len(result) == 1
    assert result[0].name == "proj-a"


async def test_touch_user(mgr, mock_db):
    mock_db["users"].update_one = AsyncMock()
    await mgr.touch_user("u-abc")
    call_args = mock_db["users"].update_one.call_args
    assert call_args[0][0] == {"user_id": "u-abc"}
    assert "last_active" in call_args[0][1]["$set"]


async def test_complete_session(mgr, mock_db):
    mock_db["sessions"].update_one = AsyncMock()
    await mgr.complete_session("s-xyz", ok=False)
    call_args = mock_db["sessions"].update_one.call_args
    assert call_args[0][0] == {"session_id": "s-xyz"}
    assert call_args[0][1]["$set"]["ok"] is False
    assert "completed_at" in call_args[0][1]["$set"]


async def test_upsert_workspace_calls_update_one(mgr, mock_db):
    mock_db["workspaces"].update_one = AsyncMock()
    await mgr.upsert_workspace("ws-123", "user-456")
    call_args = mock_db["workspaces"].update_one.call_args
    assert call_args[0][0] == {"workspace_id": "ws-123"}
    assert call_args[1]["upsert"] is True
    assert "created_at" in call_args[0][1]["$setOnInsert"]
    assert "updated_at" in call_args[0][1]["$setOnInsert"]
    assert call_args[0][1]["$setOnInsert"]["user_id"] == "user-456"
    assert call_args[0][1]["$setOnInsert"]["workspace_id"] == "ws-123"
    mock_db["workspaces"].update_one.assert_called_once()


async def test_load_agent_instructions_empty_id(mgr):
    assert await mgr.load_agent_instructions("") == ""


async def test_load_agent_instructions_prefers_agents_md(mgr, mock_db, tmp_path):
    (tmp_path / "AGENTS.md").write_text("use ruff", encoding="utf-8")
    (tmp_path / "AGENT.md").write_text("legacy", encoding="utf-8")
    mock_db["workspaces"].find_one = AsyncMock(return_value={"paths": [str(tmp_path)]})

    out = await mgr.load_agent_instructions("ws-1")
    assert "use ruff" in out
    assert "legacy" not in out  # AGENTS.md wins over AGENT.md


async def test_load_agent_instructions_concatenates_all_roots(mgr, mock_db, tmp_path):
    a = tmp_path / "repo-a"
    b = tmp_path / "repo-b"
    a.mkdir()
    b.mkdir()
    (a / "AGENTS.md").write_text("rules A", encoding="utf-8")
    (b / "AGENT.md").write_text("rules B", encoding="utf-8")  # legacy still picked up
    mock_db["workspaces"].find_one = AsyncMock(return_value={"paths": [str(a), str(b)]})

    out = await mgr.load_agent_instructions("ws-1")
    assert "rules A" in out and "rules B" in out
    assert "repo-a/AGENTS.md" in out and "repo-b/AGENT.md" in out


async def test_load_agent_instructions_falls_back_to_db_field(mgr, mock_db, tmp_path):
    mock_db["workspaces"].find_one = AsyncMock(
        return_value={"paths": [str(tmp_path)], "instructions": "db rules"}
    )
    assert await mgr.load_agent_instructions("ws-1") == "db rules"


async def test_load_agent_instructions_caps_size(mgr, mock_db, tmp_path):
    from services.orchestrator.workspace_manager import AGENT_INSTRUCTIONS_MAX_CHARS

    (tmp_path / "AGENTS.md").write_text(
        "x" * (AGENT_INSTRUCTIONS_MAX_CHARS + 5000), encoding="utf-8"
    )
    mock_db["workspaces"].find_one = AsyncMock(return_value={"paths": [str(tmp_path)]})

    out = await mgr.load_agent_instructions("ws-1")
    assert len(out) <= AGENT_INSTRUCTIONS_MAX_CHARS + 32  # cap + truncation marker
    assert out.endswith("[… truncated]")
