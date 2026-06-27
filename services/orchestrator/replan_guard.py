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


@dataclass(frozen=True)
class ReplanStop:
    """Decision returned by `replan_should_stop`.

    stop   -- True when the loop should finish instead of running `next_subgoal`.
    reason -- "" when stop is False; otherwise one of:
              "duplicate_subgoal" | "skill_repeat_cap".
    """
    stop: bool
    reason: str


def _skill_for_next(next_subgoal: str, history: list[dict]) -> str | None:
    """Best-effort: which already-used skill name appears in `next_subgoal`.

    The planner names a skill in its sub-goal text ("Run repo-fault-localize
    on ..."). We only care about skills already present in history, so a 3rd
    invocation of a heavily-reused skill is what trips the cap — we never block
    a brand-new skill the planner has not used yet.
    """
    norm_next = normalize_subgoal(next_subgoal)
    if not norm_next:
        return None
    seen: set[str] = set()
    for h in history:
        for s in (h.get("skills") or []):
            seen.add(s)
    next_words = set(norm_next.split())
    for skill in seen:
        norm_skill = normalize_subgoal(skill)
        if norm_skill:
            # A skill matches if any of its words appear in next_subgoal's words.
            skill_words = set(norm_skill.split())
            if skill_words & next_words:  # intersection
                return skill
    return None


def replan_should_stop(
    next_subgoal: str,
    history: list[dict],
    *,
    max_skill_repeats: int = 2,
) -> ReplanStop:
    """Decide whether the replan loop should stop before running next_subgoal.

    Two no-progress signals:
      1. duplicate_subgoal  -- next_subgoal normalizes equal to the MOST RECENT
         history step (the planner is re-emitting the same step).
      2. skill_repeat_cap   -- next_subgoal targets a skill already used >=
         max_skill_repeats times across history (prevents the "repo-fault-
         localize 4x" thrash).

    Pure: reads only its arguments. Returns ReplanStop(stop=False, reason="")
    when neither signal fires. An empty next_subgoal never trips (the loop's own
    done/empty check owns that case).
    """
    norm_next = normalize_subgoal(next_subgoal)
    if not norm_next:
        return ReplanStop(False, "")

    # (1) immediate duplicate of the last emitted step
    if history:
        if normalize_subgoal(history[-1].get("step", "")) == norm_next:
            return ReplanStop(True, "duplicate_subgoal")

    # (2) skill over-use cap
    skill = _skill_for_next(next_subgoal, history)
    if skill is not None and count_skill_uses(history, skill) >= max_skill_repeats:
        return ReplanStop(True, "skill_repeat_cap")

    return ReplanStop(False, "")
