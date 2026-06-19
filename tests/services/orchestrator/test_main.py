"""Tests for services/orchestrator/main.py"""
from __future__ import annotations

import asyncio
import json
import os
import socket

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import redis.asyncio as aioredis

from services.orchestrator.main import (
    OrchestratorProcess,
    GOALS_STREAM,
    GOALS_GROUP,
    RESULT_PREFIX,
    RESULT_TTL,
    _worker_id,
    _build_mcp_params,
)


# ── _worker_id ─────────────────────────────────────────────────────────────────

def test_worker_id_contains_hostname_and_pid():
    wid = _worker_id()
    assert socket.gethostname() in wid
    assert str(os.getpid()) in wid


# ── _build_mcp_params ──────────────────────────────────────────────────────────

def test_build_mcp_params_default_command():
    params = _build_mcp_params()
    assert params.command == "node"


def test_build_mcp_params_env_override(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_CMD", "deno")
    monkeypatch.setenv("MCP_BRIDGE_ARGS", "/custom/index.js")
    params = _build_mcp_params()
    assert params.command == "deno"
    assert params.args == ["/custom/index.js"]


def test_build_mcp_params_default_args_point_to_dist():
    params = _build_mcp_params()
    assert params.args[0].endswith("dist/index.js")
    assert "mcp-bridge" in params.args[0]


# ── _write_result ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_result_sets_key_with_ttl():
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()
    await proc._write_result("task-abc", {"ok": True})
    proc._redis.set.assert_awaited_once_with(
        f"{RESULT_PREFIX}task-abc",
        json.dumps({"ok": True}),
        ex=RESULT_TTL,
    )


@pytest.mark.asyncio
async def test_write_result_publishes_ready():
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()
    await proc._write_result("task-abc", {"ok": True})
    proc._redis.publish.assert_awaited_once_with(
        f"{RESULT_PREFIX}task-abc", "ready"
    )


# ── _ensure_group ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_group_creates_stream():
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()
    await proc._ensure_group()
    proc._redis.xgroup_create.assert_awaited_once_with(
        GOALS_STREAM, GOALS_GROUP, id="0", mkstream=True,
    )


@pytest.mark.asyncio
async def test_ensure_group_ignores_busygroup():
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()
    proc._redis.xgroup_create.side_effect = aioredis.ResponseError(
        "BUSYGROUP Consumer Group name already exists"
    )
    await proc._ensure_group()  # must not raise


@pytest.mark.asyncio
async def test_ensure_group_re_raises_other_errors():
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()
    proc._redis.xgroup_create.side_effect = aioredis.ResponseError("WRONGTYPE")
    with pytest.raises(aioredis.ResponseError, match="WRONGTYPE"):
        await proc._ensure_group()


# ── _handle ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_calls_run_task_and_acks():
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {"session_id": "s1", "goal_tree": {}}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()

    payload = json.dumps({"task_id": "t1", "task": "do something", "session_id": "s1"})
    await proc._handle("100-0", {"payload": payload}, orch, storage)

    orch.run_task.assert_awaited_once_with("do something", "s1", user_id="", workspace_id="")
    proc._redis.xack.assert_awaited_once_with(GOALS_STREAM, GOALS_GROUP, "100-0")


@pytest.mark.asyncio
async def test_handle_acks_on_failure():
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.side_effect = RuntimeError("graph exploded")

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()

    payload = json.dumps({"task_id": "t2", "task": "fail", "session_id": "s2"})
    await proc._handle("200-0", {"payload": payload}, orch, storage)

    # ACK must happen even on failure
    proc._redis.xack.assert_awaited_once_with(GOALS_STREAM, GOALS_GROUP, "200-0")


@pytest.mark.asyncio
async def test_handle_writes_error_result_on_failure():
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.side_effect = RuntimeError("boom")

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()

    payload = json.dumps({"task_id": "err-task", "task": "fail"})
    await proc._handle("300-0", {"payload": payload}, orch, storage)

    set_args = proc._redis.set.call_args[0]
    stored = json.loads(set_args[1])
    assert stored["ok"] is False


@pytest.mark.asyncio
async def test_handle_uses_task_id_as_session_id_when_absent():
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()

    # No session_id in payload — should fall back to task_id
    payload = json.dumps({"task_id": "standalone-task", "task": "do it"})
    await proc._handle("400-0", {"payload": payload}, orch, storage)

    orch.run_task.assert_awaited_once_with("do it", "standalone-task", user_id="", workspace_id="")


# ── mcp_client_manager shim ───────────────────────────────────────────────────

def test_mcp_client_manager_importable_from_orchestrator():
    from services.orchestrator.mcp_client_manager import MCPClientManager, CircuitOpenError
    assert MCPClientManager is not None
    assert CircuitOpenError is not None


# ── safety ────────────────────────────────────────────────────────────────────

def test_no_tiktoken_import():
    """main.py must not directly import tiktoken (litellm does as a side-effect)."""
    import re
    from pathlib import Path
    text = (Path(__file__).parent.parent.parent.parent /
            "services/orchestrator/main.py").read_text()
    assert re.search(r"^import tiktoken|^from tiktoken", text, re.MULTILINE) is None


def test_logging_goes_to_stderr_not_stdout():
    import logging, sys
    logger = logging.getLogger("orchestrator")
    for handler in logging.root.handlers + logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            assert handler.stream is not sys.stdout


# ── _handle with user_id and workspace_id ─────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_parses_user_and_workspace():
    """_handle extracts user_id and workspace_id from payload."""
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {"session_id": "s-1", "goal_tree": {}}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.get_workspace = AsyncMock(return_value=None)
    storage.workspaces._db = AsyncMock()
    storage.workspaces._db.__getitem__ = MagicMock(return_value=AsyncMock())

    payload = json.dumps({
        "task_id": "t-1",
        "task": "do something",
        "session_id": "s-1",
        "user_id": "u-abc",
        "workspace_id": "ws-xyz",
    })
    await proc._handle("msg-1", {"payload": payload}, orch, storage)

    call_kwargs = orch.run_task.call_args.kwargs
    assert call_kwargs.get("user_id") == "u-abc"
    assert call_kwargs.get("workspace_id") == "ws-xyz"


@pytest.mark.asyncio
async def test_handle_defaults_missing_user_workspace():
    """Missing user_id/workspace_id default to empty string without error."""
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    orch.run_task.return_value = {"session_id": "s-2", "goal_tree": {}}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()

    payload = json.dumps({"task_id": "t-2", "task": "hi", "session_id": "s-2"})
    await proc._handle("msg-2", {"payload": payload}, orch, storage)

    call_kwargs = orch.run_task.call_args.kwargs
    assert call_kwargs.get("user_id") == ""
    assert call_kwargs.get("workspace_id") == ""


@pytest.mark.asyncio
async def test_handle_acks_malformed_payload():
    """Malformed JSON payload must still be acked (no poison pill)."""
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()

    # Malformed JSON that will fail json.loads()
    fields = {"payload": "not-json"}
    await proc._handle("msg-bad", fields, orch, storage)

    # xack must have been called even though parsing failed
    proc._redis.xack.assert_awaited_once_with(GOALS_STREAM, GOALS_GROUP, "msg-bad")


@pytest.mark.asyncio
async def test_complete_session_called_with_ok_true_on_success():
    """complete_session must record ok=True when task succeeds (error is None)."""
    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    orch = AsyncMock()
    # Task succeeds with error: None
    orch.run_task.return_value = {"final_answer": "done", "error": None}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.get_workspace = AsyncMock(return_value=None)
    storage.workspaces._db = AsyncMock()
    storage.workspaces._db.__getitem__ = MagicMock(return_value=AsyncMock())

    payload = json.dumps({
        "task_id": "t-ok",
        "task": "succeed",
        "session_id": "s-ok",
        "user_id": "u-1",
        "workspace_id": "ws-1",
    })
    await proc._handle("msg-ok", fields={"payload": payload}, orch=orch, storage=storage)

    # complete_session must be called with ok=True (because error is None)
    storage.workspaces.complete_session.assert_awaited()
    call_args = storage.workspaces.complete_session.call_args
    # Check that ok=True was passed (either as kwarg or positional arg)
    ok_value = call_args.kwargs.get("ok")
    if ok_value is None and len(call_args.args) > 1:
        ok_value = call_args.args[1]
    assert ok_value is True
