"""Pure unit tests for `translate_event` (orchestrator snake_case -> frontend StreamEvent).

Relocated from the deleted `test_redis_bridge.py` (Piece 4 T7b), then again from
`redis_bridge.py` itself (Piece 4 T8): translate_event is Redis-free logic that
now lives in its own module as part of the gateway's transport-swap to the
in-process runtime, so its dedicated correctness coverage stays too.
"""

from services.ws_gateway.event_translate import translate_event


def test_translate_tool_start_to_camel():
    raw = {
        "type": "tool.start",
        "task_id": "t1",
        "seq": 1,
        "tool_id": "x1",
        "name": "web_search",
        "kind": "skill",
        "reasoning_why": "facts",
    }
    out = translate_event(raw, turn_id="turn-1")
    assert out["type"] == "tool.start"
    assert out["turnId"] == "turn-1"
    assert out["toolCall"]["id"] == "x1"
    assert out["toolCall"]["name"] == "web_search"
    assert out["toolCall"]["reasoningWhy"] == "facts"


def test_translate_tool_done_to_camel():
    raw = {
        "type": "tool.done",
        "task_id": "t1",
        "seq": 2,
        "tool_id": "x1",
        "status": "done",
        "summary": "found 3",
        "duration_ms": 1200,
        "result": {"hits": 3},
    }
    out = translate_event(raw, turn_id="turn-1")
    assert out == {
        "type": "tool.done",
        "turnId": "turn-1",
        "toolId": "x1",
        "status": "done",
        "summary": "found 3",
        "result": {"hits": 3},
        "durationMs": 1200,
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


def test_translate_context_to_context_update():
    raw = {
        "type": "context",
        "window": {
            "max": 16384,
            "used": 4200,
            "free": 12184,
            "segments": {
                "systemPrompt": 800,
                "skillInstructions": 0,
                "conversation": 3400,
                "workingMemory": 0,
                "reasoning": 0,
            },
        },
    }
    out = translate_event(raw, turn_id="t1")
    assert out == {"type": "context.update", "window": raw["window"]}


def test_translate_agent_status_to_agent_status():
    status = {
        "brain": {
            "model": "gemma-31b",
            "endpoint": ":8000",
            "state": "active",
            "node": "plan_node",
            "thinkingBudget": 3000,
        },
        "nervousSystem": {
            "name": "MCP bridge",
            "transport": "stdio",
            "state": "connected",
            "toolsRegistered": 4,
        },
        "hands": {"skills": []},
    }
    raw = {"type": "agent_status", "status": status}
    out = translate_event(raw, turn_id="t1")
    assert out == {"type": "agent.status", "status": status}


def test_translate_artifact_created_passthrough():
    artifact = {
        "id": "art-1",
        "name": "server.py",
        "path": "services/ws_gateway/server.py",
        "language": "Python",
        "mime": "text/x-python",
        "sizeBytes": 1024,
        "lineCount": 40,
        "preview": "code",
        "content": "# ...",
        "downloadUrl": "/artifacts/art-1",
    }
    raw = {"type": "artifact_created", "artifact": artifact}
    out = translate_event(raw, turn_id="t1")
    assert out == {"type": "artifact.created", "turnId": "t1", "artifact": artifact}
