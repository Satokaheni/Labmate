"""Tests for conversation continuity injection (Task 5).

These tests drive the REAL production code from services.orchestrator.
They would FAIL if the injection logic were removed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator import client_context
from services.orchestrator import graph as graph_mod
from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.skill_router import RouteResult


# ── ReAct loop fixture (scripted_completion) ────────────────────────────────
def _tool_call_msg(name: str, arguments_json: str):
    """Build a mock completion response with a single tool call."""
    tc = MagicMock()
    tc.id = "call-" + name
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments_json
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.fixture
def scripted_react_completion():
    """Patch litellm.acompletion to record every request body and return scripted responses.

    Step 1: run_bash (a cheap read-only tool).
    Step 2: finish.
    """
    responses = [
        _tool_call_msg("run_bash", '{"command":"ls"}'),
        _tool_call_msg("finish", '{"summary":"done"}'),
    ]
    bodies: list[dict] = []

    async def _fake_acompletion(*args, **kwargs):
        # Record messages and tools (the continuity injection goes into messages).
        bodies.append(
            {
                "messages": [dict(m) for m in kwargs["messages"]],
                "tools": kwargs.get("tools", []),
            }
        )
        return responses[min(len(bodies) - 1, len(responses) - 1)]

    with patch(
        "services.orchestrator.coding_orchestrator.litellm.acompletion",
        new=AsyncMock(side_effect=_fake_acompletion),
    ):
        yield bodies


# ── Direct-answer graph fixture ────────────────────────────────────────────
def _root_state_for_graph(description: str) -> dict:
    """Build a root state dict for graph.ainvoke."""
    return {
        "session_id": "s1",
        "root_goal": description,
        "goal_tree": {
            "root": {
                "id": "root",
                "parent_id": None,
                "children": [],
                "description": description,
                "status": "PENDING",
                "result": None,
                "error": None,
                "attempts": 0,
                "started_at": None,
                "updated_at": None,
            }
        },
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "final_answer": "",
    }


def _build_graph_with_direct_answer(monkeypatch):
    """Build the real graph with a fake orchestrator that supports direct-answer path."""
    from langgraph.checkpoint.memory import MemorySaver

    from services.orchestrator.coding_orchestrator import CodingOrchestrator

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    fake_router = MagicMock()
    # Route with no skills and no clarification -> direct-answer fast-path.
    fake_router.route = AsyncMock(
        return_value=RouteResult(
            skills=[],
            needs_clarification=False,
            sub_intents=["question"],
        )
    )
    fake_router.runner.catalog_prompt.return_value = "CATALOG"

    mock_orch = MagicMock(spec=CodingOrchestrator)
    # assess_ambiguity calls architect; return low-ambiguity JSON.
    mock_orch.architect = AsyncMock(
        return_value='{"assumptions": [], "ambiguity": 0.0, "blocking_question": ""}'
    )
    mock_orch.skill_router = fake_router
    # context_manager will be set by the test.

    real_cp = MemorySaver()
    with patch("services.orchestrator.graph._make_sqlite_checkpointer", return_value=real_cp):
        graph, _ = graph_mod.build_graph(mock_orch, MagicMock())
    return graph, mock_orch


# ────────────────────────────────────────────────────────────────────────────
# REACT LOOP TESTS — Drive the real _run_react_loop code
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_react_loop_injects_continuity_after_system_message(scripted_react_completion):
    """In _run_react_loop, continuity is injected between system message (index 0) and goal.

    This test DRIVES the REAL AsyncOrchestrator._run_react_loop code and FAILS if the
    injection at services/orchestrator/coding_orchestrator.py:537-551 is removed.
    """
    # Build a REAL AsyncOrchestrator.
    orch = AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)

    # Set up context_manager and _active_session_id (the injection guard checks both).
    orch.context_manager = MagicMock()
    orch.context_manager.conversation_context = AsyncMock(
        return_value="PRIOR: we discussed the traveling salesman problem.\nUser said it's NP-complete."
    )
    orch._active_session_id = "s1"

    # Run _run_react_loop directly (called by react_execute).
    result = await orch._run_react_loop("Is it NP-complete?", max_steps=3)

    # The recorded bodies list is populated by the mocked litellm.acompletion.
    bodies = scripted_react_completion

    # Verify the loop ran and completed.
    assert result["ok"] is True
    assert len(bodies) >= 2, "Expected at least 2 acompletion calls (goal + finish)"

    # The FIRST request body should carry the injected continuity.
    first_body = bodies[0]
    messages = first_body["messages"]

    # Verify structure: [system, continuity, goal].
    assert len(messages) >= 3, f"Expected >= 3 messages, got {len(messages)}: {messages}"
    assert messages[0]["role"] == "system", "First message must be system (index 0)"
    assert messages[1]["role"] == "user", "Second message should be continuity (index 1)"
    assert (
        "traveling salesman" in messages[1]["content"]
    ), f"Continuity message missing prior context: {messages[1]['content']}"
    assert (
        "CONVERSATION SO FAR" in messages[1]["content"]
    ), f"Continuity marker missing: {messages[1]['content']}"

    # The GOAL should come after continuity (at index 2 or later).
    goal_found = False
    for msg in messages[2:]:
        if "Is it NP-complete?" in msg.get("content", ""):
            goal_found = True
            break
    assert goal_found, f"Goal not found in messages after continuity: {messages[2:]}"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_react_loop_without_context_manager_no_injection(scripted_react_completion):
    """When context_manager is None, no continuity is injected.

    This test would PASS even if the injection code were deleted (because context_manager=None
    makes the guard skip injection), so it serves as a NEGATIVE TEST: it verifies that when
    conditions are NOT met, the behavior is correct.
    """
    orch = AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)

    # NO context_manager set (guard condition fails).
    orch._active_session_id = "s1"

    result = await orch._run_react_loop("What is 2+2?", max_steps=3)
    bodies = scripted_react_completion

    assert result["ok"] is True
    first_body = bodies[0]
    messages = first_body["messages"]

    # Without context_manager, messages should be [system, goal] only (NO continuity at index 1).
    assert (
        len(messages) == 2
    ), f"Expected 2 messages (no continuity), got {len(messages)}: {messages}"
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "What is 2+2?" in messages[1]["content"]
    assert (
        "CONVERSATION SO FAR" not in messages[1]["content"]
    ), f"Unexpected continuity in messages: {messages[1]['content']}"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_react_loop_with_empty_session_id_no_injection(scripted_react_completion):
    """When _active_session_id is empty, no continuity is injected (even if context_manager exists).

    Tests the SECOND guard condition: _active_session_id must be non-empty.
    """
    orch = AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)

    # Set context_manager but _active_session_id is empty (guard condition fails).
    orch.context_manager = MagicMock()
    orch.context_manager.conversation_context = AsyncMock(
        return_value="This should NOT be injected"
    )
    orch._active_session_id = ""  # Empty -> injection skipped.

    result = await orch._run_react_loop("Hello?", max_steps=3)
    bodies = scripted_react_completion

    assert result["ok"] is True
    first_body = bodies[0]
    messages = first_body["messages"]

    # With empty _active_session_id, no continuity should be injected.
    assert len(messages) == 2, f"Expected 2 messages, got {len(messages)}: {messages}"
    assert "This should NOT be injected" not in str(
        messages
    ), f"Continuity unexpectedly injected: {messages}"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_react_loop_continuity_injection_is_best_effort(scripted_react_completion):
    """When context_manager.conversation_context raises, the loop continues (best-effort).

    Tests the exception handler at services/orchestrator/coding_orchestrator.py:552-560.
    """
    orch = AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)

    # Set context_manager that fails.
    orch.context_manager = MagicMock()
    orch.context_manager.conversation_context = AsyncMock(side_effect=RuntimeError("Redis down"))
    orch._active_session_id = "s1"

    # The loop should NOT crash; it continues with bare messages.
    result = await orch._run_react_loop("What is 2+2?", max_steps=3)
    bodies = scripted_react_completion

    # The orchestrator should succeed even with the context_manager error.
    assert result["ok"] is True

    first_body = bodies[0]
    messages = first_body["messages"]

    # With the error, no continuity is injected -> [system, goal] only.
    assert len(messages) == 2, f"Expected 2 messages (error -> no injection), got {len(messages)}"
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "What is 2+2?" in messages[1]["content"]


# ────────────────────────────────────────────────────────────────────────────
# DIRECT-ANSWER PATH TESTS — Drive the real graph.py plan node code
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_direct_answer_injects_continuity_into_architect_prompt(monkeypatch):
    """In the graph.py plan node, direct-answer path injects continuity into architect() prompt.

    This test DRIVES the REAL graph (langgraph StateGraph) and injects a mocked orchestrator
    that records architect() calls. It would FAIL if the injection at
    services/orchestrator/graph.py:239-254 is removed.
    """
    graph, mock_orch = _build_graph_with_direct_answer(monkeypatch)

    # Set up context_manager on the MOCKED orchestrator.
    mock_orch.context_manager = MagicMock()
    mock_orch.context_manager.conversation_context = AsyncMock(
        return_value="CONTEXT: User asked about NP problems. Assistant explained TSP."
    )

    # Ensure no client doc-skills (so direct-answer path is taken).
    token = client_context.set_manifest(None)
    try:
        # The graph's plan node calls architect() with either the bare goal or the augmented
        # prompt (with continuity prepended).
        final_state = await graph.ainvoke(
            _root_state_for_graph("Is TSP NP-complete?"),
            {"configurable": {"thread_id": "t-direct-cont"}},
        )
    finally:
        client_context.reset_manifest(token)

    # Verify the graph took the direct-answer fast-path.
    assert (
        final_state.get("direct_answer") is True
    ), "Graph should take direct-answer path (no skill needed)"
    assert final_state.get("final_answer"), "direct-answer should produce a final_answer"

    # The CRITICAL assertion: architect() was called with a prompt that contains BOTH
    # the continuity block AND the goal. This proves injection happened.
    assert mock_orch.architect.called, "architect() should be called in direct-answer path"

    # Get the prompt argument to architect().
    architect_calls = mock_orch.architect.call_args_list
    assert (
        len(architect_calls) >= 2
    ), f"Expected >= 2 architect calls (assess_ambiguity + direct-answer), got {len(architect_calls)}"

    # assess_ambiguity calls architect() first (the triaging prompt).
    # Then the direct-answer plan node calls architect() AGAIN with the injected continuity.
    # The direct-answer call is the ONE with "NEW MESSAGE:" marker in the prompt.
    for call in architect_calls:
        prompt = call[0][0] if call[0] else ""  # First positional arg.
        if "NEW MESSAGE:" in prompt and "Is TSP NP-complete?" in prompt:
            # Found the direct-answer architect call (has the NEW MESSAGE: marker).
            assert (
                "CONVERSATION SO FAR" in prompt
            ), f"Direct-answer prompt missing continuity marker: {prompt[:500]}"
            assert (
                "NP problems" in prompt
            ), f"Direct-answer prompt missing prior context: {prompt[:500]}"
            return

    # If we get here, we didn't find the expected direct-answer call with injection.
    pytest.fail(
        f"Direct-answer architect call with 'NEW MESSAGE:' marker not found. Calls: {[c[0][0][:100] if c[0] else '' for c in architect_calls]}"
    )


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_direct_answer_without_context_manager_bare_prompt(monkeypatch):
    """When context_manager is None, direct-answer architect() gets bare goal (no injection).

    Negative test: ensures that when context_manager is absent, the code does NOT
    attempt injection and architect() gets only the bare goal.
    """
    graph, mock_orch = _build_graph_with_direct_answer(monkeypatch)

    # Do NOT set context_manager (it stays None).
    assert getattr(mock_orch, "context_manager", None) is None, "context_manager should not be set"

    token = client_context.set_manifest(None)
    try:
        final_state = await graph.ainvoke(
            _root_state_for_graph("what is 2+2"),
            {"configurable": {"thread_id": "t-direct-bare"}},
        )
    finally:
        client_context.reset_manifest(token)

    assert final_state.get("direct_answer") is True

    # architect() should have been called WITHOUT continuity injection.
    architect_calls = mock_orch.architect.call_args_list
    for call in architect_calls:
        prompt = call[0][0] if call[0] else ""
        if "what is 2+2" in prompt:
            # The prompt for the direct-answer call should be bare (no continuity marker).
            assert (
                "CONVERSATION SO FAR" not in prompt
            ), f"Bare prompt unexpectedly contains continuity: {prompt}"
            assert (
                "NEW MESSAGE:" not in prompt
            ), f"Bare prompt unexpectedly contains 'NEW MESSAGE:' marker: {prompt}"
            return

    pytest.fail(f"Direct-answer call not found in {architect_calls}")


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_direct_answer_continuity_injection_is_best_effort(monkeypatch):
    """When context_manager.conversation_context raises, direct-answer proceeds with bare prompt.

    Tests the exception handler at services/orchestrator/graph.py:252-254.
    The plan node should NOT crash; it continues with an unaugmented prompt.
    """
    graph, mock_orch = _build_graph_with_direct_answer(monkeypatch)

    # Set context_manager that fails.
    mock_orch.context_manager = MagicMock()
    mock_orch.context_manager.conversation_context = AsyncMock(
        side_effect=RuntimeError("Connection failed")
    )

    token = client_context.set_manifest(None)
    try:
        final_state = await graph.ainvoke(
            _root_state_for_graph("hello?"),
            {"configurable": {"thread_id": "t-direct-error"}},
        )
    finally:
        client_context.reset_manifest(token)

    # The graph should still complete despite the error.
    assert final_state.get("direct_answer") is True

    # architect() should have been called with the bare prompt (no continuity).
    architect_calls = mock_orch.architect.call_args_list
    for call in architect_calls:
        prompt = call[0][0] if call[0] else ""
        if "hello?" in prompt:
            # On error, the prompt should be bare.
            assert (
                "CONVERSATION SO FAR" not in prompt
            ), f"Error-case prompt unexpectedly contains continuity: {prompt}"
            return

    pytest.fail(f"Direct-answer call not found in {architect_calls}")


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_assess_ambiguity_receives_continuity(monkeypatch):
    """REGRESSION (live "Which problem?" bug): assess_ambiguity is the FIRST graph node —
    it runs BEFORE the plan (direct-answer) and execute (ReAct) paths. Without prior
    conversation it scores a follow-up like "is that problem NP-complete?" as ambiguous
    (unresolved referent 'that problem') and HALTS with a clarification before any answer
    path — and its continuity injection — ever runs.

    Drives the REAL assess_ambiguity node; asserts prior conversation reaches ITS
    architect() prompt. FAILS if continuity is not injected into assess_ambiguity.
    """
    graph, mock_orch = _build_graph_with_direct_answer(monkeypatch)
    mock_orch.context_manager = MagicMock()
    mock_orch.context_manager.conversation_context = AsyncMock(
        return_value="ASSISTANT: The Traveling Salesman Problem (TSP) is NP-hard."
    )

    token = client_context.set_manifest(None)
    try:
        await graph.ainvoke(
            _root_state_for_graph("Is that problem NP-complete?"),
            {"configurable": {"thread_id": "t-assess-continuity"}},
        )
    finally:
        client_context.reset_manifest(token)

    # The FIRST architect() call is assess_ambiguity's triage prompt — it MUST carry the
    # prior conversation so 'that problem' is resolvable (not flagged ambiguous → halt).
    assert mock_orch.architect.await_count >= 1
    assess_prompt = mock_orch.architect.call_args_list[0][0][0]
    assert "PRIOR CONVERSATION" in assess_prompt
    assert (
        "Traveling Salesman Problem" in assess_prompt
    ), f"assess_ambiguity prompt missing prior conversation: {assess_prompt[:600]}"
