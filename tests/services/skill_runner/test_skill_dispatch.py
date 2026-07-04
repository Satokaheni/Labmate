"""Tests for services/skill_runner/skill_dispatch.py.

dispatch() is the transport-free dispatch core extracted from
services/skill_worker/worker.py's _dispatch/_jsonable — it runs one skill
tool via a SkillRegistry and shapes the result into the
{"ok": bool, "result"|"error": ...} contract that both the (retired) Redis
worker and the in-process SkillRouter depend on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.skill_runner.skill_dispatch import dispatch
from services.skill_runner.skill_registry import SkillUnavailable


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_dispatch_success_shapes_result():
    registry = AsyncMock()
    registry.call_tool.return_value = {"content": [{"type": "text", "text": "hello"}]}

    result = await dispatch(
        registry,
        {
            "task_id": "t1",
            "skill": "ast-repo-map",
            "tool": "map_repository",
            "arguments": {"repo_path": "/tmp"},
        },
    )

    assert result["ok"] is True
    assert result["result"] == {"content": [{"type": "text", "text": "hello"}]}
    registry.call_tool.assert_awaited_once_with(
        "ast-repo-map.map_repository", {"repo_path": "/tmp"}
    )


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_dispatch_skill_unavailable():
    registry = AsyncMock()
    registry.call_tool.side_effect = SkillUnavailable("ast-repo-map.map_repository")

    result = await dispatch(
        registry,
        {"task_id": "t2", "skill": "ast-repo-map", "tool": "map_repository", "arguments": {}},
    )

    assert result["ok"] is False
    assert result["error"] == "skill_unavailable"
    assert "ast-repo-map.map_repository" in result["detail"]


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_dispatch_generic_exception():
    registry = AsyncMock()
    registry.call_tool.side_effect = RuntimeError("subprocess crashed")

    result = await dispatch(
        registry,
        {"task_id": "t3", "skill": "bad-skill", "tool": "bad-tool", "arguments": {}},
    )

    assert result["ok"] is False
    assert result["error"] == "dispatch_failed"
    assert "subprocess crashed" in result["detail"]


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_dispatch_handles_iserror_flag():
    """An MCP CallToolResult with isError=True is a normal return, not an exception."""
    registry = AsyncMock()
    mock_result = MagicMock()
    mock_result.isError = True
    mock_result.content = [{"type": "text", "text": "Tool failed"}]
    mock_result.model_dump = MagicMock(
        return_value={"isError": True, "content": [{"type": "text", "text": "Tool failed"}]}
    )
    registry.call_tool.return_value = mock_result

    result = await dispatch(
        registry,
        {"task_id": "t4", "skill": "test-skill", "tool": "test-tool", "arguments": {}},
    )

    assert result["ok"] is False
    assert result["error"] == "tool_error"
    assert "content" in result["result"]


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_dispatch_jsonables_pydantic_style_result():
    """A result with model_dump() (pydantic-style CallToolResult) is dumped to plain JSON."""
    registry = AsyncMock()
    mock_result = MagicMock()
    mock_result.isError = False
    mock_result.model_dump = MagicMock(return_value={"content": [{"type": "text", "text": "ok"}]})
    registry.call_tool.return_value = mock_result

    result = await dispatch(
        registry,
        {"task_id": "t5", "skill": "test-skill", "tool": "test-tool", "arguments": {}},
    )

    assert result["ok"] is True
    assert result["result"] == {"content": [{"type": "text", "text": "ok"}]}


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_dispatch_falls_back_to_str_for_unserializable_result():
    """A non-pydantic, non-JSON-serializable result is stringified rather than raising."""
    registry = AsyncMock()

    class Unserializable:
        def __str__(self):
            return "<unserializable>"

    registry.call_tool.return_value = Unserializable()

    result = await dispatch(
        registry,
        {"task_id": "t6", "skill": "test-skill", "tool": "test-tool", "arguments": {}},
    )

    assert result["ok"] is True
    assert result["result"] == "<unserializable>"
