from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import services.orchestrator.main as m
from services.orchestrator.main import OrchestratorProcess


def _bare_proc():
    """Bypass __init__ (heavy); set only what _run_engine touches."""
    proc = OrchestratorProcess.__new__(OrchestratorProcess)
    proc.async_orch = MagicMock()
    proc.signals = MagicMock()
    return proc


@pytest.mark.asyncio
async def test_run_engine_defaults_to_graph(monkeypatch):
    """Default ORCHESTRATOR_ENGINE should be 'graph' (LangGraph path)."""
    monkeypatch.delenv("ORCHESTRATOR_ENGINE", raising=False)
    proc = _bare_proc()
    orch = MagicMock()
    orch.run_task = AsyncMock(return_value={"ok": True, "error": None})
    out = await proc._run_engine(
        orch,
        "do it",
        "sess",
        user_id="u",
        workspace_id="w",
        agent_instructions="inst",
        store=None,
    )
    orch.run_task.assert_awaited_once()
    assert out == {"ok": True, "error": None}


@pytest.mark.asyncio
async def test_run_engine_lite_routes_to_run_goal_lite(monkeypatch):
    """ORCHESTRATOR_ENGINE=lite should route to run_goal_lite."""
    monkeypatch.setenv("ORCHESTRATOR_ENGINE", "lite")
    proc = _bare_proc()
    orch = MagicMock()
    orch.run_task = AsyncMock()
    captured = {}

    async def fake_lite(o, ao, task, sess, **kw):
        captured.update(task=task, sess=sess, kw=kw)
        return {"ok": True}

    monkeypatch.setattr(m, "run_goal_lite", fake_lite)
    out = await proc._run_engine(
        orch,
        "do it",
        "sess",
        user_id="u",
        workspace_id="w",
        agent_instructions="inst",
        store="STORE",
    )
    orch.run_task.assert_not_called()
    assert captured["task"] == "do it" and captured["sess"] == "sess"
    assert captured["kw"]["store"] == "STORE"
    assert out["error"] is None  # ok normalized to error=None


@pytest.mark.asyncio
async def test_run_engine_lite_failure_surfaces_error(monkeypatch):
    """Lite engine failure (ok=False) should surface an error key."""
    monkeypatch.setenv("ORCHESTRATOR_ENGINE", "lite")
    proc = _bare_proc()

    async def fake_lite(*a, **k):
        return {"ok": False}

    monkeypatch.setattr(m, "run_goal_lite", fake_lite)
    out = await proc._run_engine(
        MagicMock(),
        "t",
        "s",
        user_id="",
        workspace_id="",
        agent_instructions="",
        store=None,
    )
    assert out["error"] is not None  # failed goal surfaces an error for ok_flag


@pytest.mark.asyncio
async def test_run_engine_lite_clarification_untouched(monkeypatch):
    """Lite engine clarification path should not be error-normalized."""
    monkeypatch.setenv("ORCHESTRATOR_ENGINE", "lite")
    proc = _bare_proc()

    async def fake_lite(*a, **k):
        return {
            "awaiting_clarification": True,
            "clarification_question": "which file?",
            "ok": False,
        }

    monkeypatch.setattr(m, "run_goal_lite", fake_lite)
    out = await proc._run_engine(
        MagicMock(),
        "t",
        "s",
        user_id="",
        workspace_id="",
        agent_instructions="",
        store=None,
    )
    assert out.get("awaiting_clarification") is True
    assert "error" not in out  # clarification path is not error-normalized
