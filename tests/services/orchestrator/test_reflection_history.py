"""Unit tests for collect_prior_reflections — pure, no LLM, no services."""
from __future__ import annotations

import pytest

from services.orchestrator.graph import (
    collect_prior_reflections,
    MAX_PRIOR_REFLECTIONS,
)


def _state(messages):
    # Minimal State-shaped dict; only `messages` is read by the helper.
    return {"messages": messages}


class TestCollectPriorReflections:
    def test_empty_messages_returns_empty_list(self):
        assert collect_prior_reflections(_state([]), "g1") == []

    def test_missing_messages_key_returns_empty_list(self):
        # Resumed checkpoints may not carry `messages`; must not raise.
        assert collect_prior_reflections({}, "g1") == []

    def test_returns_only_matching_goal_id_in_order(self):
        messages = [
            {"role": "reflection", "goal_id": "g1", "content": "first"},
            {"role": "reflection", "goal_id": "g2", "content": "other"},
            {"role": "reflection", "goal_id": "g1", "content": "second"},
        ]
        assert collect_prior_reflections(_state(messages), "g1") == ["first", "second"]

    def test_ignores_non_reflection_roles(self):
        messages = [
            {"role": "user", "goal_id": "g1", "content": "noise"},
            {"role": "reflection", "goal_id": "g1", "content": "keep"},
        ]
        assert collect_prior_reflections(_state(messages), "g1") == ["keep"]

    def test_ignores_legacy_entries_without_goal_id(self):
        # Pre-change reflections had no goal_id; they must be skipped, not crash.
        messages = [
            {"role": "reflection", "content": "legacy-no-goal-id"},
            {"role": "reflection", "goal_id": "g1", "content": "tagged"},
        ]
        assert collect_prior_reflections(_state(messages), "g1") == ["tagged"]

    def test_caps_to_last_n_preserving_order(self):
        messages = [
            {"role": "reflection", "goal_id": "g1", "content": f"fix {i}"}
            for i in range(1, 6)  # fix 1 .. fix 5
        ]
        out = collect_prior_reflections(_state(messages), "g1")
        assert out == ["fix 3", "fix 4", "fix 5"]
        assert len(out) == MAX_PRIOR_REFLECTIONS

    def test_explicit_cap_argument_overrides_default(self):
        messages = [
            {"role": "reflection", "goal_id": "g1", "content": f"fix {i}"}
            for i in range(1, 6)
        ]
        assert collect_prior_reflections(_state(messages), "g1", cap=2) == ["fix 4", "fix 5"]

    def test_cap_zero_returns_empty(self):
        messages = [{"role": "reflection", "goal_id": "g1", "content": "x"}]
        assert collect_prior_reflections(_state(messages), "g1", cap=0) == []

    def test_default_cap_is_three(self):
        assert MAX_PRIOR_REFLECTIONS == 3
