"""Edit-intent classifier (deterministic heuristics).

Pure module deciding whether a goal needs file edits / verification — and thus
should enter the multi-tool ReAct loop instead of single-skill dispatch.

Tests verify:
  - EditIntent dataclass structure (frozen)
  - route_edit_to_react_enabled() reads env, defaults ON
  - requires_editing() flags edit/fix/verify phrases True, read/answer phrases False
  - the A/B cases c1/c2 -> True, c4 -> False
  - feature can be disabled (returns False)
  - pure + deterministic + case-insensitive
"""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError

import pytest

from services.orchestrator.edit_intent import (
    EditIntent,
    classify_edit_intent,
    exposes_bug_intent,
    requires_editing,
    requires_local_tools,
    route_edit_to_react_enabled,
)


class TestExposesBugIntent:
    def test_c3_write_test_that_exposes_it(self):
        assert (
            exposes_bug_intent(
                "Find the bug in /workspace/ab_off.py and write a unit test that exposes it."
            )
            is True
        )

    def test_reproduce_phrasing(self):
        assert exposes_bug_intent("Write a test that reproduces the crash.") is True

    def test_failing_test_phrasing(self):
        assert exposes_bug_intent("Add a failing test for the off-by-one bug.") is True

    def test_demonstrate_the_bug(self):
        assert exposes_bug_intent("Create a test to demonstrate the defect.") is True

    def test_c1_fix_until_pass_is_not_expose(self):
        # A fix goal whose end-state is a PASSING suite must NOT invert.
        assert (
            exposes_bug_intent(
                "Generate unit tests, run them, find and fix the bug, and re-run until they pass."
            )
            is False
        )

    def test_make_tests_pass_disqualifies(self):
        assert (
            exposes_bug_intent("Write a test that exposes the bug, then fix it so the tests pass.")
            is False
        )

    def test_plain_test_authoring_is_not_expose(self):
        assert exposes_bug_intent("Write unit tests for the parser.") is False

    def test_review_only_is_not_expose(self):
        assert exposes_bug_intent("Review the auth module for issues.") is False


class TestEditIntentDataclass:
    def test_has_required_fields(self):
        e = EditIntent(requires_edit=True, reason="fix verb")
        assert e.requires_edit is True
        assert e.reason == "fix verb"

    def test_is_frozen(self):
        e = EditIntent(requires_edit=True, reason="x")
        with pytest.raises(FrozenInstanceError):
            e.requires_edit = False


class TestRouteEditToReactEnabled:
    def test_defaults_to_true(self):
        original = os.environ.pop("ROUTE_EDIT_TO_REACT", None)
        try:
            assert route_edit_to_react_enabled() is True
        finally:
            if original is not None:
                os.environ["ROUTE_EDIT_TO_REACT"] = original

    @pytest.mark.parametrize(
        "falsey", ["0", "false", "False", "FALSE", "no", "No", "off", "Off", "OFF"]
    )
    def test_false_for_falsey(self, falsey):
        original = os.environ.get("ROUTE_EDIT_TO_REACT")
        try:
            os.environ["ROUTE_EDIT_TO_REACT"] = falsey
            assert route_edit_to_react_enabled() is False
        finally:
            if original is not None:
                os.environ["ROUTE_EDIT_TO_REACT"] = original
            else:
                os.environ.pop("ROUTE_EDIT_TO_REACT", None)

    @pytest.mark.parametrize("truthy", ["1", "true", "True", "yes", "on", "ON"])
    def test_true_for_truthy(self, truthy):
        original = os.environ.get("ROUTE_EDIT_TO_REACT")
        try:
            os.environ["ROUTE_EDIT_TO_REACT"] = truthy
            assert route_edit_to_react_enabled() is True
        finally:
            if original is not None:
                os.environ["ROUTE_EDIT_TO_REACT"] = original
            else:
                os.environ.pop("ROUTE_EDIT_TO_REACT", None)


# Phrases that MUST be flagged as needing edits.
EDIT_GOALS = [
    "Fix the off-by-one bug in factorial",
    "Make the tests pass",
    "Refactor the parser to use a state machine",
    "Implement the missing validate() method",
    "Patch the regex so it accepts unicode",
    "Review the code and then fix the bugs you find",
    "Resolve the failing assertion in test_app.py",
    "Make it work — the import is broken",
    "Generate tests for sort(), then find and fix the bug",
    "Edit config.py to add a timeout option",
    "Correct the typo that breaks the build",
    "Apply the fix and rerun the suite",
    "Debug and repair the broken endpoint",
]

# Phrases that MUST NOT be flagged (pure read / answer / analysis).
READ_GOALS = [
    "Summarize what this module does",
    "What is the capital of France?",
    "Find bugs in the authentication handler",
    "Explain how the event loop works",
    "Review this file for code smells",
    "List the functions defined in utils.py",
    "What does the parse_header function return?",
    "Describe the architecture of the orchestrator",
    "Search for papers about retrieval augmentation",
]


class TestRequiresEditing:
    @pytest.mark.parametrize("goal", EDIT_GOALS)
    def test_edit_goals_true(self, goal):
        assert requires_editing(goal, enabled=True) is True, goal

    @pytest.mark.parametrize("goal", READ_GOALS)
    def test_read_goals_false(self, goal):
        assert requires_editing(goal, enabled=True) is False, goal

    def test_ab_case_c1_true(self):
        assert (
            requires_editing(
                "Generate unit tests for the factorial function, then find and fix the bug",
                enabled=True,
            )
            is True
        )

    def test_ab_case_c2_true(self):
        assert (
            requires_editing(
                "Review /workspace/ab_buggy.py for bugs, then fix the code",
                enabled=True,
            )
            is True
        )

    def test_ab_case_c4_false(self):
        assert requires_editing("Find bugs in /workspace/ab_review.py", enabled=True) is False

    def test_disabled_returns_false(self):
        # Even an obvious edit goal is False when the feature is off.
        assert requires_editing("Fix the bug", enabled=False) is False

    def test_none_enabled_consults_env_default_on(self):
        original = os.environ.pop("ROUTE_EDIT_TO_REACT", None)
        try:
            assert requires_editing("Fix the bug", enabled=None) is True
        finally:
            if original is not None:
                os.environ["ROUTE_EDIT_TO_REACT"] = original

    def test_deterministic(self):
        g = "Refactor and fix the parser"
        assert requires_editing(g, enabled=True) == requires_editing(g, enabled=True)

    def test_case_insensitive(self):
        assert requires_editing("FIX THE BUG", enabled=True) is True
        assert requires_editing("fix the bug", enabled=True) is True

    def test_find_bugs_then_fix_is_true(self):
        # "find bugs" alone is read-only, but a trailing fix verb flips it.
        assert requires_editing("Find bugs in app.py and fix them", enabled=True) is True

    def test_review_without_fix_is_false(self):
        assert requires_editing("Review app.py", enabled=True) is False


class TestTestAuthoringIntent:
    """Test-authoring goals (write/add/create a test) must route to ReAct loop."""

    def test_write_a_unit_test_is_edit_intent(self):
        assert (
            requires_editing(
                "Find the bug in /workspace/ab_off.py and write a unit test that exposes it.",
                enabled=True,
            )
            is True
        )

    def test_add_tests_is_edit_intent(self):
        assert requires_editing("Add tests for the parser module.", enabled=True) is True

    def test_create_a_test_is_edit_intent(self):
        assert requires_editing("Create a test that reproduces the crash.", enabled=True) is True

    def test_find_bugs_only_stays_read_only(self):
        # c4 control must NOT become edit-intent.
        assert requires_editing("Find bugs in /workspace/ab_buggy.py.", enabled=True) is False

    def test_review_only_stays_read_only(self):
        assert requires_editing("Review the auth module for issues.", enabled=True) is False

    def test_testing_word_does_not_falsely_trigger(self):
        # "testing" / "latest" must not match the test-authoring rule.
        assert requires_editing("Explain the latest testing strategy.", enabled=True) is False


class TestClassifyEditIntent:
    def test_returns_editintent_with_reason(self):
        v = classify_edit_intent("Fix the bug", enabled=True)
        assert isinstance(v, EditIntent)
        assert v.requires_edit is True
        assert v.reason  # non-empty

    def test_disabled_reason(self):
        v = classify_edit_intent("Fix the bug", enabled=False)
        assert v.requires_edit is False
        assert "disabled" in v.reason.lower()


# ── requires_local_tools (Piece 5 / fix-B: file-access routing) ──────────────
# True = the goal needs the local file/edit tools -> must enter the ReAct loop.
# = requires_editing(...) OR an explicit file-ACCESS intent (read/show/list/cat/
# ... a file/dir/path). False = stays on the single read-only skill fast-path.

LOCAL_TOOLS_TRUE_GOALS = [
    # explicit file-access intent (Rule 5)
    "read x.py and summarize",
    "show me the contents of config.py",
    "list the files in src/",
    "cat the file bot.py",
    "open services/app.py",
    "print the contents of /tmp/data.json",
    "inspect the file",
    "read the directory listing",
    # existing edit-intent goals must still route True via requires_editing
    "fix the bug",
    "make the tests pass",
    "write a test",
]

LOCAL_TOOLS_FALSE_GOALS = [
    "review the design of the module",
    "find bugs in the approach",
    "summarize the architecture",
    "explain how routing works",
    "what is 2 + 2?",
    "list the tradeoffs of microservices",  # no file/path target
    "review the file format design",  # has "file" but no read-verb targeting it
]


class TestRequiresLocalTools:
    @pytest.mark.parametrize("goal", LOCAL_TOOLS_TRUE_GOALS)
    def test_true_goals(self, goal):
        assert requires_local_tools(goal, enabled=True) is True, goal

    @pytest.mark.parametrize("goal", LOCAL_TOOLS_FALSE_GOALS)
    def test_false_goals(self, goal):
        assert requires_local_tools(goal, enabled=True) is False, goal

    def test_disabled_returns_false(self):
        assert requires_local_tools("read x.py", enabled=False) is False

    def test_none_enabled_consults_env_default_on(self):
        original = os.environ.pop("ROUTE_EDIT_TO_REACT", None)
        try:
            assert requires_local_tools("read x.py", enabled=None) is True
        finally:
            if original is not None:
                os.environ["ROUTE_EDIT_TO_REACT"] = original
