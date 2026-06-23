from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from services.cli.repl import REPL, REPLContext
from services.cli.event_stream import FIRST_EVENT_TIMEOUT
from services.cli.identity import Identity


def _ctx():
    return REPLContext(
        identity=Identity(user_id="u-1", display_name="Tester"),
        workspace_id="ws-1", workspace_name="WS", workspace_paths=[],
        workspace_instructions=None, session_id="s-1",
        redis_url="redis://localhost:6379/0",
    )


def _repl_with_mocks(first_event, result):
    r = REPL.__new__(REPL)
    r._ctx = _ctx()
    r._renderer = MagicMock()
    r._renderer.stream_live = AsyncMock()
    r._renderer.print_answer = MagicMock()
    r._renderer.print_error = MagicMock()
    r._renderer.thinking = MagicMock()
    r._sessions = MagicMock()
    r._sessions.append = MagicMock()

    redis = MagicMock()
    redis.push_task = AsyncMock()
    redis.get_result = AsyncMock(return_value=result)

    stream = MagicMock()
    stream.first = AsyncMock(return_value=first_event)
    stream.aclose = AsyncMock()
    redis.subscribe_events = MagicMock(return_value=stream)
    r._redis = redis
    return r, redis, stream


@pytest.mark.asyncio
async def test_send_task_streams_when_first_event_arrives():
    first = {"type": "turn.start", "task": "what is the answer?"}
    result = {"ok": True, "state": {"final_answer": "42"}}
    r, redis, stream = _repl_with_mocks(first, result)

    await r._send_task("what is the answer?")

    redis.push_task.assert_awaited_once()
    r._renderer.stream_live.assert_awaited_once_with(stream)
    stream.aclose.assert_awaited_once()
    r._renderer.print_answer.assert_called_once()
    assert r._renderer.print_answer.call_args.args[0] == "42"


@pytest.mark.asyncio
async def test_send_task_falls_back_when_no_event():
    result = {"ok": True, "state": {"final_answer": "fallback-answer"}}
    r, redis, stream = _repl_with_mocks(None, result)

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    r._renderer.thinking = MagicMock(return_value=cm)

    await r._send_task("hi")

    r._renderer.stream_live.assert_not_called()
    stream.aclose.assert_awaited_once()
    redis.get_result.assert_awaited()
    r._renderer.print_answer.assert_called_once()
    assert r._renderer.print_answer.call_args.args[0] == "fallback-answer"


@pytest.mark.asyncio
async def test_send_task_reports_error_result():
    result = {"ok": False, "error": "task_failed"}
    r, redis, stream = _repl_with_mocks(None, result)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    r._renderer.thinking = MagicMock(return_value=cm)

    await r._send_task("boom")

    r._renderer.print_error.assert_called_once_with("task_failed")


def test_first_event_timeout_constant_is_reasonable():
    assert 0 < FIRST_EVENT_TIMEOUT <= 5.0
