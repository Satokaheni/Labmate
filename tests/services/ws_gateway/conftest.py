import pytest
import fakeredis.aioredis
from argon2 import PasswordHasher
from services.ws_gateway.user_store import InMemoryUserStore


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def seeded_store():
    store = InMemoryUserStore()
    ph = PasswordHasher()
    await store.create(
        email="admin@labmate.local",
        display_name="Admin",
        password_hash=ph.hash("correct-horse"),
        role="admin",
    )
    return store
