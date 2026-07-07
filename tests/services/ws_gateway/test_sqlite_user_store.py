import pytest

from services.orchestrator.local_store import LocalStore
from services.ws_gateway.user_store import SqliteUserStore


@pytest.mark.asyncio
async def test_sqlite_user_store_create_find_count(tmp_path):
    us = SqliteUserStore(LocalStore(tmp_path / "state.db"))
    assert await us.count() == 0
    doc = await us.create(email="Admin@X.io", display_name="Admin", password_hash="h", role="admin")
    assert doc["email"] == "admin@x.io" and doc["id"].startswith("u-")
    assert doc["role"] == "admin" and doc["displayName"] == "Admin"
    assert await us.count() == 1
    assert (await us.find_by_email("ADMIN@x.io"))["id"] == doc["id"]
    assert await us.find_by_email("nope@x.io") is None
