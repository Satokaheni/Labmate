"""Heuristic: does a task request an IRREVERSIBLE action that needs human approval?
Mirrors edit_intent.py's word-boundary verb-table style (reproduces the graph's
approval gate trigger without a graph)."""

from __future__ import annotations

import re

_IRREVERSIBLE = (
    r"deploy",
    r"delete",
    r"drop",
    r"rm\s+-rf",
    r"force[- ]?push",
    r"push\b",
    r"publish",
    r"migrate",
    r"truncate",
    r"wipe",
    r"reset\s+--hard",
)
_PATTERN = re.compile(r"\b(" + "|".join(_IRREVERSIBLE) + r")", re.IGNORECASE)


def requires_approval(text: str) -> bool:
    """True when the task names an irreversible action (gate before executing it)."""
    return bool(text and _PATTERN.search(text))
