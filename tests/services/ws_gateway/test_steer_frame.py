import pytest
import fakeredis.aioredis

from services.ws_gateway.redis_bridge import write_steer, STEER_PREFIX


@pytest.mark.asyncio
async def test_write_steer_sets_key_with_text():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_steer(r, "task-9", "switch to db.py")
    assert await r.get(f"{STEER_PREFIX}task-9") == "switch to db.py"
    assert await r.ttl(f"{STEER_PREFIX}task-9") > 0
