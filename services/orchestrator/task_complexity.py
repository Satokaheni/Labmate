"""Pure, deterministic, LLM-free task-complexity classifier.

The orchestrator runs an LLM `assess_ambiguity` gate on EVERY task and a
`critique` (verify) gate on EVERY code/writing artifact. Both are wasted
latency on clearly trivial inputs (e.g. "What is 2+2?"). This module makes a
cheap, deterministic decision about whether a task is trivial enough that those
gates can be safely skipped.

Design rules (see the implementation plan's Global Constraints):
  * PURE: no network, no model calls, no clock, no randomness, no I/O.
  * CONSERVATIVE: only skip when the task is CLEARLY trivial. A false "skip"
    is worse than a false "don't skip" (skipping a genuinely-ambiguous task
    would let the agent guess), so every ambiguity signal blocks skip_ambiguity.
  * OFF BY DEFAULT: `ENABLE_CONDITIONAL_GATES` is unset/falsey -> never skip.

Relationship to the direct-answer fast-path: a skill-less single-intent task
(the fast-path's trigger) is the canonical "trivial" notion. This classifier is
a deterministic, conservative pre-filter layered on top: it skips the ambiguity
gate only for short, clearly-scoped question/lookup phrasings that the ambiguity
model would itself score ~0.0-0.1.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Complexity:
    """Deterministic verdict for a single task.

    skip_ambiguity: skip the LLM assess_ambiguity gate (task is clearly specified).
    skip_verify:    skip the critique/verify gate (task won't produce a risky artifact).
    reason:         short human-readable explanation (for events / debugging).
    """
    skip_ambiguity: bool
    skip_verify: bool
    reason: str


# Master flag. OFF by default. Matches the ENABLE_DIRECT_ANSWER_FASTPATH convention:
# a value in the falsey set means OFF; anything else means ON.
_FALSEY = ("0", "false", "False", "")


def conditional_gates_enabled() -> bool:
    """True when ENABLE_CONDITIONAL_GATES is set to a non-falsey value. OFF by default."""
    return os.getenv("ENABLE_CONDITIONAL_GATES", "0") not in _FALSEY


# Max word count for a task to even be CONSIDERED trivial. Short tasks only.
# Configurable for tuning; conservative default. Read at call time so tests/env
# can override without reimporting the module.
def _trivial_max_words() -> int:
    try:
        return int(os.getenv("TRIVIAL_MAX_WORDS", "12"))
    except (TypeError, ValueError):
        return 12


# Phrasings with an undefined referent / no concrete deliverable. If ANY of these
# match, the task is treated as potentially ambiguous and skip_ambiguity is False.
# Mirrors the assess_ambiguity prompt's HIGH-score triggers (undefined "it"/"the
# thing"/"this", vague "make it better" verbs).
_AMBIGUOUS_PATTERNS = (
    re.compile(r"\b(make|fix|improve|handle|do|change|update|refactor)\s+(it|this|that|the\s+thing)\b", re.I),
    re.compile(r"^\s*(make|fix|improve|handle|do|change)\s+(it|this|that)\s*$", re.I),
    re.compile(r"\bthe\s+thing\b", re.I),
)

# Signals that a task will likely produce a code/writing ARTIFACT that warrants the
# verify gate. If ANY match, skip_verify is False.
_ARTIFACT_PATTERNS = (
    re.compile(r"\b(write|implement|build|create|refactor|generate|draft|code|design)\b", re.I),
    re.compile(r"\b(function|module|class|api|endpoint|script|schema|test|tests|component|essay|paper|report)\b", re.I),
    re.compile(r"```"),  # the task itself contains a code block
)

# Signals that a task is a TRIVIAL question / lookup / conversion — the only family
# we allow to skip the ambiguity gate. Must be short AND match one of these AND not
# trip an ambiguity pattern.
_TRIVIAL_PATTERNS = (
    re.compile(r"^\s*(what|who|when|where|which|how\s+(much|many|far|old))\b", re.I),
    re.compile(r"^\s*(define|explain|summarize|name|list|tell\s+me)\b", re.I),
    re.compile(r"^\s*convert\b", re.I),
    re.compile(r"^\s*\d+\s*[-+*/]\s*\d+", re.I),  # bare arithmetic
)


def classify_complexity(task: str, *, enabled: bool | None = None) -> Complexity:
    """Classify a task's complexity. Pure & deterministic.

    When `enabled` is None, the master env flag decides. When the feature is
    disabled, ALWAYS returns no-skip (regression-safe). When enabled, applies a
    conservative heuristic: only short, clearly-scoped question/lookup tasks with
    no ambiguity markers and no artifact markers skip the gates.
    """
    if enabled is None:
        enabled = conditional_gates_enabled()
    if not enabled:
        return Complexity(skip_ambiguity=False, skip_verify=False, reason="feature disabled")

    text = (task or "").strip()
    if not text:
        return Complexity(skip_ambiguity=False, skip_verify=False, reason="empty task")

    words = text.split()
    is_short = len(words) <= _trivial_max_words()
    is_ambiguous = any(p.search(text) for p in _AMBIGUOUS_PATTERNS)
    is_artifact = any(p.search(text) for p in _ARTIFACT_PATTERNS)
    looks_trivial = any(p.search(text) for p in _TRIVIAL_PATTERNS)

    # skip_ambiguity: only when clearly a short, well-scoped question/lookup with
    # NO ambiguity markers. Conservative: a single ambiguity signal blocks it.
    skip_ambiguity = is_short and looks_trivial and not is_ambiguous

    # skip_verify: only when the task is short and clearly NOT going to produce a
    # code/writing artifact worth critiquing. Ambiguity also blocks it (a vague
    # task might still produce an artifact we'd want verified).
    skip_verify = is_short and not is_artifact and not is_ambiguous

    if skip_ambiguity and skip_verify:
        reason = "trivial question/lookup: short, well-scoped, no artifact"
    elif skip_verify:
        reason = "short non-artifact task: verify gate not warranted"
    elif is_ambiguous:
        reason = "ambiguity markers present: gates required"
    elif is_artifact:
        reason = "artifact-producing task: verify gate required"
    else:
        reason = "not clearly trivial: gates required"

    return Complexity(skip_ambiguity=skip_ambiguity, skip_verify=skip_verify, reason=reason)
