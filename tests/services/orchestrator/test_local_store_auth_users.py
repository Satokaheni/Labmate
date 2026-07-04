import aiosqlite
import pytest

from services.orchestrator.local_store import LocalStore


@pytest.mark.asyncio
async def test_auth_user_create_find_count(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    assert await store.auth_user_count() == 0
    await store.auth_user_create(
        id="u-abc",
        email="Admin@X.io",
        display_name="Admin",
        password_hash="h",
        role="admin",
        created_at="2026-07-04T00:00:00Z",
    )
    assert await store.auth_user_count() == 1
    got = await store.auth_user_find_by_email("admin@x.io")  # case-insensitive
    assert got == {
        "id": "u-abc",
        "email": "admin@x.io",
        "displayName": "Admin",
        "passwordHash": "h",
        "role": "admin",
        "createdAt": "2026-07-04T00:00:00Z",
    }
    assert await store.auth_user_find_by_email("missing@x.io") is None
    await store.close()


@pytest.mark.asyncio
async def test_auth_user_create_duplicate_email_raises(tmp_path):
    store = LocalStore(tmp_path / "state.db")
    await store.auth_user_create(
        id="u-1", email="a@x.io", display_name="A", password_hash="h", role="user", created_at="t"
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await store.auth_user_create(
            id="u-2",
            email="A@x.io",
            display_name="A2",
            password_hash="h2",
            role="user",
            created_at="t2",
        )
    await store.close()
