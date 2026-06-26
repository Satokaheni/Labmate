from __future__ import annotations

import pytest

from services.orchestrator.tool_grounding import (
    ground_tool_result,
    DEFAULT_TOOL_RESULT_BUDGET,
)

pytestmark = pytest.mark.mocked


def test_default_budget_is_generous():
    # Default must be far above the old 4000/2000 hard cuts so real output flows.
    assert DEFAULT_TOOL_RESULT_BUDGET >= 16000


def test_under_budget_is_verbatim():
    text = "x" * 500
    assert ground_tool_result(text, budget=16000) == text


def test_exactly_at_budget_is_verbatim():
    text = "y" * 16000
    out = ground_tool_result(text, budget=16000)
    assert out == text
    assert "truncated" not in out


def test_empty_string_is_verbatim():
    assert ground_tool_result("", budget=16000) == ""


def test_over_budget_keeps_head_and_tail():
    head_block = "HEAD" + ("a" * 9996)      # 10000 chars
    tail_block = ("b" * 9988) + "TAILMARK"  # 10000 chars
    text = head_block + tail_block          # 20000 chars
    out = ground_tool_result(text, budget=8000)
    # Start of the original survives.
    assert out.startswith("HEAD")
    # End of the original survives (critical: test failures print at the end).
    assert out.endswith("TAILMARK")
    # A truncation marker sits between head and tail.
    assert "truncated" in out
    assert "…" in out


def test_over_budget_marker_reports_dropped_count():
    text = "z" * 40000
    budget = 16000
    out = ground_tool_result(text, budget=budget)
    dropped = len(text) - (len(out) - _marker_len(out))
    # The number reported in the marker equals chars actually dropped.
    assert f"{dropped} chars truncated" in out


def test_over_budget_payload_is_about_budget_sized():
    text = "q" * 100000
    budget = 16000
    out = ground_tool_result(text, budget=budget)
    # Kept content (excluding the marker) must not exceed the budget.
    kept = len(out) - _marker_len(out)
    assert kept <= budget


def test_tiny_budget_still_returns_both_ends():
    text = "START" + ("m" * 100) + "END"
    out = ground_tool_result(text, budget=10)
    assert out.startswith("ST")    # some head
    assert out.endswith("ND")      # some tail
    assert "truncated" in out


def _marker_len(out: str) -> int:
    # Length of the "\n…[N chars truncated]…\n" segment inside `out`.
    import re
    m = re.search(r"\n…\[\d+ chars truncated\]…\n", out)
    assert m is not None, f"no marker found in: {out[:80]!r}..."
    return len(m.group(0))
