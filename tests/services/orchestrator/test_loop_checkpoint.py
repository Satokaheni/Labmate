"""Pure serialize/deserialize unit tests for the inner-loop checkpoint.

No Mongo, no asyncio in this file — to_dict/from_dict are pure sync functions.
"""
from __future__ import annotations

import json

from services.orchestrator.loop_checkpoint import (
    CHECKPOINT_VERSION,
    LoopCheckpoint,
    to_dict,
    from_dict,
)


def _sample() -> LoopCheckpoint:
    return LoopCheckpoint(
        task_id="task-123",
        goal="fix the factorial off-by-one",
        messages=[
            {"role": "system", "content": "you are a coding agent"},
            {"role": "user", "content": "fix the factorial off-by-one"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "def factorial(...)"},
        ],
        used=3,
        absolute_turns=3,
        grace_used=False,
        edited_files=["src/math.py"],
        tests_passed=False,
        verify_nudges_used=1,
        loop_signatures=["read_file::{}", "write_file::{\"path\":\"x\"}"],
        tools_used=["read_file", "write_file"],
        start_monotonic_offset=12.5,
        turn=3,
    )


def test_round_trip_is_lossless():
    cp = _sample()
    restored = from_dict(to_dict(cp))
    assert restored == cp


def test_to_dict_is_json_serializable():
    cp = _sample()
    d = to_dict(cp)
    # Must survive a full JSON round-trip (Mongo stores BSON, but we keep it
    # JSON-able so the dict is trivially inspectable/loggable).
    reparsed = json.loads(json.dumps(d))
    assert from_dict(reparsed) == cp


def test_to_dict_includes_version():
    assert to_dict(_sample())["version"] == CHECKPOINT_VERSION


def test_from_dict_none_returns_none():
    assert from_dict(None) is None


def test_from_dict_missing_required_key_returns_none():
    d = to_dict(_sample())
    del d["messages"]
    assert from_dict(d) is None


def test_from_dict_unknown_version_returns_none():
    d = to_dict(_sample())
    d["version"] = 999
    assert from_dict(d) is None


def test_from_dict_wrong_type_returns_none():
    d = to_dict(_sample())
    d["used"] = "not-an-int"
    assert from_dict(d) is None


def test_from_dict_tolerates_extra_keys():
    d = to_dict(_sample())
    d["_id"] = "mongo-object-id"  # Mongo adds this; load must ignore it
    d["saved_at"] = "2026-06-26T00:00:00Z"
    assert from_dict(d) == _sample()
