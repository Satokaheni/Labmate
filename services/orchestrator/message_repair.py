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
    """True unless ENABLE_MESSAGE_REPAIR is an explicit falsey value.

    Default ON. Mirrors task_complexity.conditional_gates_enabled.
    """
    return os.getenv("ENABLE_MESSAGE_REPAIR", "1").strip().lower() not in _FALSEY


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


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Return a NEW, repaired copy of ``messages`` (see module docstring).

    Stub: filled in by later tasks. For now, a shallow copy so callers already
    get a new list (purity contract) without behavior change.
    """
    if not messages:
        return []
    return _drop_orphan_tool_results(messages)


def validate_messages(messages: list[dict]) -> list[str]:
    """Return a list of human-readable problems found in ``messages``.

    Stub: filled in by a later task. Used by tests and (optionally) logging.
    """
    return []
