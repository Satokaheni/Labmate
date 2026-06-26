"""Out-of-band steer injection: turn a mid-turn user steer into a genuine
user instruction inside the ReAct message list, preserving role alternation.

The injected text is wrapped in an explicit marker so the model treats it as a
real, mid-turn user message (the system prompt explains the marker is genuine,
NOT a prompt-injection attack). Injecting onto the LAST tool message keeps the
assistant/tool/user alternation valid (the steer rides on the tool turn the
model just produced); when no tool message exists yet the steer becomes a
standalone user turn after the goal.
"""
from __future__ import annotations

import copy

OOB_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, "
    "delivered mid-turn; not tool output]"
)
OOB_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"


def wrap_oob(text: str) -> str:
    """Wrap steer text in the out-of-band marker."""
    return f"{OOB_OPEN} {text} {OOB_CLOSE}"


def _sanitize(messages: list[dict]) -> list[dict]:
    """Repair role alternation via the sibling message_repair module.

    DEPENDENCY: services.orchestrator.message_repair.sanitize_messages (sibling
    plan). If that module is not present yet, degrade to identity — injection
    onto the last tool message already preserves a valid shape, so a missing
    repair pass is safe, not a correctness hole.
    """
    try:
        from services.orchestrator.message_repair import (
            sanitize_messages,
            message_repair_enabled,
        )
    except Exception:
        return messages
    if not message_repair_enabled():
        return messages
    try:
        return sanitize_messages(messages)
    except Exception:
        return messages


def inject_steer(messages: list[dict], text: str) -> list[dict]:
    """Return a NEW message list with the steer injected as a marked OOB user
    instruction. Appends to the last tool message when present (preserving
    alternation); otherwise adds a standalone user turn.
    """
    out = copy.deepcopy(messages)
    wrapped = wrap_oob(text)
    # If the last message is a tool message, append the wrapped steer to it
    if out and out[-1].get("role") == "tool":
        existing = out[-1].get("content") or ""
        out[-1]["content"] = f"{existing}\n\n{wrapped}" if existing else wrapped
    else:
        # No tool message at the end; append a new standalone user message
        out.append({"role": "user", "content": wrapped})
    return _sanitize(out)
