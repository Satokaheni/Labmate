from __future__ import annotations

import pytest

from services.orchestrator.message_repair import (
    sanitize_messages,
    validate_messages,
    message_repair_enabled,
)


def test_enabled_defaults_on(monkeypatch):
    monkeypatch.delenv("ENABLE_MESSAGE_REPAIR", raising=False)
    assert message_repair_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "FALSE", " Off "])
def test_enabled_falsey_values_disable(monkeypatch, val):
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", val)
    assert message_repair_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "ON"])
def test_enabled_truthy_values_enable(monkeypatch, val):
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", val)
    assert message_repair_enabled() is True


def test_sanitize_returns_new_list_not_same_object():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    out = sanitize_messages(msgs)
    assert out is not msgs
    assert out == msgs


def test_validate_returns_list():
    assert validate_messages([{"role": "user", "content": "hi"}]) == []


def _assistant_with_calls(*ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": i, "type": "function",
             "function": {"name": "run_bash", "arguments": "{}"}}
            for i in ids
        ],
    }


def test_orphaned_tool_result_is_dropped():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        # tool result with NO preceding assistant tool_call for "ghost"
        {"role": "tool", "tool_call_id": "ghost", "content": "stale"},
    ]
    out = sanitize_messages(msgs)
    assert out == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
    ]


def test_valid_edit_tool_finish_sequence_unchanged():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "edit the file"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "wrote file"},
        {"role": "assistant", "content": "done"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs  # well-formed → byte-identical pass-through


def test_answered_tool_result_is_kept_orphan_dropped():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "kept"},      # answered → keep
        {"role": "tool", "tool_call_id": "orphan", "content": "drop"},  # no call → drop
    ]
    out = sanitize_messages(msgs)
    assert out == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "kept"},
    ]


def test_input_is_not_mutated():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "ghost", "content": "stale"},
    ]
    before = [dict(m) for m in msgs]
    sanitize_messages(msgs)
    assert msgs == before  # caller's list untouched
