from __future__ import annotations

import pytest

from services.orchestrator.progress_breaker import ProgressBreaker, ProgressStep


@pytest.mark.mocked
class TestProgressBreakerCore:
    def test_starts_at_zero_not_tripped(self):
        b = ProgressBreaker(default_cap=5)
        assert b.consecutive == 0
        assert b.tripped is False

    def test_no_progress_increments_consecutive(self):
        b = ProgressBreaker(default_cap=5)
        s = b.step(False, cap=5)
        assert isinstance(s, ProgressStep)
        assert s.consecutive == 1
        assert s.tripped is False
        s2 = b.step(False, cap=5)
        assert s2.consecutive == 2
        assert s2.tripped is False

    def test_progress_resets_consecutive_to_zero(self):
        b = ProgressBreaker(default_cap=5)
        b.step(False, cap=5)
        b.step(False, cap=5)
        assert b.consecutive == 2
        s = b.step(True, cap=5)
        assert s.consecutive == 0
        assert s.tripped is False
        assert b.consecutive == 0

    def test_trips_exactly_at_cap(self):
        b = ProgressBreaker(default_cap=5)
        # cap is 3: third consecutive no-progress turn trips.
        assert b.step(False, cap=3).tripped is False  # 1
        assert b.step(False, cap=3).tripped is False  # 2
        s = b.step(False, cap=3)                       # 3 == cap
        assert s.consecutive == 3
        assert s.tripped is True
        assert b.tripped is True

    def test_does_not_trip_below_cap(self):
        b = ProgressBreaker(default_cap=5)
        for _ in range(4):
            assert b.step(False, cap=5).tripped is False
        assert b.consecutive == 4

    def test_progress_before_cap_prevents_trip(self):
        b = ProgressBreaker(default_cap=5)
        b.step(False, cap=3)   # 1
        b.step(False, cap=3)   # 2
        b.step(True, cap=3)    # reset -> 0
        assert b.step(False, cap=3).tripped is False  # 1 again
        assert b.step(False, cap=3).tripped is False  # 2 again

    def test_cap_zero_disables_breaker(self):
        b = ProgressBreaker(default_cap=5)
        for _ in range(50):
            s = b.step(False, cap=0)
            assert s.tripped is False
        # Counter still climbs, but it can never trip.
        assert b.consecutive == 50
        assert b.tripped is False

    def test_cap_none_uses_default_cap(self):
        b = ProgressBreaker(default_cap=2)
        assert b.step(False).tripped is False  # 1
        s = b.step(False)                       # 2 == default_cap
        assert s.tripped is True

    def test_decision_table_idle_no_progress_increments(self):
        # (idle & !progress) -> +1
        b = ProgressBreaker(default_cap=5)
        assert b.step(False, cap=5).consecutive == 1

    def test_decision_table_progress_resets(self):
        # progress -> reset 0
        b = ProgressBreaker(default_cap=5)
        b.step(False, cap=5)
        assert b.step(True, cap=5).consecutive == 0

    def test_progress_step_is_frozen(self):
        s = ProgressStep(consecutive=1, tripped=False)
        with pytest.raises(Exception):
            s.consecutive = 2  # type: ignore[misc]
