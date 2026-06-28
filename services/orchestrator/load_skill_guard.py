"""Pure helpers to de-duplicate load_skill calls within a single ReAct goal.

The weak local model frequently re-issues load_skill for a skill it has
already loaded this goal, burning iteration budget. The ReAct loop tracks the
set of skills loaded so far this goal and uses these helpers to (a) detect a
repeat load and (b) build a clear "already loaded — call its tools directly"
tool result. No async, no I/O, no orchestrator imports — fully unit-testable.
"""

from __future__ import annotations


def is_repeat_load(name: str, loaded_names: set[str]) -> bool:
    """True iff ``name`` is a non-empty skill already in ``loaded_names``.

    An empty / missing name is NOT a repeat: it must fall through to the real
    loader so the proper "unknown skill" error is surfaced to the model.
    """
    return bool(name) and name in loaded_names


def already_loaded_message(name: str, loaded_names: set[str]) -> dict:
    """Build a JSON-serializable load_skill tool result for a repeat load.

    Mirrors SkillRunner.load_skill's envelope shape
    ({"name": "load_skill", "response": {...}}) so the model sees a familiar
    structure, with status 'already_loaded' and an explicit instruction to call
    the skill's tools directly instead of re-loading it.
    """
    loaded_sorted = sorted(loaded_names)
    message = (
        f"skill '{name}' is already loaded; its tools are available — "
        f"call them directly, do not load_skill it again. "
        f"Loaded skills: {', '.join(loaded_sorted)}"
    )
    return {
        "name": "load_skill",
        "response": {
            "status": "already_loaded",
            "name": name,
            "message": message,
            "loaded": loaded_sorted,
        },
    }


__all__ = ["is_repeat_load", "already_loaded_message"]
