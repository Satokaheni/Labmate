from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from services.cli.redis_client import LabmateRedisClient
from services.cli.renderer import extract_answer, Renderer


@pytest.mark.asyncio
async def test_push_then_get_result():
    """Full push → subscribe → get cycle with mock Redis."""
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock(return_value="msg-1")
    mock_redis.get = AsyncMock(return_value=json.dumps({
        "ok": True,
        "state": {"final_answer": "42"}
    }).encode())

    pubsub = AsyncMock()
    pubsub.get_message = AsyncMock(return_value={"type": "message", "data": b"ready"})
    pubsub.aclose = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=pubsub)

    client = LabmateRedisClient.__new__(LabmateRedisClient)
    client._redis = mock_redis

    await client.push_task("t-1", "what is the answer?", "s-1", "u-1", "ws-1")
    result = await client.get_result("t-1", timeout=5.0)
    assert result["ok"] is True
    assert extract_answer(result["state"]) == "42"


def test_renderer_does_not_raise():
    r = Renderer()
    r.print_answer("# Hello\n\nThis is a **test**.", session_id="s-test")
    r.print_error("something went wrong")
    r.print_info("info message")
