"""Tests for services/orchestrator/main.py"""

from __future__ import annotations

import os
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator.inproc_bus import ResultRegistry
from services.orchestrator.main import (
    OrchestratorProcess,
    _build_mcp_params,
    _worker_id,
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


# ── submit_goal ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_goal_returns_task_id_and_enqueues():
    proc = OrchestratorProcess()
    task_id = await proc.submit_goal({"task": "x", "session_id": "s"})
    assert task_id
    assert proc._goal_queue.qsize() == 1
    queued = proc._goal_queue.get_nowait()
    assert queued["task_id"] == task_id
    assert queued["task"] == "x"
    assert queued["session_id"] == "s"


@pytest.mark.asyncio
async def test_submit_goal_preserves_explicit_task_id():
    proc = OrchestratorProcess()
    task_id = await proc.submit_goal({"task_id": "explicit-1", "task": "y"})
    assert task_id == "explicit-1"
    queued = proc._goal_queue.get_nowait()
    assert queued["task_id"] == "explicit-1"


# ── _write_result / ResultRegistry ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_result_delivers_via_result_registry():
    proc = OrchestratorProcess()
    await proc._write_result("task-abc", {"ok": True})
    result = await proc.results.wait_result("task-abc", timeout=1.0)
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_write_result_wait_before_set():
    """wait_result called before _write_result still resolves once the result lands."""
    proc = OrchestratorProcess.__new__(OrchestratorProcess)
    proc.results = ResultRegistry()

    async def _delayed_write():
        import asyncio

        await asyncio.sleep(0.01)
        await proc._write_result("task-late", {"ok": True, "state": {}})

    import asyncio

    waiter = asyncio.create_task(proc.results.wait_result("task-late", timeout=1.0))
    writer = asyncio.create_task(_delayed_write())
    result = await waiter
    await writer
    assert result["ok"] is True


# ── _handle ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_calls_run_task():
    proc = OrchestratorProcess()

    orch = AsyncMock()
    orch.run_task.return_value = {"session_id": "s1", "goal_tree": {}}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.load_agent_instructions = AsyncMock(return_value="")
    payload = {"task_id": "t1", "task": "do something", "session_id": "s1"}
    await proc._handle(payload, orch, storage)

    orch.run_task.assert_awaited_once_with(
        "do something", "s1", user_id="", workspace_id="", agent_instructions=""
    )


@pytest.mark.asyncio
async def test_handle_writes_error_result_on_failure():
    proc = OrchestratorProcess()

    orch = AsyncMock()
    orch.run_task.side_effect = RuntimeError("boom")

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    payload = {"task_id": "err-task", "task": "fail"}
    await proc._handle(payload, orch, storage)

    stored = await proc.results.wait_result("err-task", timeout=1.0)
    assert stored["ok"] is False


@pytest.mark.asyncio
async def test_handle_uses_task_id_as_session_id_when_absent():
    proc = OrchestratorProcess()

    orch = AsyncMock()
    orch.run_task.return_value = {}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.load_agent_instructions = AsyncMock(return_value="")

    # No session_id in payload — should fall back to task_id
    payload = {"task_id": "standalone-task", "task": "do it"}
    await proc._handle(payload, orch, storage)

    orch.run_task.assert_awaited_once_with(
        "do it", "standalone-task", user_id="", workspace_id="", agent_instructions=""
    )


# ── mcp_client_manager shim ───────────────────────────────────────────────────


def test_mcp_client_manager_importable_from_orchestrator():
    from services.orchestrator.mcp_client_manager import CircuitOpenError, MCPClientManager

    assert MCPClientManager is not None
    assert CircuitOpenError is not None


# ── safety ────────────────────────────────────────────────────────────────────


def test_no_tiktoken_import():
    """main.py must not directly import tiktoken (litellm does as a side-effect)."""
    import re
    from pathlib import Path

    text = (
        Path(__file__).parent.parent.parent.parent / "services/orchestrator/main.py"
    ).read_text()
    assert re.search(r"^import tiktoken|^from tiktoken", text, re.MULTILINE) is None


def test_logging_goes_to_stderr_not_stdout():
    import logging
    import sys

    logger = logging.getLogger("orchestrator")
    for handler in logging.root.handlers + logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            assert handler.stream is not sys.stdout


# ── _handle with user_id and workspace_id ─────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_parses_user_and_workspace():
    """_handle extracts user_id and workspace_id from payload."""
    proc = OrchestratorProcess()

    orch = AsyncMock()
    orch.run_task.return_value = {"session_id": "s-1", "goal_tree": {}}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.get_workspace = AsyncMock(return_value=None)
    storage.workspaces._db = AsyncMock()
    storage.workspaces._db.__getitem__ = MagicMock(return_value=AsyncMock())

    payload = {
        "task_id": "t-1",
        "task": "do something",
        "session_id": "s-1",
        "user_id": "u-abc",
        "workspace_id": "ws-xyz",
    }
    await proc._handle(payload, orch, storage)

    call_kwargs = orch.run_task.call_args.kwargs
    assert call_kwargs.get("user_id") == "u-abc"
    assert call_kwargs.get("workspace_id") == "ws-xyz"


@pytest.mark.asyncio
async def test_handle_defaults_missing_user_workspace():
    """Missing user_id/workspace_id default to empty string without error."""
    proc = OrchestratorProcess()

    orch = AsyncMock()
    orch.run_task.return_value = {"session_id": "s-2", "goal_tree": {}}

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()

    payload = {"task_id": "t-2", "task": "hi", "session_id": "s-2"}
    await proc._handle(payload, orch, storage)

    call_kwargs = orch.run_task.call_args.kwargs
    assert call_kwargs.get("user_id") == ""
    assert call_kwargs.get("workspace_id") == ""


@pytest.mark.asyncio
async def test_complete_session_called_with_ok_true_on_success():
    """complete_session must record ok=True when task succeeds (error is None)."""
    proc = OrchestratorProcess()

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

    payload = {
        "task_id": "t-ok",
        "task": "succeed",
        "session_id": "s-ok",
        "user_id": "u-1",
        "workspace_id": "ws-1",
    }
    await proc._handle(payload, orch, storage)

    # complete_session must be called with ok=True (because error is None)
    storage.workspaces.complete_session.assert_awaited()
    call_args = storage.workspaces.complete_session.call_args
    # Check that ok=True was passed (either as kwarg or positional arg)
    ok_value = call_args.kwargs.get("ok")
    if ok_value is None and len(call_args.args) > 1:
        ok_value = call_args.args[1]
    assert ok_value is True


@pytest.mark.asyncio
async def test_complete_session_called_with_ok_false_on_run_task_exception():
    """complete_session must record ok=False when run_task raises an exception."""
    proc = OrchestratorProcess()

    orch = AsyncMock()
    # Task fails with exception
    orch.run_task.side_effect = RuntimeError("boom")

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.get_workspace = AsyncMock(return_value=None)
    storage.workspaces._db = AsyncMock()
    storage.workspaces._db.__getitem__ = MagicMock(return_value=AsyncMock())

    payload = {
        "task_id": "t-fail",
        "task": "fail",
        "session_id": "s-fail",
        "user_id": "u-2",
        "workspace_id": "ws-2",
    }
    await proc._handle(payload, orch, storage)

    # complete_session must be called with ok=False (because run_task raised)
    storage.workspaces.complete_session.assert_awaited()
    call_args = storage.workspaces.complete_session.call_args
    # Check that ok=False was passed (either as kwarg or positional arg)
    ok_value = call_args.kwargs.get("ok")
    if ok_value is None and len(call_args.args) > 1:
        ok_value = call_args.args[1]
    assert ok_value is False


@pytest.mark.asyncio
async def test_write_result_ok_false_when_final_state_has_error():
    """FIX #2: _write_result derives ok=False from final_state.error (not exception)."""
    proc = OrchestratorProcess()

    orch = AsyncMock()
    # Graph finalized with FAILED root and error set (no exception raised)
    orch.run_task.return_value = {
        "final_answer": "Task failed with subtask errors",
        "error": "1 subtask(s) failed: subtask 1 (error: worker error: bad thing)",
        "goal_tree": {"root": {"status": "FAILED"}},
    }

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.get_workspace = AsyncMock(return_value=None)
    storage.workspaces._db = AsyncMock()
    storage.workspaces._db.__getitem__ = MagicMock(return_value=AsyncMock())

    payload = {
        "task_id": "t-graph-failed",
        "task": "subtask fails",
        "session_id": "s-graph-failed",
        "user_id": "u-3",
        "workspace_id": "ws-3",
    }
    await proc._handle(payload, orch, storage)

    # result must be ok=False (derived from error != None)
    result = await proc.results.wait_result("t-graph-failed", timeout=1.0)
    assert result["ok"] is False, "result should have ok=False when final_state.error is set"

    # complete_session must also record ok=False
    storage.workspaces.complete_session.assert_awaited()
    call_args = storage.workspaces.complete_session.call_args
    ok_value = call_args.kwargs.get("ok")
    if ok_value is None and len(call_args.args) > 1:
        ok_value = call_args.args[1]
    assert ok_value is False


# ── skill router wiring ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_emits_turn_start_and_done():
    import asyncio

    proc = OrchestratorProcess()

    orch = MagicMock()
    orch.run_task = AsyncMock(return_value={"final_answer": "done", "error": None})
    orch.stream_final_answer = AsyncMock(return_value="done")
    storage = MagicMock()
    storage.workspaces = MagicMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.upsert_workspace = AsyncMock()

    # Events now travel over the in-process bus instead of Redis xadd.
    sub = proc.bus.subscribe("events:t-1")
    payload = {"task_id": "t-1", "task": "do it", "session_id": "t-1"}
    await proc._handle(payload, orch, storage)

    types = []
    while True:
        try:
            evt = await asyncio.wait_for(sub.__anext__(), timeout=0.2)
        except TimeoutError:
            break
        types.append(evt["type"])
    sub.close()

    # agent_status (active) is emitted first, then turn.start
    assert types[0] == "agent_status"
    assert "turn.start" in types
    assert "turn.done" in types
