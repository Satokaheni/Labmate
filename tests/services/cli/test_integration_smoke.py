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


@pytest.mark.asyncio
async def test_run_task_with_streaming_live_path_returns_result():
    from services.cli.event_stream import run_task_with_streaming

    renderer = MagicMock()
    renderer.stream_live = AsyncMock()

    client = MagicMock()
    client.get_result = AsyncMock(return_value={"ok": True, "state": {"final_answer": "Z"}})

    stream = MagicMock()
    stream.first = AsyncMock(return_value={"type": "turn.start", "task": "hi"})
    stream.aclose = AsyncMock()
    client.subscribe_events = MagicMock(return_value=stream)

    result = await run_task_with_streaming(client, renderer, "t-1")

    renderer.stream_live.assert_awaited_once_with(stream)
    stream.aclose.assert_awaited_once()
    assert result == {"ok": True, "state": {"final_answer": "Z"}}


@pytest.mark.asyncio
async def test_run_task_with_streaming_fallback_path():
    from services.cli.event_stream import run_task_with_streaming

    renderer = MagicMock()
    renderer.stream_live = AsyncMock()

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    renderer.thinking = MagicMock(return_value=cm)

    client = MagicMock()
    client.get_result = AsyncMock(return_value={"ok": True, "state": {"final_answer": "fb"}})

    stream = MagicMock()
    stream.first = AsyncMock(return_value=None)  # timeout -> fallback
    stream.aclose = AsyncMock()
    client.subscribe_events = MagicMock(return_value=stream)

    result = await run_task_with_streaming(client, renderer, "t-2")

    renderer.stream_live.assert_not_called()
    renderer.thinking.assert_called_once_with("Working…")
    assert result["state"]["final_answer"] == "fb"
