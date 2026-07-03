import pytest

from eval.routing_metrics import (
    inflation_estimate,
    majority_class_accuracy,
    random_baseline_accuracy,
)


def test_majority_class_is_modal_share():
    cases = [
        {"expected": "code-review"},
        {"expected": "code-review"},
        {"expected": "code-review"},
        {"expected": "test-gen"},
    ]
    # modal label 'code-review' covers 3/4
    assert majority_class_accuracy(cases) == pytest.approx(0.75)


def test_majority_class_empty_is_zero():
    assert majority_class_accuracy([]) == 0.0


def test_random_baseline_is_one_over_actions():
    # 10 skills + 1 decline option -> 1/11 per case
    cases = [{"expected": "a"}, {"expected": "none"}]
    assert random_baseline_accuracy(cases, n_skills=10) == pytest.approx(1 / 11)


def test_inflation_is_generated_minus_heldout():
    assert inflation_estimate(0.90, 0.72) == pytest.approx(0.18)


def test_inflation_can_be_negative():
    assert inflation_estimate(0.70, 0.75) == pytest.approx(-0.05)
