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
        if (
            role == "assistant"
            and _is_mergeable_assistant(prev)
            and _is_mergeable_assistant(m)
        ):
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
            if prev_role == "assistant" and _is_mergeable_assistant(m) and not (
                messages[idx - 1].get("tool_calls")
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
