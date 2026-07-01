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


# ---------------------------------------------------------------------------
# _recommend_threshold tests (new)
# ---------------------------------------------------------------------------


def test_recommend_threshold_normal_case():
    """Normal case: the highest threshold with acceptable false-skip is recommended."""
    from eval.pregate_recall_eval import _recommend_threshold

    rows = [
        {
            "threshold": 0.20,
            "false_skip_rate": 0.02,
            "correct_skip_rate": 0.50,
            "per_skill_false_skip": {"code-review": 0.02},
            "n_skill": 50,
            "n_none": 10,
        },
        {
            "threshold": 0.25,
            "false_skip_rate": 0.04,
            "correct_skip_rate": 0.70,
            "per_skill_false_skip": {"code-review": 0.04},
            "n_skill": 50,
            "n_none": 10,
        },
        {
            "threshold": 0.30,
            "false_skip_rate": 0.08,
            "correct_skip_rate": 0.80,
            "per_skill_false_skip": {"code-review": 0.08},
            "n_skill": 50,
            "n_none": 10,
        },
    ]
    rec = _recommend_threshold(rows, max_false_skip=0.05)
    # Both 0.20 and 0.25 qualify (false_skip_rate <= 0.05 and correct_skip_rate > 0)
    # The highest is 0.25
    assert rec == 0.25


def test_recommend_threshold_rejects_no_op_threshold():
    """A threshold with correct_skip_rate == 0 (no latency win) must NOT be recommended."""
    from eval.pregate_recall_eval import _recommend_threshold

    rows = [
        {
            "threshold": 0.20,
            "false_skip_rate": 0.0,
            "correct_skip_rate": 0.0,  # No latency benefit (no none rows skipped)
            "per_skill_false_skip": {},
            "n_skill": 10,
            "n_none": 0,
        },
    ]
    rec = _recommend_threshold(rows, max_false_skip=0.05)
    # No-op threshold with 0 correct_skip should not be recommended
    assert rec is None


def test_recommend_threshold_all_regressions():
    """Sweep where all useful thresholds regress recall — no recommendation."""
    from eval.pregate_recall_eval import _recommend_threshold

    rows = [
        {
            "threshold": 0.20,
            "false_skip_rate": 0.02,
            "correct_skip_rate": 0.5,
            "per_skill_false_skip": {"code-review": 0.02},
            "n_skill": 50,
            "n_none": 10,
        },
        {
            "threshold": 0.25,
            "false_skip_rate": 0.08,  # Exceeds 0.05 gate
            "correct_skip_rate": 0.7,
            "per_skill_false_skip": {"code-review": 0.08},
            "n_skill": 50,
            "n_none": 10,
        },
        {
            "threshold": 0.30,
            "false_skip_rate": 0.12,  # Exceeds 0.05 gate
            "correct_skip_rate": 0.8,
            "per_skill_false_skip": {"code-review": 0.12},
            "n_skill": 50,
            "n_none": 10,
        },
    ]
    rec = _recommend_threshold(rows, max_false_skip=0.05)
    # Only 0.20 passes the gate, and it has correct_skip_rate > 0, so it's recommended
    assert rec == 0.20


def test_recommend_threshold_mixed_with_no_op():
    """Mix of useful and no-op thresholds; only useful ones are considered."""
    from eval.pregate_recall_eval import _recommend_threshold

    rows = [
        {
            "threshold": 0.15,
            "false_skip_rate": 0.0,
            "correct_skip_rate": 0.0,  # No-op: skips nothing
            "per_skill_false_skip": {},
            "n_skill": 50,
            "n_none": 10,
        },
        {
            "threshold": 0.25,
            "false_skip_rate": 0.03,
            "correct_skip_rate": 0.60,  # Useful: delivers latency win
            "per_skill_false_skip": {"code-review": 0.03},
            "n_skill": 50,
            "n_none": 10,
        },
    ]
    rec = _recommend_threshold(rows, max_false_skip=0.05)
    # 0.15 is rejected (no latency win), 0.25 is recommended
    assert rec == 0.25
