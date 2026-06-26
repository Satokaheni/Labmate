from __future__ import annotations

import pytest

from services.orchestrator.iteration_budget import IterationBudget, CHEAP_TOOLS


@pytest.mark.mocked
class TestIterationBudgetCore:
    def test_starts_unused_with_full_remaining(self):
        b = IterationBudget(max_total=6)
        assert b.max_total == 6
        assert b.used == 0
        assert b.remaining == 6

    def test_consume_decrements_remaining_and_returns_true(self):
        b = IterationBudget(max_total=3)
        assert b.consume() is True
        assert b.used == 1
        assert b.remaining == 2

    def test_consume_returns_false_when_exhausted(self):
        b = IterationBudget(max_total=2)
        assert b.consume() is True
        assert b.consume() is True
        # Third consume is over cap -> False, used stays at the cap
        assert b.consume() is False
        assert b.used == 2
        assert b.remaining == 0

    def test_remaining_never_negative(self):
        b = IterationBudget(max_total=1)
        b.consume()
        b.consume()  # rejected
        b.consume()  # rejected
        assert b.remaining == 0
        assert b.remaining >= 0

    def test_cheap_tools_contains_pure_reads(self):
        assert "read_file" in CHEAP_TOOLS
        assert "list_dir" in CHEAP_TOOLS
        assert "code_semantic_search" in CHEAP_TOOLS
        # Writes / execution are NOT cheap
        assert "run_bash" not in CHEAP_TOOLS
        assert "write_file" not in CHEAP_TOOLS
        assert "call_skill_tool" not in CHEAP_TOOLS
        assert "finish" not in CHEAP_TOOLS


@pytest.mark.mocked
class TestIterationBudgetRefund:
    def test_refund_restores_one_unit(self):
        b = IterationBudget(max_total=3)
        b.consume()
        b.consume()
        assert b.used == 2
        b.refund()
        assert b.used == 1
        assert b.remaining == 2

    def test_refund_cannot_go_below_zero(self):
        b = IterationBudget(max_total=3)
        # No consume yet — refund must be a no-op, not negative.
        b.refund()
        assert b.used == 0
        assert b.remaining == 3

    def test_refund_cannot_exceed_max_total(self):
        b = IterationBudget(max_total=2)
        b.consume()
        # Two refunds against a single consume: used floors at 0, so remaining
        # never exceeds max_total (a refund cannot manufacture budget).
        b.refund()
        b.refund()
        assert b.used == 0
        assert b.remaining == 2  # not 3

    def test_refund_then_consume_allows_extra_turn(self):
        # Cap of 2: consume a cheap turn, refund it, then 2 working turns fit.
        b = IterationBudget(max_total=2)
        assert b.consume() is True   # cheap turn
        b.refund()                   # refunded
        assert b.consume() is True   # work 1
        assert b.consume() is True   # work 2
        assert b.consume() is False  # now exhausted
        assert b.used == 2
