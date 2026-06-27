"""Pure, dependency-free guards for the replan sequencing loop.

These helpers detect when the planner is no longer making progress — it keeps
emitting (near-)identical sub-goals, or it over-uses a single skill across
sub-steps — and tell the loop to finish honestly instead of re-cycling. The
module is intentionally pure (no I/O, no model calls, no async) so it is
trivially unit-testable and free of side effects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Tokens dropped during normalization so trivially-reworded sub-goals collapse
# to the same canonical string (duplicate detection is a normalized ==).
_ARTICLES = {"the", "a", "an", "on", "of", "to", "for", "in", "into"}
_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def normalize_subgoal(text: str) -> str:
    """Canonicalize a sub-goal string for equality comparison.

    Lowercase, strip punctuation, drop a small set of articles/prepositions,
    and collapse whitespace. Two sub-goals that differ only in casing,
    punctuation, or filler words normalize to the same value.
    """
    if not text:
        return ""
    lowered = str(text).lower()
    no_punct = _NON_WORD.sub(" ", lowered)
    tokens = [t for t in _WS.sub(" ", no_punct).strip().split(" ") if t and t not in _ARTICLES]
    return " ".join(tokens)


def count_skill_uses(history: list[dict], skill: str) -> int:
    """How many history steps recorded `skill` in their `skills` list."""
    if not skill:
        return 0
    total = 0
    for h in history:
        skills = h.get("skills") or []
        if skill in skills:
            total += 1
    return total
