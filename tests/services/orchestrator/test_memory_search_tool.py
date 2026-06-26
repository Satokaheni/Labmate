from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.memory_search import MemorySearch
from tests.conftest import run_async


def _tool_call_msg(name: str, arguments: dict):
    tc = MagicMock()
    tc.id = f"call-{name}"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    async def search_memories(self, query, top_k=5):
        return self.rows[:top_k]


def _run(orch, goal, responses, captured):
    async def _emit(event_type, **kw):
        if event_type == "tool.done" and "result" in kw:
            captured.append(kw["result"])

    with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
               new_callable=AsyncMock, side_effect=responses), \
         patch("services.orchestrator.coding_orchestrator.events.emit", new=_emit):
        return run_async(orch.react_execute(goal))


@pytest.mark.mocked
def test_memory_search_branch_returns_raw_snippets_into_loop():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.memory_search = MemorySearch(FakeStore([
        {"id": "1", "fact": "We chose Postgres over Mongo for billing.", "raw_fact": "", "metadata": {}, "distance": 0.1},
    ]))
    captured: list[str] = []
    responses = [
        _tool_call_msg("memory_search", {"query": "billing database", "k": 5}),
        _tool_call_msg("finish", {"summary": "recalled"}),
    ]
    result = _run(orch, "continue billing", responses, captured)
    assert result["ok"] is True
    assert any("Postgres over Mongo" in c for c in captured)


@pytest.mark.mocked
def test_memory_search_tool_absent_when_no_store():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    assert orch.memory_search is None
    # Tool not advertised: a model that nonetheless names it gets a clear error,
    # never a crash.
    captured: list[str] = []
    responses = [
        _tool_call_msg("memory_search", {"query": "x"}),
        _tool_call_msg("finish", {"summary": "done"}),
    ]
    _run(orch, "recall something", responses, captured)
    assert any("memory search not available" in c for c in captured)
