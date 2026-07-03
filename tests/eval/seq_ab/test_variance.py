import pytest

from eval.seq_ab.variance import intervals_disjoint, wilson_interval


def test_wilson_zero_n_is_full_uncertainty():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_all_pass_is_high_and_bounded():
    low, high = wilson_interval(3, 3)
    assert 0.0 < low < 1.0
    assert high == pytest.approx(1.0, abs=1e-9) or high < 1.0
    assert low < 1.0  # 3/3 on n=3 is NOT certainty


def test_wilson_half_is_centered_below_point():
    low, high = wilson_interval(5, 10)
    assert low < 0.5 < high


def test_intervals_disjoint():
    assert intervals_disjoint((0.0, 0.3), (0.4, 0.9)) is True
    assert intervals_disjoint((0.4, 0.9), (0.0, 0.3)) is True
    assert intervals_disjoint((0.0, 0.5), (0.4, 0.9)) is False
