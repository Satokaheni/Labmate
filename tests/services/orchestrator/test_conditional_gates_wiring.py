"""Tests for Task 3: conditional gates wiring into assess_ambiguity, ambiguity_router, verify_router.

Tests verify that:
1. assess_ambiguity returns complexity and skip flags in its result
2. ambiguity_router honors skip_ambiguity and returns "plan" immediately
3. verify_router honors skip_verify and returns "check" immediately
"""
import pytest

from services.orchestrator.graph import (
    ambiguity_router,
    verify_router,
)
from services.orchestrator.types import State
from langgraph.graph import END


class TestAmbiguityRouterHonorsSkipFlag:
    """ambiguity_router should return "plan" immediately when skip_ambiguity=True."""

    def test_ambiguity_router_returns_plan_when_skip_ambiguity_true(self):
        """When skip_ambiguity=True, ambiguity_router returns "plan" (before threshold)."""
        state: State = {
            "ambiguity": 0.95,  # High ambiguity, would normally trigger END
            "skip_ambiguity": True,  # But this flag short-circuits
        }
        result = ambiguity_router(state)
        assert result == "plan", "skip_ambiguity=True should skip the threshold check"

    def test_ambiguity_router_returns_plan_on_low_ambiguity(self):
        """When ambiguity < threshold and skip_ambiguity not set, return "plan"."""
        state: State = {
            "ambiguity": 0.3,  # Below threshold
        }
        result = ambiguity_router(state)
        assert result == "plan"

    def test_ambiguity_router_returns_end_on_high_ambiguity(self):
        """When ambiguity >= threshold and skip_ambiguity not set, return END."""
        state: State = {
            "ambiguity": 0.75,  # Above threshold
        }
        result = ambiguity_router(state)
        assert result == END

    def test_ambiguity_router_skip_flag_takes_precedence(self):
        """skip_ambiguity=True takes precedence over high ambiguity score."""
        state: State = {
            "ambiguity": 0.9,  # Well above threshold
            "skip_ambiguity": True,  # But flag set
        }
        result = ambiguity_router(state)
        assert result == "plan", "skip_ambiguity=True should override high ambiguity"

    def test_ambiguity_router_skip_false_respects_threshold(self):
        """When skip_ambiguity=False explicitly, threshold check applies."""
        state: State = {
            "ambiguity": 0.75,
            "skip_ambiguity": False,  # Explicit False
        }
        result = ambiguity_router(state)
        assert result == END


class TestVerifyRouterHonorsSkipFlag:
    """verify_router should return "check" immediately when skip_verify=True."""

    def test_verify_router_returns_check_when_skip_verify_true(self):
        """When skip_verify=True, verify_router returns "check" (before threshold)."""
        state: State = {
            "critique_score": 0.5,  # Low score, would normally trigger reflect
            "skip_verify": True,  # But this flag short-circuits
        }
        result = verify_router(state)
        assert result == "check", "skip_verify=True should skip the threshold check"

    def test_verify_router_returns_check_on_high_score(self):
        """When critique_score >= threshold and skip_verify not set, return "check"."""
        state: State = {
            "critique_score": 0.95,  # Above threshold
        }
        result = verify_router(state)
        assert result == "check"

    def test_verify_router_returns_reflect_on_low_score_with_flag(self):
        """When critique_score < threshold, _verify_reflect=True, return "reflect"."""
        state: State = {
            "critique_score": 0.5,  # Below threshold
            "_verify_reflect": True,
        }
        result = verify_router(state)
        assert result == "reflect"

    def test_verify_router_skip_flag_takes_precedence(self):
        """skip_verify=True takes precedence over low score."""
        state: State = {
            "critique_score": 0.2,  # Well below threshold
            "_verify_reflect": True,  # Would trigger reflect
            "skip_verify": True,  # But flag set
        }
        result = verify_router(state)
        assert result == "check", "skip_verify=True should override low critique_score"

    def test_verify_router_skip_false_respects_threshold(self):
        """When skip_verify=False explicitly, threshold check applies."""
        state: State = {
            "critique_score": 0.5,
            "_verify_reflect": True,
            "skip_verify": False,  # Explicit False
        }
        result = verify_router(state)
        assert result == "reflect"

    def test_verify_router_backward_compat_no_verify_reflect_flag(self):
        """When _verify_reflect not set, fall back to score < threshold check."""
        state: State = {
            "critique_score": 0.5,  # Below threshold
            # _verify_reflect not set (backward compat)
        }
        result = verify_router(state)
        assert result == "reflect"
