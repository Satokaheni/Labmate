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
  3. If a TEST-AUTHORING phrase appears ("write a test", "add tests", "create a test",
     "author tests") -> requires edit. Narrow: an authoring verb followed by
     "test"/"tests", not matching "testing"/"latest" due to word boundary.
  4. Otherwise -> no edit (pure read/answer). "review", "find bugs", "summarize",
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

# Rule 3: test-authoring — "write a unit test", "add tests", "create a test".
# Narrow: an authoring verb immediately followed (optionally via a/an/some/new/
# unit/integration) by the word "test"/"tests". "testing"/"latest" do NOT match
# because \btests?\b requires a word boundary after "test".
_TEST_AUTHOR_RE = re.compile(
    r"\b(?:writ(?:e|es|ing)|add(?:s|ed|ing)?|creat(?:e|es|ing|ed)|author(?:s|ed|ing)?)\s+"
    r"(?:a\s+|an\s+|some\s+|new\s+|unit\s+|integration\s+)*tests?\b",
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

    if _TEST_AUTHOR_RE.search(text):
        return EditIntent(requires_edit=True, reason="test-authoring (write/add a test)")

    return EditIntent(
        requires_edit=False,
        reason="read/answer goal (no edit verb or verification phrase)",
    )


def requires_editing(goal: str, *, enabled: bool | None = None) -> bool:
    """Bool-only convenience wrapper over classify_edit_intent()."""
    return classify_edit_intent(goal, enabled=enabled).requires_edit


# ── Expose-bug intent (inverted success signal) ───────────────────────────────
# For "write a test that EXPOSES / reproduces the bug" goals, a test that RUNS
# and FAILS is the SUCCESS signal (a passing test would NOT expose the bug). The
# default verification policy (nudge toward / credit only a PASSING run) is
# therefore inverted for these goals. Kept deliberately NARROW so a normal
# fix-goal ("fix the bug and make the tests pass") is never misclassified — a
# misfire would let a fix goal finish with a failing suite.
_EXPOSE_BUG_RE = re.compile(
    r"(?:"
    # "test that/which/to [up to 3 words] expose|reproduce|demonstrate|reveal|trigger|surface|catch"
    r"tests?\s+(?:that|which|to)\s+(?:\w+\s+){0,3}?"
    r"(?:expos|reproduc|demonstrat|reveal|trigger|surfac|catch)"
    # "expose|reproduce|... the bug / it / the issue"
    r"|(?:expos|reproduc|demonstrat|reveal|trigger|surfac|catch)\w*\s+(?:the\s+)?(?:bug|issue|defect|it)\b"
    # explicit failing-test phrasings
    r"|failing\s+tests?\b|tests?\s+that\s+fails?\b"
    r")",
    re.IGNORECASE,
)

# A pass/fix-completion intent ("until they pass", "make the tests pass", "so the
# tests pass") means a PASSING test is the desired end-state, so the inversion
# must NOT apply even if an expose verb is also present.
_PASS_INTENT_RE = re.compile(
    r"(?:until\s+(?:they|it|the\s+tests?)\s+pass"
    r"|make\s+(?:the\s+|all\s+)?(?:tests?|it|build)\s+\w*\s*pass"
    r"|so\s+(?:the\s+|all\s+)?tests?\s+pass"
    r"|tests?\s+(?:now\s+)?pass)",
    re.IGNORECASE,
)


def exposes_bug_intent(goal: str) -> bool:
    """True iff the goal asks for a test that EXPOSES/reproduces a bug (a FAILING
    run is the success signal), and there is no competing "make the tests pass"
    intent. Pure/deterministic; no I/O. See the inversion note above.
    """
    text = goal or ""
    if _PASS_INTENT_RE.search(text):
        return False
    return bool(_EXPOSE_BUG_RE.search(text))
