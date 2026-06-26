"""Unit tests for the pure, deterministic complexity classifier.

No LLM, no graph, no env mutation beyond monkeypatched os.getenv. The classifier
is the ONLY place the skip decision is made, so these tests pin every branch.
"""
from __future__ import annotations

import pytest

from services.orchestrator.task_complexity import (
    Complexity,
    classify_complexity,
    conditional_gates_enabled,
)


@pytest.mark.mocked
class TestComplexityDataclass:
    def test_is_frozen_and_has_fields(self):
        c = Complexity(skip_ambiguity=True, skip_verify=True, reason="trivial")
        assert c.skip_ambiguity is True
        assert c.skip_verify is True
        assert c.reason == "trivial"
        with pytest.raises(Exception):
            c.skip_ambiguity = False  # frozen


@pytest.mark.mocked
class TestFeatureFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ENABLE_CONDITIONAL_GATES", raising=False)
        assert conditional_gates_enabled() is False

    @pytest.mark.parametrize("val", ["0", "false", "False", ""])
    def test_falsey_values_are_off(self, monkeypatch, val):
        monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", val)
        assert conditional_gates_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "True", "yes"])
    def test_truthy_values_are_on(self, monkeypatch, val):
        monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", val)
        assert conditional_gates_enabled() is True


@pytest.mark.mocked
class TestClassifyDisabled:
    def test_disabled_never_skips(self, monkeypatch):
        monkeypatch.delenv("ENABLE_CONDITIONAL_GATES", raising=False)
        c = classify_complexity("What is 2+2?")
        assert c == Complexity(skip_ambiguity=False, skip_verify=False, reason="feature disabled")

    def test_explicit_enabled_false_overrides_env(self, monkeypatch):
        monkeypatch.setenv("ENABLE_CONDITIONAL_GATES", "1")
        c = classify_complexity("What is 2+2?", enabled=False)
        assert c.skip_ambiguity is False
        assert c.skip_verify is False


@pytest.mark.mocked
class TestClassifyTrivial:
    """When enabled, clearly trivial tasks skip both gates."""

    @pytest.mark.parametrize(
        "task",
        [
            "What is 2+2?",
            "what is the capital of France",
            "Who wrote Hamlet?",
            "Define entropy.",
            "Convert 10 km to miles",
        ],
    )
    def test_trivial_question_skips_both(self, task):
        c = classify_complexity(task, enabled=True)
        assert c.skip_ambiguity is True
        assert c.skip_verify is True
        assert c.reason  # non-empty explanation


@pytest.mark.mocked
class TestClassifyAmbiguous:
    """Underspecified phrasings must NEVER be classified trivial."""

    @pytest.mark.parametrize(
        "task",
        ["make it better", "fix the thing", "improve this", "do that", "handle it"],
    )
    def test_ambiguous_does_not_skip_ambiguity(self, task):
        c = classify_complexity(task, enabled=True)
        assert c.skip_ambiguity is False


@pytest.mark.mocked
class TestClassifyNonTrivial:
    """Long / multi-clause / build-y tasks keep both gates."""

    @pytest.mark.parametrize(
        "task",
        [
            "Implement a rate limiter with a sliding window and tests",
            "Write a Python module that parses CSV, validates rows, and writes JSON",
            "Refactor the auth layer to support OAuth and add integration tests",
            "Build a REST API with three endpoints and a Postgres schema",
        ],
    )
    def test_nontrivial_keeps_both_gates(self, task):
        c = classify_complexity(task, enabled=True)
        assert c.skip_ambiguity is False
        assert c.skip_verify is False


@pytest.mark.mocked
class TestDeterminism:
    def test_same_input_same_output(self):
        a = classify_complexity("What is 2+2?", enabled=True)
        b = classify_complexity("What is 2+2?", enabled=True)
        assert a == b

    def test_empty_string_is_safe_no_skip(self):
        c = classify_complexity("", enabled=True)
        assert c.skip_ambiguity is False
        assert c.skip_verify is False
