"""Unit tests for the pure revise-before-deliver helpers.

should_revise is the single side-effect / cap / empty / error guard; it makes
NO model call. build_revision_prompt formats the single revision prompt.
"""
import pytest

from services.orchestrator.finalize_revision import should_revise, build_revision_prompt


# ── should_revise: the happy path ────────────────────────────────────────────
def test_should_revise_true_when_clean_answer_under_cap():
    assert should_revise(
        "2, 3, 5",
        had_side_effects=False,
        attempts=0,
        max_attempts=1,
        errored=False,
    ) is True


# ── side-effect guard ────────────────────────────────────────────────────────
def test_should_revise_false_after_side_effects():
    assert should_revise(
        "Created report.txt.",
        had_side_effects=True,
        attempts=0,
        max_attempts=1,
        errored=False,
    ) is False


# ── cap guard (idempotency: a second pass on the same answer is blocked) ──────
def test_should_revise_false_at_cap():
    assert should_revise(
        "2, 3, 5",
        had_side_effects=False,
        attempts=1,
        max_attempts=1,
        errored=False,
    ) is False


def test_should_revise_false_over_cap():
    assert should_revise(
        "anything",
        had_side_effects=False,
        attempts=5,
        max_attempts=1,
        errored=False,
    ) is False


def test_should_revise_false_when_cap_is_zero():
    assert should_revise(
        "2, 3, 5",
        had_side_effects=False,
        attempts=0,
        max_attempts=0,
        errored=False,
    ) is False


# ── no visible text guard ────────────────────────────────────────────────────
@pytest.mark.parametrize("blank", ["", "   ", "\n\t  \n"])
def test_should_revise_false_when_no_visible_answer(blank):
    assert should_revise(
        blank,
        had_side_effects=False,
        attempts=0,
        max_attempts=1,
        errored=False,
    ) is False


# ── errored / aborted run guard ──────────────────────────────────────────────
def test_should_revise_false_when_errored():
    assert should_revise(
        "Failed subtasks: fetch (error: connection refused)",
        had_side_effects=False,
        attempts=0,
        max_attempts=1,
        errored=True,
    ) is False


# ── idempotency: same answer twice does not re-revise (attempts already spent) ─
def test_should_revise_idempotent_after_one_pass():
    # First pass allowed.
    assert should_revise("x", had_side_effects=False, attempts=0, max_attempts=1, errored=False) is True
    # After it ran once, attempts==1 -> blocked even with identical inputs otherwise.
    assert should_revise("x", had_side_effects=False, attempts=1, max_attempts=1, errored=False) is False


# ── build_revision_prompt ────────────────────────────────────────────────────
def test_build_revision_prompt_includes_task_and_answer():
    p = build_revision_prompt("List primes under 10", "2, 3, 5")
    assert "List primes under 10" in p
    assert "2, 3, 5" in p


def test_build_revision_prompt_is_deterministic():
    a = build_revision_prompt("t", "ans")
    b = build_revision_prompt("t", "ans")
    assert a == b


def test_build_revision_prompt_keeps_answer_when_already_correct():
    # The instruction must permit returning the answer UNCHANGED (no forced edit).
    p = build_revision_prompt("t", "ans").lower()
    assert "unchanged" in p or "as is" in p or "return it unchanged" in p
