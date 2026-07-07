from __future__ import annotations

import pytest

from services.orchestrator.message_repair import (
    message_repair_enabled,
    sanitize_messages,
    validate_messages,
)


def test_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("ENABLE_MESSAGE_REPAIR", raising=False)
    assert message_repair_enabled() is False


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
            {"id": i, "type": "function", "function": {"name": "run_bash", "arguments": "{}"}}
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
        {"role": "tool", "tool_call_id": "c1", "content": "kept"},  # answered → keep
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


def test_adjacent_user_messages_are_merged():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    out = sanitize_messages(msgs)
    # leading system+user prefix is sacred: the FIRST user is the prefix anchor,
    # so the merge folds the SECOND user into a single trailing user turn only
    # when it is not the prefix anchor. Here both are leading; the contract is
    # that the prefix (system, first user) stays, and the extra adjacent user is
    # merged into the prefix user content.
    assert out == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "first\nsecond"},
    ]


def test_adjacent_assistant_no_toolcalls_merged():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "part one"},
        {"role": "assistant", "content": "part two"},
    ]
    out = sanitize_messages(msgs)
    assert out == [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "part one\npart two"},
    ]


def test_assistant_with_toolcalls_not_merged_into_next_assistant():
    a = _assistant_with_calls("c1")
    msgs = [
        {"role": "user", "content": "go"},
        a,
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "summary"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs  # assistant-with-tool_calls is a distinct turn, untouched


def test_synthetic_user_after_tool_stays_valid():
    # interrupt-steering injects a synthetic user turn right after a tool result.
    # user-after-tool is legal alternation → must pass through unchanged.
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
        {"role": "user", "content": "[steering] focus on tests"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs


def test_adjacent_tool_results_are_not_merged():
    msgs = [
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1", "c2"),
        {"role": "tool", "tool_call_id": "c1", "content": "one"},
        {"role": "tool", "tool_call_id": "c2", "content": "two"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs  # two tool results for one assistant turn is legal


def test_sanitize_is_idempotent():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "tool", "tool_call_id": "ghost", "content": "x"},
        {"role": "assistant", "content": "p"},
        {"role": "assistant", "content": "q"},
    ]
    once = sanitize_messages(msgs)
    twice = sanitize_messages(once)
    assert once == twice


def test_validate_reports_orphan_and_adjacency_and_dangling():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},  # adjacency
        {"role": "tool", "tool_call_id": "ghost", "content": "x"},  # orphan
        _assistant_with_calls("never_answered"),  # dangling
    ]
    problems = validate_messages(msgs)
    assert any("orphan" in p.lower() for p in problems)
    assert any("adjacent" in p.lower() for p in problems)
    assert any("dangling" in p.lower() or "unanswered" in p.lower() for p in problems)


def test_validate_clean_sequence_no_problems():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    assert validate_messages(msgs) == []


# ── patch_dangling_tool_calls (G7 always-on dangling patch) ──────────────────


def test_dangling_tool_call_gets_a_stub():
    from services.orchestrator.message_repair import (
        patch_dangling_tool_calls,
        validate_messages,
    )

    # assistant declares c1 but there is NO tool result for it (dropped/cancelled),
    # then a user turn is injected (steer/verify) — classic poisoning sequence.
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "user", "content": "actually, focus here"},
    ]
    assert validate_messages(msgs) != []  # dangling before patch
    out = patch_dangling_tool_calls(msgs)
    # A stub tool result for c1 is inserted immediately after the assistant turn,
    # BEFORE the injected user message.
    assert out[3]["role"] == "tool"
    assert out[3]["tool_call_id"] == "c1"
    assert isinstance(out[3]["content"], str) and out[3]["content"]
    assert out[4] == {"role": "user", "content": "actually, focus here"}
    assert validate_messages(out) == []  # well-formed after patch


def test_patch_is_noop_when_all_calls_answered():
    from services.orchestrator.message_repair import patch_dangling_tool_calls

    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    out = patch_dangling_tool_calls(msgs)
    assert out == msgs  # nothing added
    # A NEW list is returned (input not mutated).
    assert out is not msgs


def test_patch_handles_multiple_danglers_including_partial_turn():
    from services.orchestrator.message_repair import (
        patch_dangling_tool_calls,
        validate_messages,
    )

    # One assistant turn declares c1 AND c2 but only c1 is answered; a later turn
    # declares c3 with no answer at all.
    msgs = [
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1", "c2"),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        _assistant_with_calls("c3"),
    ]
    out = patch_dangling_tool_calls(msgs)
    stub_ids = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    assert stub_ids.count("c2") == 1 and stub_ids.count("c3") == 1
    # c1's real result is preserved (not duplicated).
    assert stub_ids.count("c1") == 1
    assert validate_messages(out) == []


def test_patch_does_not_mutate_input_or_system_prefix():
    from services.orchestrator.message_repair import patch_dangling_tool_calls

    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
    ]
    before = [dict(m) for m in msgs]
    out = patch_dangling_tool_calls(msgs)
    assert msgs == before  # input untouched
    assert out[0] == {"role": "system", "content": "S"}  # prefix anchor preserved
    assert len(out) == 4  # stub appended
