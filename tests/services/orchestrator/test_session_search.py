"""Tests for SessionSearch wrapper and StorageManager.search_turns."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.prompt_assembler import PromptAssembler
from services.orchestrator.session_search import SessionSearch
from tests.conftest import run_async

# ---------------------------------------------------------------------------
# Fake store helpers
# ---------------------------------------------------------------------------


class FakeStore:
    """Minimal stand-in for StorageManager.search_turns — no Mongo."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple] = []

    async def search_turns(
        self,
        query: str,
        top_k: int = 8,
        *,
        mode: str = "text",
        session_id: str | None = None,
    ) -> list[dict]:
        self.calls.append((query, top_k, mode, session_id))
        return self.rows[:top_k]


# ---------------------------------------------------------------------------
# SessionSearch unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_returns_formatted_snippets():
    store = FakeStore(
        [
            {"sessionId": "s1", "seq": 1, "text": "We decided to use Postgres for billing."},
            {"sessionId": "s2", "seq": 5, "text": "Retry budget is capped at 2."},
        ]
    )
    ss = SessionSearch(store)
    out = await ss.search("billing database")
    assert "[1] (session s1, turn 1)" in out
    assert "We decided to use Postgres for billing." in out
    assert "[2] (session s2, turn 5)" in out
    assert "Retry budget is capped at 2." in out


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_ranked_order_preserved():
    store = FakeStore(
        [
            {"sessionId": "s1", "seq": 1, "text": "first result"},
            {"sessionId": "s2", "seq": 2, "text": "second result"},
        ]
    )
    ss = SessionSearch(store)
    out = await ss.search("q")
    assert out.index("first result") < out.index("second result")


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_passes_k_through():
    store = FakeStore([{"sessionId": "s1", "seq": i, "text": f"turn {i}"} for i in range(20)])
    ss = SessionSearch(store, max_results=8)
    await ss.search("q", k=3)
    assert store.calls[-1][1] == 3


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_defaults_k_to_max_results():
    store = FakeStore([])
    ss = SessionSearch(store, max_results=5)
    await ss.search("q")
    assert store.calls[-1][1] == 5


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_clamps_k_to_twenty():
    store = FakeStore([])
    ss = SessionSearch(store)
    await ss.search("q", k=999)
    assert store.calls[-1][1] == 20


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_clamps_k_to_one():
    store = FakeStore([])
    ss = SessionSearch(store)
    await ss.search("q", k=0)
    assert store.calls[-1][1] == 1


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_passes_mode_text():
    store = FakeStore([])
    ss = SessionSearch(store)
    await ss.search("q", mode="text")
    assert store.calls[-1][2] == "text"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_passes_mode_regex():
    store = FakeStore([])
    ss = SessionSearch(store)
    await ss.search("foo.bar", mode="regex")
    assert store.calls[-1][2] == "regex"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_passes_session_id():
    store = FakeStore([])
    ss = SessionSearch(store)
    await ss.search("q", session_id="abc123")
    assert store.calls[-1][3] == "abc123"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_empty_results_return_sentinel():
    ss = SessionSearch(FakeStore([]))
    out = await ss.search("nothing here")
    assert out == "(no matching past turns)"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_blank_text_rows_filtered_then_sentinel():
    ss = SessionSearch(FakeStore([{"sessionId": "s1", "seq": 1, "text": "  "}]))
    out = await ss.search("q")
    assert out == "(no matching past turns)"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_total_output_capped():
    big = "x" * 5000
    store = FakeStore([{"sessionId": "s1", "seq": i, "text": big} for i in range(10)])
    ss = SessionSearch(store)
    out = await ss.search("q")
    assert len(out) <= 4000


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_per_snippet_capped():
    big = "y" * 5000
    store = FakeStore([{"sessionId": "s1", "seq": 1, "text": big}])
    ss = SessionSearch(store)
    out = await ss.search("q")
    # snippet body capped at 600 chars plus the prefix
    assert len(out) <= 700


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_store_error_returns_error_text_not_raise():
    class Boom:
        async def search_turns(self, query, top_k=8, *, mode="text", session_id=None):
            raise RuntimeError("mongo down")

    ss = SessionSearch(Boom())
    out = await ss.search("q")
    assert "session search failed" in out
    assert "mongo down" in out


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_none_store_returns_unavailable():
    ss = SessionSearch(None)
    out = await ss.search("q")
    assert out == "(session search not available)"


# ---------------------------------------------------------------------------
# Tool schema tests
# ---------------------------------------------------------------------------


def test_session_search_schema_present_when_enabled():
    assembler = PromptAssembler(session_search_enabled=True)
    names = {t["function"]["name"] for t in assembler.tools() if t.get("type") == "function"}
    assert "session_search" in names


def test_session_search_schema_absent_when_disabled():
    assembler = PromptAssembler(session_search_enabled=False)
    names = {t["function"]["name"] for t in assembler.tools() if t.get("type") == "function"}
    assert "session_search" not in names


def test_session_search_schema_has_required_query():
    assembler = PromptAssembler(session_search_enabled=True)
    tool = next(
        t
        for t in assembler.tools()
        if t.get("type") == "function" and t["function"]["name"] == "session_search"
    )
    params = tool["function"]["parameters"]
    assert "query" in params["properties"]
    assert "k" in params["properties"]
    assert "mode" in params["properties"]
    assert "session_id" in params["properties"]
    assert params["required"] == ["query"]


# ---------------------------------------------------------------------------
# db_indexes: ensure_indexes creates chat_turns text index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_ensure_indexes_creates_chat_turns_text_index(mock_mongo):
    """ensure_indexes must create a $text index on chat_turns."""
    from services.orchestrator.db_indexes import ensure_indexes

    db = mock_mongo[None]  # get the mock db

    await ensure_indexes(db)

    # chat_turns collection must have create_index called at least twice
    ct = db["chat_turns"]
    assert ct.create_index.await_count >= 2

    # One of the calls must be the text index
    calls = [str(c) for c in ct.create_index.call_args_list]
    assert any("text" in c for c in calls), f"No text index call found in: {calls}"


# ---------------------------------------------------------------------------
# AsyncOrchestrator dispatch: session_search tool
# ---------------------------------------------------------------------------


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


def _run(orch, goal, responses, captured):
    async def _emit(event_type, **kw):
        if event_type == "tool.done" and "result" in kw:
            captured.append(kw["result"])

    with (
        patch(
            "services.orchestrator.coding_orchestrator.acompletion_with_failover",
            new_callable=AsyncMock,
            side_effect=responses,
        ),
        patch("services.orchestrator.coding_orchestrator.events.emit", new=_emit),
    ):
        return run_async(orch.react_execute(goal))


@pytest.mark.mocked
def test_session_search_branch_returns_snippets_into_loop():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.session_search = SessionSearch(
        FakeStore(
            [
                {"sessionId": "s1", "seq": 3, "text": "We chose Postgres for billing."},
            ]
        )
    )
    captured: list[str] = []
    responses = [
        _tool_call_msg("session_search", {"query": "billing", "k": 5}),
        _tool_call_msg("finish", {"summary": "recalled"}),
    ]
    result = _run(orch, "what did we decide about billing", responses, captured)
    assert result["ok"] is True
    assert any("Postgres for billing" in c for c in captured)


@pytest.mark.mocked
def test_session_search_tool_absent_when_no_store():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    assert orch.session_search is None
    captured: list[str] = []
    responses = [
        _tool_call_msg("session_search", {"query": "x"}),
        _tool_call_msg("finish", {"summary": "done"}),
    ]
    _run(orch, "recall something", responses, captured)
    assert any("session search not available" in c for c in captured)


@pytest.mark.mocked
def test_session_search_defaults_to_none():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    assert orch.session_search is None
