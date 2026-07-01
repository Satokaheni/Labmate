"""Unit tests for summarize_skips — the pure aggregation core of pregate_recall_eval."""

import pytest

from eval.pregate_recall_eval import summarize_skips

# ---------------------------------------------------------------------------
# Basic bucket placement
# ---------------------------------------------------------------------------


def test_false_skip_counted_at_correct_threshold():
    """A skill row with max_sim below threshold is a false-skip (recall regression)."""
    rows = [
        {"expected": "code-review", "max_sim": 0.20},
    ]
    results = summarize_skips(rows, thresholds=[0.25])
    assert len(results) == 1
    r = results[0]
    assert r["threshold"] == 0.25
    # 1 false-skip out of 1 skill row
    assert r["false_skip_rate"] == pytest.approx(1.0)
    # 0 none-rows → correct_skip_rate should be 0.0 (no ZeroDivisionError)
    assert r["correct_skip_rate"] == pytest.approx(0.0)
    assert r["n_skill"] == 1
    assert r["n_none"] == 0


def test_correct_skip_counted_at_correct_threshold():
    """A none row with max_sim below threshold is a correct-skip (latency win)."""
    rows = [
        {"expected": "none", "max_sim": 0.10},
    ]
    results = summarize_skips(rows, thresholds=[0.25])
    r = results[0]
    assert r["correct_skip_rate"] == pytest.approx(1.0)
    assert r["false_skip_rate"] == pytest.approx(0.0)
    assert r["n_skill"] == 0
    assert r["n_none"] == 1


def test_above_threshold_not_counted_as_skip():
    """A skill row with max_sim above threshold is NOT a false-skip."""
    rows = [
        {"expected": "code-review", "max_sim": 0.40},
    ]
    results = summarize_skips(rows, thresholds=[0.25])
    r = results[0]
    assert r["false_skip_rate"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Mixed rows — multiple thresholds
# ---------------------------------------------------------------------------


def test_mixed_rows_two_thresholds():
    """Both false-skip and correct-skip counted correctly across two thresholds."""
    rows = [
        {"expected": "ast-search", "max_sim": 0.22},  # false-skip at 0.25, not at 0.20
        {"expected": "none", "max_sim": 0.15},  # correct-skip at both thresholds
    ]
    results = summarize_skips(rows, thresholds=[0.20, 0.25])
    by_threshold = {r["threshold"]: r for r in results}

    # At 0.20: skill row has max_sim=0.22 ≥ 0.20 → NOT a false-skip
    r20 = by_threshold[0.20]
    assert r20["false_skip_rate"] == pytest.approx(0.0)
    assert r20["correct_skip_rate"] == pytest.approx(1.0)

    # At 0.25: skill row has max_sim=0.22 < 0.25 → IS a false-skip
    r25 = by_threshold[0.25]
    assert r25["false_skip_rate"] == pytest.approx(1.0)
    assert r25["correct_skip_rate"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Per-skill false-skip breakdown
# ---------------------------------------------------------------------------


def test_per_skill_false_skip_groups_by_expected():
    """per_skill_false_skip groups false-skips by expected skill name."""
    rows = [
        {"expected": "ast-search", "max_sim": 0.10},  # false-skip
        {"expected": "ast-search", "max_sim": 0.10},  # false-skip
        {"expected": "code-review", "max_sim": 0.10},  # false-skip
        {"expected": "code-review", "max_sim": 0.50},  # NOT a false-skip (above 0.25)
    ]
    results = summarize_skips(rows, thresholds=[0.25])
    r = results[0]
    psk = r["per_skill_false_skip"]
    # ast-search: 2/2 = 1.0
    assert psk["ast-search"] == pytest.approx(1.0)
    # code-review: 1/2 = 0.5
    assert psk["code-review"] == pytest.approx(0.5)


def test_per_skill_false_skip_excludes_none_rows():
    """none rows do NOT appear in per_skill_false_skip."""
    rows = [
        {"expected": "none", "max_sim": 0.10},
    ]
    results = summarize_skips(rows, thresholds=[0.25])
    r = results[0]
    assert "none" not in r["per_skill_false_skip"]


# ---------------------------------------------------------------------------
# Empty / edge cases — no ZeroDivisionError
# ---------------------------------------------------------------------------


def test_empty_input_is_safe():
    """Empty rows list must not raise and returns 0.0 rates."""
    results = summarize_skips([], thresholds=[0.25, 0.30])
    assert len(results) == 2
    for r in results:
        assert r["false_skip_rate"] == pytest.approx(0.0)
        assert r["correct_skip_rate"] == pytest.approx(0.0)
        assert r["n_skill"] == 0
        assert r["n_none"] == 0


def test_empty_thresholds_returns_empty_list():
    """Empty thresholds list returns an empty list."""
    rows = [{"expected": "code-review", "max_sim": 0.10}]
    results = summarize_skips(rows, thresholds=[])
    assert results == []


def test_all_skill_rows_no_none():
    """All skill rows, no none rows — correct_skip_rate stays 0.0 (no ZeroDivisionError)."""
    rows = [
        {"expected": "ast-search", "max_sim": 0.10},
        {"expected": "code-review", "max_sim": 0.40},
    ]
    results = summarize_skips(rows, thresholds=[0.25])
    r = results[0]
    assert r["correct_skip_rate"] == pytest.approx(0.0)
    assert r["n_skill"] == 2
    assert r["n_none"] == 0


def test_all_none_rows_no_skill():
    """All none rows, no skill rows — false_skip_rate stays 0.0 (no ZeroDivisionError)."""
    rows = [
        {"expected": "none", "max_sim": 0.10},
        {"expected": "none", "max_sim": 0.40},
    ]
    results = summarize_skips(rows, thresholds=[0.25])
    r = results[0]
    assert r["false_skip_rate"] == pytest.approx(0.0)
    assert r["n_skill"] == 0
    assert r["n_none"] == 2
