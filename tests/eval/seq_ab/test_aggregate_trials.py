"""Unit tests for the pure aggregation helpers in eval/seq_ab/run_seq_ab.py.

These must run WITHOUT Redis or the orchestrator — the helpers are pure.
Importing run_seq_ab must not open a Redis connection (connection lives in main()).
"""

from eval.seq_ab.run_seq_ab import aggregate_trials, median, summarize_line


def _trial(ok, llm_calls, wall_s):
    # Mirrors the shape run_case() returns (only the fields aggregation reads).
    return {
        "id": "cX",
        "kind": "compound",
        "task": "t",
        "ok": ok,
        "skill_sequence": ["s"],
        "llm_calls": llm_calls,
        "wall_s": wall_s,
        "final_answer": "",
        "task_id": "tid",
    }


# ---- median ----
def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_median_even_returns_mean_of_middle_two():
    assert median([1, 2, 3, 4]) == 2.5


def test_median_ignores_none():
    assert median([None, 4, 2, None]) == 3


def test_median_all_none_is_none():
    assert median([None, None]) is None


def test_median_empty_is_none():
    assert median([]) is None


# ---- aggregate_trials ----
def test_aggregate_all_pass():
    agg = aggregate_trials([_trial(True, 10, 5.0), _trial(True, 20, 7.0)])
    assert agg["pass_count"] == 2
    assert agg["trials_run"] == 2
    assert agg["pass_rate"] == 1.0
    assert agg["median_llm_calls"] == 15
    assert agg["median_wall_s"] == 6.0


def test_aggregate_all_fail():
    agg = aggregate_trials([_trial(False, 10, 5.0), _trial(False, 12, 6.0)])
    assert agg["pass_count"] == 0
    assert agg["pass_rate"] == 0.0
    assert agg["trials_run"] == 2


def test_aggregate_mixed_rounds_to_two_dp():
    agg = aggregate_trials([_trial(True, 1, 1.0), _trial(False, 2, 2.0), _trial(True, 3, 3.0)])
    assert agg["pass_count"] == 2
    assert agg["pass_rate"] == 0.67  # 2/3 -> 0.67


def test_aggregate_empty_no_div_by_zero():
    agg = aggregate_trials([])
    assert agg["pass_count"] == 0
    assert agg["trials_run"] == 0
    assert agg["pass_rate"] == 0.0
    assert agg["median_llm_calls"] is None
    assert agg["median_wall_s"] is None
    assert agg["trials"] == []


def test_aggregate_only_true_counts_as_pass():
    # ok can be None (orchestrator never answered) — must NOT count as a pass.
    agg = aggregate_trials([_trial(None, None, 30.0), _trial(True, 5, 5.0)])
    assert agg["pass_count"] == 1
    assert agg["pass_rate"] == 0.5


def test_aggregate_median_ignores_none_llm_calls():
    agg = aggregate_trials([_trial(None, None, 10.0), _trial(True, 8, 4.0), _trial(True, 12, 6.0)])
    assert agg["median_llm_calls"] == 10  # median of [8, 12]
    assert agg["median_wall_s"] == 6.0  # median of [10.0, 4.0, 6.0]


def test_aggregate_preserves_trials_list_identity_of_contents():
    trials = [_trial(True, 1, 1.0)]
    agg = aggregate_trials(trials)
    assert agg["trials"] == trials


# ---- summarize_line ----
def test_summarize_line_compact():
    agg = aggregate_trials([_trial(True, 10, 5.0), _trial(False, 20, 7.0), _trial(True, 30, 9.0)])
    line = summarize_line("skill_first", "c1_testgen_review_fix", agg)
    assert "skill_first" in line
    assert "c1_testgen_review_fix" in line
    assert "2/3" in line
    assert "0.67" in line


# ---- Wilson CI tests ----
def test_aggregate_includes_wilson_ci():
    agg = aggregate_trials([{"ok": True}, {"ok": True}, {"ok": False}])
    assert agg["pass_count"] == 2 and agg["trials_run"] == 3
    assert 0.0 <= agg["pass_rate_ci_low"] < agg["pass_rate"] < agg["pass_rate_ci_high"] <= 1.0


def test_aggregate_ci_empty_is_full_range():
    agg = aggregate_trials([])
    assert agg["pass_rate_ci_low"] == 0.0
    assert agg["pass_rate_ci_high"] == 1.0
