from __future__ import annotations
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from services.orchestrator.workspace_manager import WorkspaceManager
from services.orchestrator.models import User, Workspace


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


@pytest.mark.asyncio
async def test_create_user(mgr, mock_db):
    mock_db["users"].insert_one = AsyncMock(return_value=MagicMock(inserted_id="abc"))
    user = await mgr.create_user("Alice")
    assert user.display_name == "Alice"
    assert user.user_id
    mock_db["users"].insert_one.assert_called_once()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_get_workspace_not_found(mgr, mock_db):
    mock_db["workspaces"].find_one = AsyncMock(return_value=None)
    result = await mgr.get_workspace("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_list_workspaces(mgr, mock_db):
    now = datetime.now(timezone.utc)
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[
        {"workspace_id": "ws-1", "name": "proj-a", "user_id": "u-1",
         "paths": [], "sources": [], "instructions": None,
         "description": None, "created_at": now, "updated_at": now},
    ])
    mock_db["workspaces"].find = MagicMock(return_value=cursor)
    result = await mgr.list_workspaces("u-1")
    assert len(result) == 1
    assert result[0].name == "proj-a"
