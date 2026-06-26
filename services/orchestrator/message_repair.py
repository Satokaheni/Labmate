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


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Return a NEW, repaired copy of ``messages`` (see module docstring).

    Stub: filled in by later tasks. For now, a shallow copy so callers already
    get a new list (purity contract) without behavior change.
    """
    return [dict(m) for m in messages]


def validate_messages(messages: list[dict]) -> list[str]:
    """Return a list of human-readable problems found in ``messages``.

    Stub: filled in by a later task. Used by tests and (optionally) logging.
    """
    return []
