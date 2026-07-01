"""Test that _relay_task persists the complete assistant turn on turn.done."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from services.ws_gateway.server import _relay_task


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def fake_ws():
    """Mock WebSocket that tracks sent frames."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


@pytest.fixture
def fake_store():
    """Mock session store with add_turn tracking."""
    store = AsyncMock()
    store.add_turn = AsyncMock()
    return store


async def fake_tail_task_events(events_list):
    """Async generator that yields events from a list."""
    for event in events_list:
        yield event


@pytest.mark.asyncio
async def test_relay_persist_complete_turn(fake_ws, fake_store):
    """
    Drive _relay_task with a sequence of events (answer.delta, reasoning, tool.start/done, turn.done)
    and verify that:
    1. The assistant turn is persisted via store.add_turn with the correct structure
    2. All frames are still forwarded to the client (relay behavior unchanged)
    3. A store error does NOT propagate (best-effort)
    """
    task_id = "task-test-123"
    session_id = "s-test-789"
    assistant_turn_id = "turn-asst-123"

    # Sequence of events the orchestrator would emit
    events = [
        {
            "type": "answer.delta",
            "task_id": task_id,
            "seq": 1,
            "text": "Hello, ",
        },
        {
            "type": "answer.delta",
            "task_id": task_id,
            "seq": 2,
            "text": "world!",
        },
        {
            "type": "reasoning",
            "task_id": task_id,
            "seq": 3,
            "text": "Thinking about the problem.",
            "node": "chat_node",
        },
        {
            "type": "tool.start",
            "task_id": task_id,
            "seq": 4,
            "tool_id": "tool-001",
            "name": "test_tool",
            "kind": "tool",
            "summary": "Running test tool",
            "reasoning_why": "To verify something",
            "args": {"arg1": "value1"},
        },
        {
            "type": "tool.done",
            "task_id": task_id,
            "seq": 5,
            "tool_id": "tool-001",
            "status": "done",
            "summary": "Tool completed",
            "result": {"output": "test result"},
            "duration_ms": 150,
        },
        {
            "type": "turn.done",
            "task_id": task_id,
            "seq": 6,
            "status": "complete",
            "final_answer": "FULL ANSWER",
        },
    ]

    # Mock tail_task_events to return our events
    import services.ws_gateway.server as server_module

    original_tail = server_module.tail_task_events
    server_module.tail_task_events = lambda redis, task_id, **kw: fake_tail_task_events(events)

    try:
        # Call _relay_task with store and session_id threaded in
        await _relay_task(
            fake_ws,
            AsyncMock(),  # redis (not used in this test)
            task_id,
            assistant_turn_id,
            store=fake_store,
            session_id=session_id,
            debug=False,
        )
    finally:
        # Restore original
        server_module.tail_task_events = original_tail

    # Verify store.add_turn was called exactly once
    assert fake_store.add_turn.call_count == 1
    call_args = fake_store.add_turn.call_args
    assert call_args[0][0] == session_id  # First positional arg is session_id

    # Verify the turn structure
    turn = call_args[0][1]  # Second positional arg is the turn dict
    assert turn["id"] == assistant_turn_id
    assert turn["sessionId"] == session_id
    assert turn["role"] == "assistant"
    assert turn["text"] == "FULL ANSWER"  # Preferred from final_answer
    # Verify reasoning is a structured dict, not a bare string
    assert isinstance(
        turn["reasoning"], dict
    ), f"reasoning should be dict, got {type(turn['reasoning'])}"
    assert turn["reasoning"]["text"] == "Thinking about the problem."
    assert turn["reasoning"]["summary"] == "Thinking about the problem."
    assert turn["reasoning"]["node"] == "chat_node"
    assert isinstance(turn["reasoning"]["tokens"], int)
    assert isinstance(turn["reasoning"]["budget"], int)
    assert turn["status"] == "complete"
    assert isinstance(turn["createdAt"], str)  # ISO format
    assert "toolCalls" in turn
    assert len(turn["toolCalls"]) == 1

    # Verify tool call structure
    tool_call = turn["toolCalls"][0]
    assert tool_call["id"] == "tool-001"
    assert tool_call["name"] == "test_tool"
    assert tool_call["args"] == {"arg1": "value1"}
    assert tool_call["result"] == {"output": "test result"}
    assert tool_call["status"] == "done"

    # Verify client still received all frames (relay preserved)
    # Frames sent: answer.delta (x2), reasoning.done, tool.start, tool.done, turn.done
    assert fake_ws.send_json.call_count >= 4  # At least the main frames


@pytest.mark.asyncio
async def test_relay_persist_no_session_id_skips_store(fake_ws, fake_store):
    """When session_id is empty/None, add_turn should NOT be called (best-effort)."""
    task_id = "task-test-123"
    assistant_turn_id = "turn-asst-123"

    events = [
        {"type": "answer.delta", "task_id": task_id, "seq": 1, "text": "reply"},
        {
            "type": "turn.done",
            "task_id": task_id,
            "seq": 2,
            "status": "complete",
            "final_answer": "FULL",
        },
    ]

    import services.ws_gateway.server as server_module

    original_tail = server_module.tail_task_events
    server_module.tail_task_events = lambda redis, task_id, **kw: fake_tail_task_events(events)

    try:
        # Call with empty session_id
        await _relay_task(
            fake_ws,
            AsyncMock(),
            task_id,
            assistant_turn_id,
            store=fake_store,
            session_id="",  # Empty session_id
            debug=False,
        )
    finally:
        server_module.tail_task_events = original_tail

    # Verify store.add_turn was NOT called
    assert fake_store.add_turn.call_count == 0


@pytest.mark.asyncio
async def test_relay_persist_store_error_does_not_propagate(fake_ws, fake_store):
    """If store.add_turn raises an exception, it should NOT break the relay (best-effort)."""
    task_id = "task-test-123"
    session_id = "s-test-789"
    assistant_turn_id = "turn-asst-123"

    events = [
        {"type": "answer.delta", "task_id": task_id, "seq": 1, "text": "hi"},
        {
            "type": "turn.done",
            "task_id": task_id,
            "seq": 2,
            "status": "complete",
            "final_answer": "FULL",
        },
    ]

    # Make store.add_turn raise an exception
    fake_store.add_turn = AsyncMock(side_effect=RuntimeError("Mongo is down"))

    import services.ws_gateway.server as server_module

    original_tail = server_module.tail_task_events
    server_module.tail_task_events = lambda redis, task_id, **kw: fake_tail_task_events(events)

    try:
        # Should NOT raise, despite store.add_turn failing
        await _relay_task(
            fake_ws,
            AsyncMock(),
            task_id,
            assistant_turn_id,
            store=fake_store,
            session_id=session_id,
            debug=False,
        )
    finally:
        server_module.tail_task_events = original_tail

    # Verify the relay still sent frames (client gets the turn.done)
    assert fake_ws.send_json.call_count >= 1


@pytest.mark.asyncio
async def test_relay_persist_uses_final_answer_if_present(fake_ws, fake_store):
    """The final_answer from turn.done event should be preferred over answer chunks."""
    task_id = "task-test-123"
    session_id = "s-test-789"
    assistant_turn_id = "turn-asst-123"

    events = [
        {"type": "answer.delta", "task_id": task_id, "seq": 1, "text": "Old answer"},
        {"type": "answer.delta", "task_id": task_id, "seq": 2, "text": " text"},
        {
            "type": "turn.done",
            "task_id": task_id,
            "seq": 3,
            "status": "complete",
            "final_answer": "NEW FINAL ANSWER",
        },
    ]

    import services.ws_gateway.server as server_module

    original_tail = server_module.tail_task_events
    server_module.tail_task_events = lambda redis, task_id, **kw: fake_tail_task_events(events)

    try:
        await _relay_task(
            fake_ws,
            AsyncMock(),
            task_id,
            assistant_turn_id,
            store=fake_store,
            session_id=session_id,
            debug=False,
        )
    finally:
        server_module.tail_task_events = original_tail

    # Verify the turn text is the final_answer, not the concatenated deltas
    turn = fake_store.add_turn.call_args[0][1]
    assert turn["text"] == "NEW FINAL ANSWER"


@pytest.mark.asyncio
async def test_relay_persist_fallback_to_deltas_if_no_final_answer(fake_ws, fake_store):
    """If turn.done has no final_answer, concatenate the answer.delta chunks."""
    task_id = "task-test-123"
    session_id = "s-test-789"
    assistant_turn_id = "turn-asst-123"

    events = [
        {"type": "answer.delta", "task_id": task_id, "seq": 1, "text": "Hello "},
        {"type": "answer.delta", "task_id": task_id, "seq": 2, "text": "world"},
        {
            "type": "turn.done",
            "task_id": task_id,
            "seq": 3,
            "status": "complete",
        },  # No final_answer
    ]

    import services.ws_gateway.server as server_module

    original_tail = server_module.tail_task_events
    server_module.tail_task_events = lambda redis, task_id, **kw: fake_tail_task_events(events)

    try:
        await _relay_task(
            fake_ws,
            AsyncMock(),
            task_id,
            assistant_turn_id,
            store=fake_store,
            session_id=session_id,
            debug=False,
        )
    finally:
        server_module.tail_task_events = original_tail

    # Verify the turn text is the concatenated deltas
    turn = fake_store.add_turn.call_args[0][1]
    assert turn["text"] == "Hello world"


@pytest.mark.asyncio
async def test_relay_persist_error_status(fake_ws, fake_store):
    """When turn.done has status='error', it should be recorded in the turn."""
    task_id = "task-test-123"
    session_id = "s-test-789"
    assistant_turn_id = "turn-asst-123"

    events = [
        {"type": "answer.delta", "task_id": task_id, "seq": 1, "text": "Failed"},
        {
            "type": "turn.done",
            "task_id": task_id,
            "seq": 2,
            "status": "error",
            "final_answer": "Error occurred",
        },
    ]

    import services.ws_gateway.server as server_module

    original_tail = server_module.tail_task_events
    server_module.tail_task_events = lambda redis, task_id, **kw: fake_tail_task_events(events)

    try:
        await _relay_task(
            fake_ws,
            AsyncMock(),
            task_id,
            assistant_turn_id,
            store=fake_store,
            session_id=session_id,
            debug=False,
        )
    finally:
        server_module.tail_task_events = original_tail

    # Verify status is recorded
    turn = fake_store.add_turn.call_args[0][1]
    assert turn["status"] == "error"


@pytest.mark.asyncio
async def test_relay_persist_no_reasoning_none(fake_ws, fake_store):
    """When turn.done has no reasoning events, persisted reasoning should be None."""
    task_id = "task-test-123"
    session_id = "s-test-789"
    assistant_turn_id = "turn-asst-123"

    events = [
        {"type": "answer.delta", "task_id": task_id, "seq": 1, "text": "Hello"},
        {
            "type": "turn.done",
            "task_id": task_id,
            "seq": 2,
            "status": "complete",
            "final_answer": "Hello there",
        },
    ]

    import services.ws_gateway.server as server_module

    original_tail = server_module.tail_task_events
    server_module.tail_task_events = lambda redis, task_id, **kw: fake_tail_task_events(events)

    try:
        await _relay_task(
            fake_ws,
            AsyncMock(),
            task_id,
            assistant_turn_id,
            store=fake_store,
            session_id=session_id,
            debug=False,
        )
    finally:
        server_module.tail_task_events = original_tail

    # Verify reasoning is None when no reasoning events were present
    turn = fake_store.add_turn.call_args[0][1]
    assert turn["reasoning"] is None
