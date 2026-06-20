"""Tests for services/skill_worker/worker.py"""
from __future__ import annotations

import json
import socket
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import redis.asyncio as aioredis

from services.skill_worker.worker import (
    SkillWorker,
    STREAM,
    GROUP,
    RESULT_PREFIX,
    RESULT_TTL,
    _worker_id,
)
from services.skill_runner.skill_registry import SkillUnavailable


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_worker(**kwargs) -> SkillWorker:
    defaults = dict(
        redis_url="redis://localhost:6379/0",
        concurrency=2,
        worker_id="test-worker",
        call_timeout=30.0,
    )
    defaults.update(kwargs)
    return SkillWorker(**defaults)


def _payload(**kwargs) -> str:
    data = {"task_id": "t1", "skill": "ast-repo-map", "tool": "map_repository", "arguments": {}}
    data.update(kwargs)
    return json.dumps(data)


# ── _worker_id ─────────────────────────────────────────────────────────────────

def test_worker_id_default_contains_hostname_and_pid():
    wid = _worker_id()
    assert socket.gethostname() in wid
    assert str(os.getpid()) in wid


def test_worker_id_respects_env_var(monkeypatch):
    monkeypatch.setenv("WORKER_ID", "custom-worker")
    assert _worker_id() == "custom-worker"


# ── _write_result ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_result_sets_correct_key():
    worker = _make_worker()
    worker._redis = AsyncMock()
    await worker._write_result("abc-123", {"ok": True, "result": "data"})
    worker._redis.set.assert_awaited_once_with(
        f"{RESULT_PREFIX}abc-123",
        json.dumps({"ok": True, "result": "data"}),
        ex=RESULT_TTL,
    )


@pytest.mark.asyncio
async def test_write_result_publishes_ready():
    worker = _make_worker()
    worker._redis = AsyncMock()
    await worker._write_result("task-99", {"ok": False})
    worker._redis.publish.assert_awaited_once_with(
        f"{RESULT_PREFIX}task-99", "ready"
    )


# ── _dispatch ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_success():
    worker = _make_worker()
    registry = AsyncMock()
    registry.call_tool.return_value = {"content": [{"type": "text", "text": "hello"}]}
    worker._registry = registry

    result = await worker._dispatch({
        "task_id": "t1",
        "skill": "ast-repo-map",
        "tool": "map_repository",
        "arguments": {"repo_path": "/tmp"},
    })

    assert result["ok"] is True
    assert result["result"] == {"content": [{"type": "text", "text": "hello"}]}
    registry.call_tool.assert_awaited_once_with(
        "ast-repo-map.map_repository", {"repo_path": "/tmp"}
    )


@pytest.mark.asyncio
async def test_dispatch_skill_unavailable():
    worker = _make_worker()
    registry = AsyncMock()
    registry.call_tool.side_effect = SkillUnavailable("ast-repo-map.map_repository")
    worker._registry = registry

    result = await worker._dispatch({
        "task_id": "t2",
        "skill": "ast-repo-map",
        "tool": "map_repository",
        "arguments": {},
    })
    assert result["ok"] is False
    assert result["error"] == "skill_unavailable"
    assert "ast-repo-map.map_repository" in result["detail"]


@pytest.mark.asyncio
async def test_dispatch_generic_exception():
    worker = _make_worker()
    registry = AsyncMock()
    registry.call_tool.side_effect = RuntimeError("subprocess crashed")
    worker._registry = registry

    result = await worker._dispatch({
        "task_id": "t3",
        "skill": "bad-skill",
        "tool": "bad-tool",
        "arguments": {},
    })
    assert result["ok"] is False
    assert result["error"] == "dispatch_failed"
    assert "subprocess crashed" in result["detail"]


@pytest.mark.asyncio
async def test_dispatch_handles_iserror_flag():
    """Test that MCP CallToolResult with isError=True is reported as failure."""
    worker = _make_worker()
    registry = AsyncMock()

    # Create a mock MCP result with isError=True (a normal return, not an exception)
    mock_result = MagicMock()
    mock_result.isError = True
    mock_result.content = [{"type": "text", "text": "Tool failed"}]
    mock_result.model_dump = MagicMock(return_value={"isError": True, "content": [{"type": "text", "text": "Tool failed"}]})
    registry.call_tool.return_value = mock_result
    worker._registry = registry

    result = await worker._dispatch({
        "task_id": "t4",
        "skill": "test-skill",
        "tool": "test-tool",
        "arguments": {},
    })

    # Should be reported as failure, not success
    assert result["ok"] is False
    assert result["error"] == "tool_error"
    assert "content" in result["result"]


# ── _handle ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_acks_on_success():
    worker = _make_worker()
    worker._redis = AsyncMock()
    worker._registry = AsyncMock()
    worker._registry.call_tool.return_value = {}

    await worker._handle("1234-0", {"payload": _payload()})
    worker._redis.xack.assert_awaited_once_with(STREAM, GROUP, "1234-0")


@pytest.mark.asyncio
async def test_handle_acks_on_dispatch_error():
    worker = _make_worker()
    worker._redis = AsyncMock()
    worker._registry = AsyncMock()
    worker._registry.call_tool.side_effect = RuntimeError("boom")

    await worker._handle("5678-0", {"payload": _payload()})
    # Must ACK even on failure — no poison-pill infinite retry
    worker._redis.xack.assert_awaited_once_with(STREAM, GROUP, "5678-0")


@pytest.mark.asyncio
async def test_handle_acks_on_malformed_json():
    worker = _make_worker()
    worker._redis = AsyncMock()
    worker._registry = AsyncMock()

    await worker._handle("9999-0", {"payload": "NOT JSON {"})
    worker._redis.xack.assert_awaited_once_with(STREAM, GROUP, "9999-0")


@pytest.mark.asyncio
async def test_handle_writes_error_result_on_dispatch_failure():
    worker = _make_worker()
    worker._redis = AsyncMock()
    worker._registry = AsyncMock()
    worker._registry.call_tool.side_effect = RuntimeError("oops")

    await worker._handle("111-0", {"payload": _payload(task_id="err-task")})

    call_args = worker._redis.set.call_args
    stored = json.loads(call_args[0][1])
    assert stored["ok"] is False


# ── _ensure_group ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_group_creates_stream_if_absent():
    worker = _make_worker()
    worker._redis = AsyncMock()
    await worker._ensure_group()
    worker._redis.xgroup_create.assert_awaited_once_with(
        STREAM, GROUP, id="0", mkstream=True
    )


@pytest.mark.asyncio
async def test_ensure_group_ignores_busygroup_error():
    worker = _make_worker()
    worker._redis = AsyncMock()
    worker._redis.xgroup_create.side_effect = aioredis.ResponseError(
        "BUSYGROUP Consumer Group name already exists"
    )
    # Must not raise
    await worker._ensure_group()


@pytest.mark.asyncio
async def test_ensure_group_re_raises_other_errors():
    worker = _make_worker()
    worker._redis = AsyncMock()
    worker._redis.xgroup_create.side_effect = aioredis.ResponseError("WRONGTYPE")
    with pytest.raises(aioredis.ResponseError, match="WRONGTYPE"):
        await worker._ensure_group()


# ── safety checks ─────────────────────────────────────────────────────────────

def test_no_tiktoken_import():
    """worker.py must not directly import tiktoken."""
    import re
    from pathlib import Path
    text = (Path(__file__).parent.parent.parent.parent /
            "services/skill_worker/worker.py").read_text()
    assert re.search(r"^import tiktoken|^from tiktoken", text, re.MULTILINE) is None


def test_no_stdout_logging():
    import logging, sys
    # No handler in skill_worker logger should write to stdout
    logger = logging.getLogger("skill_worker")
    for handler in logging.root.handlers + logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            assert handler.stream is not sys.stdout, (
                "skill_worker must not log to stdout"
            )
