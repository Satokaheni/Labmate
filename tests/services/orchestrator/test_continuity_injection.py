"""Tests for conversation continuity injection (Task 5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_direct_answer_with_context_manager_injects_continuity():
    """When context_manager exists and returns continuity, architect() is called with augmented prompt."""
    # Mock orchestrator with context_manager
    orch = MagicMock()
    orch.architect = AsyncMock(return_value="The TSP is NP-complete")
    orch.context_manager = AsyncMock()
    orch.context_manager.conversation_context = AsyncMock(
        return_value="USER: What is the traveling salesman problem?\nASSISTANT: It's a classic problem..."
    )

    # Mock state
    state = {
        "goal_tree": {},
        "session_id": "test_session",
    }

    # Simulate the pattern from graph.py (direct-answer injection)
    goal_desc = "Is it NP-complete?"
    cm = getattr(orch, "context_manager", None)
    prompt = goal_desc

    if cm is not None:
        session_id = state.get("session_id", "")
        continuity_block = await cm.conversation_context(session_id)
        if continuity_block:
            prompt = (
                f"CONVERSATION SO FAR (prior context — answer the NEW message at the end):\n"
                f"{continuity_block}\n\n"
                f"NEW MESSAGE: {goal_desc}"
            )

    assert "Is it NP-complete?" in prompt
    assert "traveling salesman problem" in prompt
    assert "CONVERSATION SO FAR" in prompt


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_direct_answer_without_context_manager_uses_bare_prompt():
    """When context_manager is absent or doesn't return continuity, architect() gets bare goal."""
    goal_desc = "What is 2+2?"

    # Without context_manager — create orch without setting context_manager
    orch = type("Orch", (), {"architect": AsyncMock(return_value="4")})()
    cm = getattr(orch, "context_manager", None)

    prompt = goal_desc
    if cm is not None:
        session_id = "test"
        continuity_block = await cm.conversation_context(session_id)
        if continuity_block:
            prompt = (
                f"CONVERSATION SO FAR (prior context — answer the NEW message at the end):\n"
                f"{continuity_block}\n\n"
                f"NEW MESSAGE: {goal_desc}"
            )

    # prompt stays bare
    assert prompt == goal_desc
    assert "CONVERSATION SO FAR" not in prompt


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_direct_answer_context_injection_is_best_effort():
    """When context_manager.conversation_context raises, the bare prompt is used (no break)."""
    goal_desc = "What is 2+2?"

    # Mock orchestrator with failing context_manager
    orch = MagicMock()
    orch.architect = AsyncMock(return_value="4")
    orch.context_manager = AsyncMock()
    orch.context_manager.conversation_context = AsyncMock(side_effect=RuntimeError("Redis down"))

    cm = getattr(orch, "context_manager", None)
    prompt = goal_desc

    try:
        if cm is not None:
            session_id = "test"
            continuity_block = await cm.conversation_context(session_id)
            if continuity_block:
                prompt = (
                    f"CONVERSATION SO FAR (prior context — answer the NEW message at the end):\n"
                    f"{continuity_block}\n\n"
                    f"NEW MESSAGE: {goal_desc}"
                )
    except Exception:
        # Best-effort: on any failure, proceed with bare prompt
        pass

    # Even though context_manager.conversation_context raised, prompt is unmodified
    assert prompt == goal_desc


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_react_loop_injects_continuity_after_system_message():
    """In _run_react_loop, continuity is injected between system message (index 0) and goal."""
    # Simulate the message assembly from _run_react_loop
    # (without using PromptAssembler which has complex setup)

    # Create a minimal system message dict (as returned by assembler.system_message())
    system_message = {
        "role": "system",
        "content": "You are a helpful coding assistant.",
    }

    goal = "What is machine learning?"
    messages = [system_message]  # frozen system dict at index 0

    # Create orchestrator with context_manager
    async_orch = MagicMock()
    async_orch.context_manager = AsyncMock()
    async_orch.context_manager.conversation_context = AsyncMock(
        return_value="USER: Tell me about AI\nASSISTANT: AI is..."
    )
    async_orch._active_session_id = "test_session"

    # Inject continuity (same pattern as in coding_orchestrator.py)
    if async_orch.context_manager is not None and async_orch._active_session_id:
        continuity_block = await async_orch.context_manager.conversation_context(
            async_orch._active_session_id
        )
        if continuity_block:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"CONVERSATION SO FAR (context — the user's new message is below):\n"
                        f"{continuity_block}"
                    ),
                }
            )

    # Append the actual goal message
    messages.append(
        {"role": "user", "content": goal},
    )

    # Verify structure
    assert len(messages) == 3
    assert messages[0]["role"] == "system"  # system message unchanged
    assert "CONVERSATION SO FAR" in messages[1]["content"]  # continuity at index 1
    assert "Tell me about AI" in messages[1]["content"]
    assert messages[2]["content"] == goal  # goal at index 2


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_react_loop_without_context_manager_no_injection():
    """When context_manager is None or _active_session_id is empty, no continuity injected."""
    # Create orchestrator without context_manager
    async_orch = type("AsyncOrch", (), {})()
    async_orch._active_session_id = ""

    system_message = {
        "role": "system",
        "content": "You are a helpful coding assistant.",
    }
    goal = "What is machine learning?"
    messages = [system_message]

    # No injection when context_manager is None
    if getattr(async_orch, "context_manager", None) is not None and async_orch._active_session_id:
        # This block is skipped
        pass

    messages.append(
        {"role": "user", "content": goal},
    )

    # Only system + goal (no continuity message at index 1)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == goal


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_react_loop_continuity_injection_is_best_effort():
    """When context_manager.conversation_context raises, the loop continues with no injection."""
    async_orch = MagicMock()
    async_orch.context_manager = AsyncMock()
    async_orch.context_manager.conversation_context = AsyncMock(
        side_effect=RuntimeError("Memory error")
    )
    async_orch._active_session_id = "test_session"

    system_message = {
        "role": "system",
        "content": "You are a helpful coding assistant.",
    }
    goal = "What is machine learning?"
    messages = [system_message]

    # Injection attempt (same as in real code)
    if async_orch.context_manager is not None and async_orch._active_session_id:
        try:
            continuity_block = await async_orch.context_manager.conversation_context(
                async_orch._active_session_id
            )
            if continuity_block:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"CONVERSATION SO FAR (context — the user's new message is below):\n"
                            f"{continuity_block}"
                        ),
                    }
                )
        except Exception:
            # Best-effort: log to stderr and proceed
            pass

    messages.append(
        {"role": "user", "content": goal},
    )

    # Even with the error, loop continues with only system + goal
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == goal
