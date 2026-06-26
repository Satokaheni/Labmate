from services.orchestrator.steer_inject import (
    OOB_OPEN,
    OOB_CLOSE,
    wrap_oob,
    inject_steer,
)


def test_wrap_oob_brackets_text_with_marker():
    wrapped = wrap_oob("switch to db.py")
    assert wrapped.startswith(OOB_OPEN)
    assert wrapped.endswith(OOB_CLOSE)
    assert "switch to db.py" in wrapped


def test_inject_appends_to_last_tool_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ran ls"},
    ]
    out = inject_steer(messages, "stop, work on db.py")
    # Same number of messages — the steer rode on the tool turn.
    assert len(out) == len(messages)
    last = out[-1]
    assert last["role"] == "tool"
    assert "ran ls" in last["content"]
    assert OOB_OPEN in last["content"]
    assert "stop, work on db.py" in last["content"]
    # Original list is not mutated.
    assert OOB_OPEN not in messages[-1]["content"]


def test_inject_adds_standalone_user_when_no_tool_message_yet():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
    ]
    out = inject_steer(messages, "use staging db")
    assert len(out) == len(messages) + 1
    assert out[-1]["role"] == "user"
    assert OOB_OPEN in out[-1]["content"]


def test_inject_preserves_role_alternation_validity():
    # After injection no two adjacent NON-system messages share a role in a way
    # that would break OpenAI's tool/assistant contract: a tool message must be
    # preceded by an assistant with tool_calls; a standalone user is fine after
    # a user/assistant. We assert there is never an orphan tool message.
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
    ]
    out = inject_steer(messages, "x")
    for i, m in enumerate(out):
        if m["role"] == "tool":
            assert i > 0 and out[i - 1]["role"] == "assistant"
