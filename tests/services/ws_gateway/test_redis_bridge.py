import json
import pytest

from services.ws_gateway.redis_bridge import (
    push_task,
    tail_task_events,
    translate_event,
    write_tool_result,
    GOALS_STREAM,
    TOOL_RESULTS_PREFIX,
)


@pytest.mark.asyncio
async def test_push_task_xadds_payload_to_goals(redis):
    await push_task(redis, "task-1", task="do thing", session_id="s1", user_id="u1")
    entries = await redis.xrange(GOALS_STREAM)
    assert len(entries) == 1
    _id, fields = entries[0]
    payload = json.loads(fields["payload"])
    assert payload["task_id"] == "task-1"
    assert payload["task"] == "do thing"
    assert payload["session_id"] == "s1"


@pytest.mark.asyncio
async def test_tail_yields_decoded_events_and_stops_on_turn_done(redis):
    stream = "labmate:events:task-1"
    await redis.xadd(stream, {"event": json.dumps({"type": "turn.start", "task_id": "task-1", "seq": 1})})
    await redis.xadd(stream, {"event": json.dumps({"type": "answer.delta", "task_id": "task-1", "seq": 2, "text": "hi"})})
    await redis.xadd(stream, {"event": json.dumps({"type": "turn.done", "task_id": "task-1", "seq": 3, "status": "complete"})})

    seen = []
    async for ev in tail_task_events(redis, "task-1", block_ms=50):
        seen.append(ev)
    assert [e["type"] for e in seen] == ["turn.start", "answer.delta", "turn.done"]


def test_translate_tool_start_to_camel():
    raw = {
        "type": "tool.start", "task_id": "t1", "seq": 1,
        "tool_id": "x1", "name": "web_search", "kind": "skill", "reasoning_why": "facts",
    }
    out = translate_event(raw, turn_id="turn-1")
    assert out["type"] == "tool.start"
    assert out["turnId"] == "turn-1"
    assert out["toolCall"]["id"] == "x1"
    assert out["toolCall"]["name"] == "web_search"
    assert out["toolCall"]["reasoningWhy"] == "facts"


def test_translate_tool_done_to_camel():
    raw = {
        "type": "tool.done", "task_id": "t1", "seq": 2,
        "tool_id": "x1", "status": "done", "summary": "found 3", "duration_ms": 1200, "result": {"hits": 3},
    }
    out = translate_event(raw, turn_id="turn-1")
    assert out == {
        "type": "tool.done", "turnId": "turn-1", "toolId": "x1",
        "status": "done", "summary": "found 3", "result": {"hits": 3}, "durationMs": 1200,
    }


def test_translate_reasoning_to_delta():
    raw = {"type": "reasoning", "task_id": "t1", "seq": 1, "text": "thinking"}
    out = translate_event(raw, turn_id="turn-1")
    assert out == {"type": "reasoning.delta", "turnId": "turn-1", "text": "thinking"}


def test_translate_answer_delta():
    raw = {"type": "answer.delta", "task_id": "t1", "seq": 1, "text": "hello"}
    out = translate_event(raw, turn_id="turn-1")
    assert out == {"type": "answer.delta", "turnId": "turn-1", "text": "hello"}


def test_translate_turn_done():
    raw = {"type": "turn.done", "task_id": "t1", "seq": 9, "status": "complete"}
    out = translate_event(raw, turn_id="turn-1")
    assert out == {"type": "turn.done", "turnId": "turn-1", "status": "complete"}


def test_translate_unknown_returns_none():
    raw = {"type": "answer.done", "task_id": "t1", "seq": 1, "text": "x"}
    assert translate_event(raw, turn_id="turn-1") is None


def test_translate_tool_request_passthrough():
    raw = {
        "type": "tool.request", "task_id": "t1", "seq": 4,
        "tool_request_id": "req-9", "name": "read_file", "args": {"path": "a.txt"},
    }
    out = translate_event(raw, turn_id="turn-1")
    assert out == {
        "type": "tool.request",
        "turnId": "turn-1",
        "toolRequestId": "req-9",
        "name": "read_file",
        "args": {"path": "a.txt"},
    }


@pytest.mark.asyncio
async def test_write_tool_result_xadds_to_results_stream(redis):
    await write_tool_result(redis, "task-7", "req-9", {"content": "hi"}, error=None)
    entries = await redis.xrange(f"{TOOL_RESULTS_PREFIX}task-7")
    assert len(entries) == 1
    _id, fields = entries[0]
    frame = json.loads(fields["result"])
    assert frame == {"tool_request_id": "req-9", "result": {"content": "hi"}, "error": None}


def test_translate_context_to_context_update():
    raw = {
        "type": "context",
        "window": {
            "max": 16384, "used": 4200, "free": 12184,
            "segments": {"systemPrompt": 800, "skillInstructions": 0,
                         "conversation": 3400, "workingMemory": 0, "reasoning": 0},
        },
    }
    out = translate_event(raw, turn_id="t1")
    assert out == {"type": "context.update", "window": raw["window"]}


def test_translate_agent_status_to_agent_status():
    status = {
        "brain": {"model": "gemma-31b", "endpoint": ":8000", "state": "active",
                  "node": "plan_node", "thinkingBudget": 3000},
        "nervousSystem": {"name": "MCP bridge", "transport": "stdio",
                          "state": "connected", "toolsRegistered": 4},
        "hands": {"skills": []},
    }
    raw = {"type": "agent_status", "status": status}
    out = translate_event(raw, turn_id="t1")
    assert out == {"type": "agent.status", "status": status}


def test_translate_artifact_created_passthrough():
    artifact = {
        "id": "art-1", "name": "server.py", "path": "services/ws_gateway/server.py",
        "language": "Python", "mime": "text/x-python",
        "sizeBytes": 1024, "lineCount": 40,
        "preview": "code", "content": "# ...", "downloadUrl": "/artifacts/art-1",
    }
    raw = {"type": "artifact_created", "artifact": artifact}
    out = translate_event(raw, turn_id="t1")
    assert out == {"type": "artifact.created", "turnId": "t1", "artifact": artifact}


@pytest.mark.asyncio
async def test_write_cancel_sets_redis_key(redis):
    from services.ws_gateway.redis_bridge import write_cancel, check_cancel
    await write_cancel(redis, "task-cancel-1")
    assert await check_cancel(redis, "task-cancel-1") is True


@pytest.mark.asyncio
async def test_check_cancel_returns_false_when_not_set(redis):
    from services.ws_gateway.redis_bridge import check_cancel
    assert await check_cancel(redis, "task-not-cancelled") is False
