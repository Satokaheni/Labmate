from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from services.cli.redis_client import LabmateRedisClient

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.xadd = AsyncMock(return_value="msg-id")
    r.get = AsyncMock(return_value=json.dumps({"ok": True, "state": {"final_answer": "hello"}}).encode())
    return r


@pytest.mark.asyncio
async def test_push_task(mock_redis):
    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = mock_redis
    await client.push_task(
        task_id="t-1",
        task="write hello world",
        session_id="s-1",
        user_id="u-1",
        workspace_id="ws-1",
    )
    mock_redis.xadd.assert_called_once()
    call_args = mock_redis.xadd.call_args
    payload = json.loads(call_args[0][1]["payload"])
    assert payload["task_id"] == "t-1"
    assert payload["user_id"] == "u-1"
    assert payload["workspace_id"] == "ws-1"


@pytest.mark.asyncio
async def test_get_result_ok(mock_redis):
    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = mock_redis

    pubsub = AsyncMock()
    pubsub.get_message = AsyncMock(return_value={"type": "message", "data": b"ready"})
    pubsub.aclose = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=pubsub)

    result = await client.get_result("t-1", timeout=5.0)
    assert result["ok"] is True
    assert result["state"]["final_answer"] == "hello"
