"""Edit-intent classifier — deterministic heuristics for find-and-fix routing.

Decides whether a goal needs FILE EDITS / VERIFICATION (and so should enter the
multi-tool ReAct loop, where the model interleaves read + edit + run) versus a
pure read/answer goal (which keeps the existing single-skill dispatch).

The classifier is pure and deterministic: the same goal always yields the same
verdict, with no LLM call and no I/O. It is intentionally CONSERVATIVE — it only
returns True when an explicit edit/fix/verify verb is present, so a pure
"summarize" / "find bugs" / "review" goal is never mis-routed.

Rule (documented so it can be audited):
  1. If an UNCONDITIONAL edit verb appears as a word (fix, edit, patch, refactor,
     implement, rewrite, modify, repair, correct, resolve, debug, apply ...),
     -> requires edit.
  2. If a VERIFICATION phrase appears ("make the tests pass", "make it work",
     "get the tests green", "so the tests pass") -> requires edit.
  3. Otherwise -> no edit (pure read/answer). "review", "find bugs", "summarize",
     "explain", "what/why/how" questions are read-only UNLESS an edit verb from
     rule 1 also appears later in the goal (e.g. "review then fix").
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EditIntent:
    """Immutable edit-intent verdict for a goal."""

    requires_edit: bool
    reason: str


_FALSEY = {"", "0", "false", "no", "off"}


def route_edit_to_react_enabled() -> bool:
    """Whether edit-intent goals route to the ReAct loop.

    Reads env ``ROUTE_EDIT_TO_REACT``. Default ON (returns True when unset).
    Falsey values ("0", "false", "no", "off", case-insensitive) -> False.
    """
    return os.getenv("ROUTE_EDIT_TO_REACT", "1").strip().lower() not in _FALSEY


# Rule 1: explicit edit/fix verbs. Word-boundary matched so "prefix" / "fixture"
# do NOT match "fix", and "implementation" matches "implement" only as a stem
# via the explicit alternatives below. These are verbs that imply mutating files.
_EDIT_VERBS = (
    r"fix(?:es|ed|ing)?",
    r"edit(?:s|ed|ing)?",
    r"patch(?:es|ed|ing)?",
    r"refactor(?:s|ed|ing)?",
    r"implement(?:s|ed|ing)?",
    r"rewrite|rewrites|rewriting|rewrote",
    r"modif(?:y|ies|ied|ying)",
    r"repair(?:s|ed|ing)?",
    r"correct(?:s|ed|ing)?",
    r"resolv(?:e|es|ed|ing)",
    r"debug(?:s|ged|ging)?",
    r"appl(?:y|ies|ied|ying)",
    r"updat(?:e|es|ed|ing)",
)
_EDIT_VERB_RE = re.compile(
    r"\b(?:" + "|".join(_EDIT_VERBS) + r")\b", re.IGNORECASE
)

# Rule 2: verification phrases — "make the tests pass", "make it work", etc.
# These imply the agent must change code until a check passes.
_VERIFY_PHRASE_RE = re.compile(
    r"(?:"
    r"make\s+(?:the\s+|all\s+)?tests?\s+pass"
    r"|get\s+(?:the\s+)?tests?\s+(?:to\s+)?(?:pass|green|passing)"
    r"|so\s+(?:the\s+|all\s+)?tests?\s+pass"
    r"|make\s+it\s+work"
    r"|get\s+it\s+working"
    r"|make\s+the\s+build\s+(?:pass|green|work)"
    r")",
    re.IGNORECASE,
)


def classify_edit_intent(
    goal: str,
    *,
    enabled: bool | None = None,
) -> EditIntent:
    """Classify whether ``goal`` needs file edits / verification.

    Args:
        goal: The goal/sub-goal text.
        enabled: Explicit feature toggle. If None, consults
            route_edit_to_react_enabled(). If False, always returns no-edit.

    Returns:
        EditIntent(requires_edit, reason).
    """
    if enabled is None:
        enabled = route_edit_to_react_enabled()

    if not enabled:
        return EditIntent(requires_edit=False, reason="feature disabled")

    text = goal or ""

    if _EDIT_VERB_RE.search(text):
        return EditIntent(requires_edit=True, reason="explicit edit/fix verb")

    if _VERIFY_PHRASE_RE.search(text):
        return EditIntent(requires_edit=True, reason="verification phrase (make tests pass / make it work)")

    return EditIntent(
        requires_edit=False,
        reason="read/answer goal (no edit verb or verification phrase)",
    )


def requires_editing(goal: str, *, enabled: bool | None = None) -> bool:
    """Bool-only convenience wrapper over classify_edit_intent()."""
    return classify_edit_intent(goal, enabled=enabled).requires_edit
