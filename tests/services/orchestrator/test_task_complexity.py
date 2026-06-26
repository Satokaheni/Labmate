"""Task complexity classifier (deterministic heuristics).

Pure module for classifying task complexity to enable conditional gates
(skip ambiguity/verify for trivial tasks).

Tests verify:
  - Complexity dataclass structure
  - conditional_gates_enabled() reads env
  - classify_complexity() applies deterministic heuristics
  - Feature can be disabled entirely (returns Complexity(False, False, "feature disabled"))
"""
from __future__ import annotations

import os
import pytest

from services.orchestrator.task_complexity import (
    Complexity,
    conditional_gates_enabled,
    classify_complexity,
)


class TestComplexityDataclass:
    """Complexity dataclass exists and is frozen."""

    def test_complexity_has_required_fields(self):
        """Complexity has skip_ambiguity, skip_verify, reason."""
        c = Complexity(skip_ambiguity=True, skip_verify=False, reason="simple query")
        assert c.skip_ambiguity is True
        assert c.skip_verify is False
        assert c.reason == "simple query"

    def test_complexity_is_frozen(self):
        """Complexity is immutable (dataclass frozen=True)."""
        c = Complexity(skip_ambiguity=True, skip_verify=True, reason="test")
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            c.skip_ambiguity = False


class TestConditionalGatesEnabled:
    """conditional_gates_enabled() reads ENABLE_CONDITIONAL_GATES env."""

    def test_defaults_to_false(self):
        """Env OFF by default."""
        # Save the original env var
        original = os.environ.pop("ENABLE_CONDITIONAL_GATES", None)
        try:
            # Make sure it's not set
            assert conditional_gates_enabled() is False
        finally:
            if original is not None:
                os.environ["ENABLE_CONDITIONAL_GATES"] = original

    @pytest.mark.parametrize(
        "falsey_value",
        ["", "0", "false", "False", "FALSE", "no", "No", "off", "Off", "OFF"],
    )
    def test_false_for_explicit_falsey_values(self, falsey_value):
        """Env var set to falsey values (0, false, no, off) → False."""
        original = os.environ.get("ENABLE_CONDITIONAL_GATES")
        try:
            os.environ["ENABLE_CONDITIONAL_GATES"] = falsey_value
            assert conditional_gates_enabled() is False, f"Failed for value: {falsey_value!r}"
        finally:
            if original is not None:
                os.environ["ENABLE_CONDITIONAL_GATES"] = original
            else:
                os.environ.pop("ENABLE_CONDITIONAL_GATES", None)

    @pytest.mark.parametrize(
        "truthy_value",
        ["1", "true", "True", "TRUE", "yes", "Yes", "on", "On", "ON"],
    )
    def test_true_for_explicit_truthy_values(self, truthy_value):
        """Env var set to truthy values (1, true, yes, on) → True."""
        original = os.environ.get("ENABLE_CONDITIONAL_GATES")
        try:
            os.environ["ENABLE_CONDITIONAL_GATES"] = truthy_value
            assert conditional_gates_enabled() is True, f"Failed for value: {truthy_value!r}"
        finally:
            if original is not None:
                os.environ["ENABLE_CONDITIONAL_GATES"] = original
            else:
                os.environ.pop("ENABLE_CONDITIONAL_GATES", None)


class TestClassifyComplexity:
    """classify_complexity() applies deterministic heuristics."""

    def test_disabled_feature_returns_skip_both(self):
        """Feature disabled → Complexity(False, False, "feature disabled")."""
        result = classify_complexity("What is 2+2?", enabled=False)
        assert result.skip_ambiguity is False
        assert result.skip_verify is False
        assert result.reason == "feature disabled"

    def test_enabled_false_at_runtime(self):
        """enabled=False overrides env; returns feature disabled."""
        result = classify_complexity("Write code", enabled=False)
        assert result.skip_ambiguity is False
        assert result.skip_verify is False

    def test_simple_arithmetic_skips_both_gates(self):
        """Simple arithmetic → skip_ambiguity=True, skip_verify=True."""
        result = classify_complexity("What is 2+2?", enabled=True)
        assert result.skip_ambiguity is True
        assert result.skip_verify is True
        assert "arithmetic" in result.reason.lower()

    def test_simple_fact_query_skips_both_gates(self):
        """Simple fact lookup → skip both."""
        result = classify_complexity("What is the capital of France?", enabled=True)
        assert result.skip_ambiguity is True
        assert result.skip_verify is True

    def test_code_writing_skips_ambiguity_not_verify(self):
        """Code/writing tasks → skip_ambiguity=True, skip_verify=False (need critique)."""
        result = classify_complexity("Write a Python function to reverse a list.", enabled=True)
        assert result.skip_ambiguity is True
        assert result.skip_verify is False

    def test_search_query_skips_ambiguity(self):
        """Web search / lookup → skip ambiguity, but not verify."""
        result = classify_complexity("Find research papers about machine learning.", enabled=True)
        assert result.skip_ambiguity is True
        assert result.skip_verify is False

    def test_complex_task_does_not_skip(self):
        """Complex, multi-step, ambiguous → skip neither gate."""
        result = classify_complexity(
            "Design a system for collaborative document editing with real-time updates, "
            "version control, and offline support.",
            enabled=True,
        )
        assert result.skip_ambiguity is False
        assert result.skip_verify is False

    def test_none_enabled_consults_env(self):
        """enabled=None → consults conditional_gates_enabled()."""
        original = os.environ.get("ENABLE_CONDITIONAL_GATES")
        try:
            # Test with feature OFF
            os.environ.pop("ENABLE_CONDITIONAL_GATES", None)
            result = classify_complexity("What is 2+2?", enabled=None)
            assert result.reason == "feature disabled"

            # Test with feature ON
            os.environ["ENABLE_CONDITIONAL_GATES"] = "1"
            result = classify_complexity("What is 2+2?", enabled=None)
            assert result.skip_ambiguity is True
        finally:
            if original is not None:
                os.environ["ENABLE_CONDITIONAL_GATES"] = original
            else:
                os.environ.pop("ENABLE_CONDITIONAL_GATES", None)

    def test_heuristic_is_deterministic(self):
        """Same task always produces same result."""
        task = "Write a function to compute Fibonacci numbers."
        result1 = classify_complexity(task, enabled=True)
        result2 = classify_complexity(task, enabled=True)
        assert result1.skip_ambiguity == result2.skip_ambiguity
        assert result1.skip_verify == result2.skip_verify
        assert result1.reason == result2.reason

    def test_case_insensitivity(self):
        """Heuristic is case-insensitive."""
        result1 = classify_complexity("WRITE A PYTHON FUNCTION", enabled=True)
        result2 = classify_complexity("write a python function", enabled=True)
        assert result1.skip_ambiguity == result2.skip_ambiguity
        assert result1.skip_verify == result2.skip_verify

    def test_multiline_task_handled(self):
        """Multiline task text is handled gracefully."""
        task = """Write a Python function that:
        - Takes a list of numbers
        - Returns the sum
        - Handles empty lists gracefully"""
        result = classify_complexity(task, enabled=True)
        assert isinstance(result.skip_ambiguity, bool)
        assert isinstance(result.skip_verify, bool)
        assert isinstance(result.reason, str)

    def test_pure_function_no_side_effects(self):
        """Function is pure — no state mutations, same task same output."""
        task = "What is the weather today?"
        result_before = classify_complexity(task, enabled=True)
        # Call it again; environment unchanged.
        result_after = classify_complexity(task, enabled=True)
        assert result_before == result_after
