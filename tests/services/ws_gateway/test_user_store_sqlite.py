import os
import stat

import pytest

from services.ws_gateway.user_store import SqliteUserStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "data" / "users.db")


async def test_create_then_find_roundtrip(db_path):
    store = SqliteUserStore(db_path)
    created = await store.create(
        email="Admin@Labmate.Local",
        display_name="Admin",
        password_hash="argon2$hash",
        role="admin",
    )
    assert created["id"].startswith("u-")
    assert created["email"] == "admin@labmate.local"  # lowercased
    found = await store.find_by_email("admin@labmate.local")
    assert found == created


async def test_find_is_case_insensitive(db_path):
    store = SqliteUserStore(db_path)
    await store.create(email="a@b.com", display_name="A", password_hash="h")
    assert (await store.find_by_email("A@B.COM"))["email"] == "a@b.com"
    assert await store.find_by_email("missing@x.com") is None


async def test_count_and_persistence_across_reopen(db_path):
    s1 = SqliteUserStore(db_path)
    assert await s1.count() == 0
    await s1.create(email="a@b.com", display_name="A", password_hash="h", role="admin")
    assert await s1.count() == 1
    # Re-open the SAME file with a fresh instance — the durability guarantee.
    s2 = SqliteUserStore(db_path)
    assert await s2.count() == 1
    assert (await s2.find_by_email("a@b.com"))["role"] == "admin"


async def test_duplicate_email_rejected(db_path):
    import sqlite3

    store = SqliteUserStore(db_path)
    await store.create(email="a@b.com", display_name="A", password_hash="h")
    with pytest.raises(sqlite3.IntegrityError):
        await store.create(email="A@B.com", display_name="A2", password_hash="h2")


async def test_db_file_is_0600(db_path):
    SqliteUserStore(db_path)
    mode = stat.S_IMODE(os.stat(db_path).st_mode)
    assert mode == 0o600
