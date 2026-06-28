"""Step definitions for the memory_search tool BDD feature."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.memory_search import MemorySearch
from services.orchestrator.prompt_assembler import PromptAssembler
from tests.conftest import run_async

scenarios("features/memory_search.feature")


# ── helpers ──────────────────────────────────────────────────────────────────

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


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows

    async def search_memories(self, query, top_k=5):
        return self.rows[:top_k]


@pytest.fixture
def ctx():
    return {"responses": [], "result": None, "tool_results": [], "assembler": None}


def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(_tool_call_msg("finish", {"summary": "filler"}))


# ── Given: orchestrator construction ─────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and no memory store")
def _orch_no_store(ctx):
    ctx["orch"] = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")


@given("an AsyncOrchestrator with no skill router and a memory store")
def _orch_with_store(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.memory_search = MemorySearch(_FakeStore([]))  # rows set by subsequent steps
    ctx["orch"] = orch
    ctx["snippets"] = []  # accumulator for snippet rows


@given(parsers.parse('the memory store returns the snippet "{fact}"'))
def _store_snippet(ctx, fact):
    # Accumulate snippets into the context
    row = {"id": str(len(ctx["snippets"]) + 1), "fact": fact, "raw_fact": fact, "metadata": {}, "distance": 0.0}
    ctx["snippets"].append(row)
    # Update the orchestrator's memory_search to use the accumulated snippets
    ctx["orch"].memory_search = MemorySearch(_FakeStore(ctx["snippets"]))


# ── Given: scripted model turns ──────────────────────────────────────────────

@given(parsers.parse('the model calls memory_search with query "{query}" on turn {turn:d}'))
def _memory_search_turn(ctx, query, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("memory_search", {"query": query})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


# ── When: tool-list build ────────────────────────────────────────────────────

@when("the prompt assembler builds the tool list with memory disabled")
def _build_disabled(ctx):
    ctx["assembler"] = PromptAssembler(skill_router=None, memory_enabled=False)


@when("the prompt assembler builds the tool list with memory enabled")
def _build_enabled(ctx):
    ctx["assembler"] = PromptAssembler(skill_router=None, memory_enabled=True)


# ── When: run the loop ───────────────────────────────────────────────────────

@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run_goal(ctx, goal):
    captured: list[str] = []

    async def _emit(event_type, **kw):
        if event_type == "tool.done" and "result" in kw:
            captured.append(kw["result"])

    with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
               new_callable=AsyncMock, side_effect=ctx["responses"]), \
         patch("services.orchestrator.coding_orchestrator.events.emit", new=_emit):
        ctx["result"] = run_async(ctx["orch"].react_execute(goal))
    ctx["tool_results"] = captured


# ── Then: tool-list assertions ───────────────────────────────────────────────

@then(parsers.parse('the tool list contains a tool named "{name}"'))
def _tool_has(ctx, name):
    names = [t["function"]["name"] for t in ctx["assembler"].tools()]
    assert name in names


@then(parsers.parse('the tool list does not contain a tool named "{name}"'))
def _tool_absent(ctx, name):
    names = [t["function"]["name"] for t in ctx["assembler"].tools()]
    assert name not in names


@then(parsers.parse('the memory_search tool has a "{param}" parameter'))
def _tool_param(ctx, param):
    schema = next(t for t in ctx["assembler"].tools() if t["function"]["name"] == "memory_search")
    assert param in schema["function"]["parameters"]["properties"]


# ── Then: loop result assertions ─────────────────────────────────────────────

@then(parsers.parse('the memory_search tool result contains "{needle}"'))
def _result_contains(ctx, needle):
    joined = "\n".join(ctx["tool_results"])
    assert needle in joined, f"{needle!r} not in {joined!r}"
