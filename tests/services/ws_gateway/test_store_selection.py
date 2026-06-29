import pytest

from services.ws_gateway.config import Config
from services.ws_gateway.server import _build_user_store
from services.ws_gateway.user_store import InMemoryUserStore, SqliteUserStore


def _cfg(tmp_path, **over):
    base = dict(
        redis_url="redis://x",
        jwt_secret="s",
        admin_email="a@b.c",
        admin_password="",
        jwt_expiry_seconds=60,
        cors_origins=("*",),
        mongo_url="mongodb://x",
        data_dir=str(tmp_path),
    )
    base.update(over)
    return Config(**base)


def test_selects_sqlite(tmp_path):
    store = _build_user_store(_cfg(tmp_path, user_store="sqlite"))
    assert isinstance(store, SqliteUserStore)
    assert (tmp_path / "users.db").exists()


def test_selects_memory(tmp_path):
    assert isinstance(_build_user_store(_cfg(tmp_path, user_store="memory")), InMemoryUserStore)


def test_unknown_store_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown USER_STORE"):
        _build_user_store(_cfg(tmp_path, user_store="bogus"))


@pytest.mark.asyncio
async def test_seed_idempotent_across_reopen(tmp_path):
    # First store: empty → admin would be seeded. Second store on same dir: count
    # stays 1, so _seed_admin's `count()==0` guard skips (no re-seed needed).
    s1 = _build_user_store(_cfg(tmp_path, user_store="sqlite"))
    await s1.create(email="a@b.c", display_name="Admin", password_hash="h", role="admin")
    s2 = _build_user_store(_cfg(tmp_path, user_store="sqlite"))
    assert await s2.count() == 1
