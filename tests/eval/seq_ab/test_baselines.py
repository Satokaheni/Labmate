from eval.seq_ab.baselines import noop_floor


def test_noop_cannot_complete_any_case():
    # A do-nothing agent that finishes with no work passes nothing.
    assert noop_floor("compound") == 0.0
    assert noop_floor("control_single") == 0.0
    assert noop_floor("control_trivial") == 0.0


def test_noop_unknown_kind_defaults_zero():
    assert noop_floor("something_new") == 0.0
