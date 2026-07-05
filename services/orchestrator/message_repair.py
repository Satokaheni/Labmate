"""Pure, deterministic message-sequence repair for the OpenAI-compatible
inference seam.

The orchestrator appends tool results and assistant turns directly onto a
``messages`` list with no validation; sibling features (verification-stop
guard, interrupt-steering) inject SYNTHETIC user/tool turns that can wedge
role alternation. ``sanitize_messages`` repairs the list right before each
model call: it drops ORPHANED tool results, merges illegal adjacent same-role
runs, and NEVER mutates the system message at index 0 or reorders the leading
system+user prefix (the llama.cpp prompt-cache prefix must stay byte-stable).

Everything here is pure: no I/O, no network, deterministic, input never
mutated, a NEW list returned.
"""

from __future__ import annotations

import os

_FALSEY = {"0", "false", "no", "off", ""}


def message_repair_enabled() -> bool:
    """True when ENABLE_MESSAGE_REPAIR is an explicit truthy value.

    Default OFF. Mirrors conditional_gates_enabled() and finalize_revision_enabled().
    """
    return os.getenv("ENABLE_MESSAGE_REPAIR", "0").strip().lower() not in _FALSEY


def _declared_tool_call_ids(messages: list[dict]) -> set[str]:
    """All tool_call ids declared by any assistant message in the list."""
    ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tid is not None:
                    ids.add(tid)
    return ids


def _drop_orphan_tool_results(messages: list[dict]) -> list[dict]:
    """Drop any tool message whose tool_call_id was never declared by a
    PRECEDING assistant tool_calls entry. Uses a running set so a tool result
    that appears BEFORE its assistant call is still treated as orphaned.
    """
    declared_so_far: set[str] = set()
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tid is not None:
                    declared_so_far.add(tid)
            out.append(dict(m))
        elif role == "tool":
            if m.get("tool_call_id") in declared_so_far:
                out.append(dict(m))
            # else: orphaned → drop
        else:
            out.append(dict(m))
    return out


def _tool_call_ids(m: dict) -> list[str]:
    """The tool_call ids declared by a single assistant message, in order."""
    ids: list[str] = []
    for tc in m.get("tool_calls") or []:
        tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if tid is not None:
            ids.append(tid)
    return ids


def _answered_tool_call_ids(messages: list[dict]) -> set[str]:
    """All tool_call ids that HAVE a matching tool-role result anywhere."""
    return {
        m.get("tool_call_id")
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id") is not None
    }


# Content for a synthetic tool result standing in for a dropped/interrupted call.
_DANGLING_STUB = (
    "[tool result unavailable — the call was interrupted, cancelled, or dropped "
    "before a result was recorded]"
)


def patch_dangling_tool_calls(messages: list[dict], *, stub: str = _DANGLING_STUB) -> list[dict]:
    """Insert a synthetic tool result for any assistant ``tool_calls`` id that has
    NO matching tool-role message anywhere in the list.

    The OpenAI-compatible endpoint rejects (400) a request where an assistant
    message declares a tool_call with no answering ``tool`` message — and a single
    dangling call POISONS every subsequent request. Danglers arise when the loop
    halts/returns/cancels before appending a tool result, or when a synthetic
    user turn (verification-stop / steer) is injected between an assistant
    tool_call and its result. This is the CONVERSE of
    ``_drop_orphan_tool_results`` (which handles tool results with no preceding
    call), and unlike ``sanitize_messages`` it is meant to run ALWAYS (it only
    ADDS missing results — it never drops or reorders real content, so it is a
    safe no-op when the history is already well-formed).

    A stub for a missing id is inserted immediately AFTER the declaring assistant
    message (ahead of any real results already present for that turn — still a
    contiguous tool block, which the provider accepts). Pure: input never
    mutated, a NEW list returned; the system message at index 0 is untouched.

    Scope: ``answered`` means a matching tool result exists ANYWHERE. A real
    result that exists but is DISPLACED (e.g. sits after an injected user turn,
    breaking contiguity) is treated as answered and left as-is — repairing a
    displaced-but-present result is sanitize's job, not this patch's. The live
    loop appends all of a turn's tool results before any nudge/steer, so that
    displaced-result ordering is not produced in practice.
    """
    if not messages:
        return []
    answered = _answered_tool_call_ids(messages)
    out: list[dict] = []
    for m in messages:
        out.append(dict(m))
        if m.get("role") != "assistant":
            continue
        for tid in _tool_call_ids(m):
            if tid not in answered:
                out.append({"role": "tool", "tool_call_id": tid, "content": stub})
                answered.add(tid)  # guard against a repeated id yielding two stubs
    return out


def _content_str(m: dict) -> str:
    c = m.get("content")
    return c if isinstance(c, str) else ("" if c is None else str(c))


def _is_mergeable_assistant(m: dict) -> bool:
    """An assistant message with NO tool_calls is plain text and may merge."""
    return m.get("role") == "assistant" and not (m.get("tool_calls"))


def _merge_adjacent_same_role(messages: list[dict]) -> list[dict]:
    """Collapse illegal adjacent same-role runs.

    Merges two adjacent ``user`` messages, or two adjacent ``assistant``
    messages that BOTH carry no tool_calls, by joining content with '\\n'.
    ``tool`` messages are never merged (multiple tool results for one assistant
    turn are legal). An assistant-with-tool_calls is never merged.
    """
    out: list[dict] = []
    for m in messages:
        if not out:
            out.append(dict(m))
            continue
        prev = out[-1]
        role = m.get("role")
        if role == "user" and prev.get("role") == "user":
            merged = dict(prev)
            merged["content"] = _content_str(prev) + "\n" + _content_str(m)
            out[-1] = merged
            continue
        if role == "assistant" and _is_mergeable_assistant(prev) and _is_mergeable_assistant(m):
            merged = dict(prev)
            merged["content"] = _content_str(prev) + "\n" + _content_str(m)
            out[-1] = merged
            continue
        out.append(dict(m))
    return out


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Return a NEW, repaired copy of ``messages`` (see module docstring).

    Order of passes:
      1. Drop orphaned tool results (running-declared-id check).
      2. Merge illegal adjacent same-role runs.
    The system message at index 0 and the leading system+user prefix are never
    reordered: the merge pass only ever folds a LATER adjacent message into an
    EARLIER one of the same role, preserving the prefix anchor's position.
    """
    if not messages:
        return []
    stage1 = _drop_orphan_tool_results(messages)
    stage2 = _merge_adjacent_same_role(stage1)
    return stage2


def validate_messages(messages: list[dict]) -> list[str]:
    """Return human-readable problems (for tests / logging). Does not repair."""
    problems: list[str] = []
    declared_so_far: set[str] = set()
    answered: set[str] = set()
    prev_role: str | None = None
    for idx, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tid is not None:
                    declared_so_far.add(tid)
            if (
                prev_role == "assistant"
                and _is_mergeable_assistant(m)
                and not (messages[idx - 1].get("tool_calls"))
            ):
                problems.append(f"adjacent assistant messages at index {idx}")
        elif role == "tool":
            tid = m.get("tool_call_id")
            if tid not in declared_so_far:
                problems.append(f"orphaned tool result tool_call_id={tid!r} at index {idx}")
            else:
                answered.add(tid)
        elif role == "user":
            if prev_role == "user":
                problems.append(f"adjacent user messages at index {idx}")
        prev_role = role
    dangling = declared_so_far - answered
    for tid in sorted(dangling):
        problems.append(f"dangling unanswered assistant tool_call id={tid!r}")
    return problems
