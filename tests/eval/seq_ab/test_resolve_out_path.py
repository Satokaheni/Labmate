from eval.seq_ab.run_seq_ab import resolve_out_path


def test_default_path():
    assert resolve_out_path("skill_first", [], {}) == "eval/seq_ab/results-skill_first.json"


def test_ab_only_distinct_path():
    assert resolve_out_path("react", ["c2"], {}) == "eval/seq_ab/results-react-only-c2.json"


def test_seq_ab_out_override_wins_over_default():
    # A flag-A/B directs each arm here so it NEVER overwrites the committed baseline.
    out = resolve_out_path(
        "skill_first", [], {"SEQ_AB_OUT": "eval/seq_ab/results-flagab-default.json"}
    )
    assert out == "eval/seq_ab/results-flagab-default.json"


def test_seq_ab_out_override_wins_even_with_ab_only():
    assert resolve_out_path("skill_first", ["c2"], {"SEQ_AB_OUT": "x.json"}) == "x.json"
