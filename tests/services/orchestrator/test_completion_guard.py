"""Pure unit tests for the completion_guard heuristics.

These functions are deterministic string heuristics (no model, no I/O), so
unlike model-output tests we assert exact booleans/returns on fixed inputs.
"""
from __future__ import annotations

import pytest

from services.orchestrator.completion_guard import (
    is_punt_answer,
    asserts_success,
    reconcile_ok,
)


# ── is_punt_answer ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "I couldn't analyze the file because it is too large. Please provide a smaller snippet.",
        "The file is too long for me to process; break it into smaller parts.",
        "I was unable to complete the task.",
        "I could not find the bug.",
        "Cannot process this input.",
        "Please break the file into smaller pieces and try again.",
        "Provide a snippet and I will help.",
        "Please provide a smaller portion of the code.",
        "  COULDN'T read the file — IT IS TOO LARGE.  ",  # case/space insensitive
    ],
)
def test_is_punt_answer_true_for_terminal_punts(text):
    assert is_punt_answer(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "2 + 2 = 4",
        "I fixed the off-by-one bug and the tests now pass.",
        "Here is the refactored factorial function.",
        "The function returns the square of a number.",
        "",
        "Done.",
        "I reviewed the file and found three potential issues.",  # honest non-punt
    ],
)
def test_is_punt_answer_false_for_real_answers(text):
    assert is_punt_answer(text) is False


# ── asserts_success ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "I fixed the bug.",
        "The bug is fixed.",
        "All tests pass.",
        "The tests now pass.",
        "Tests are now passing.",
        "I resolved the bug in factorial.",
        "I have fixed the off-by-one error and all tests pass.",
        "  THE BUG IS FIXED.  ",  # case/space insensitive
    ],
)
def test_asserts_success_true_for_success_claims(text):
    assert asserts_success(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "2 + 2 = 4",
        "I reviewed the file for bugs and found a potential null deref.",
        "Here are the unit tests I generated.",
        "I could not fix the bug.",
        "The tests are failing.",
        "",
    ],
)
def test_asserts_success_false_for_neutral_or_negative(text):
    assert asserts_success(text) is False


# ── reconcile_ok ──────────────────────────────────────────────────────────────

def test_reconcile_punt_downgrades_ok_true_to_false():
    ok, note = reconcile_ok(
        True,
        "I couldn't analyze the file because it is too large. Provide a smaller snippet.",
        tests_passed=False,
    )
    assert ok is False
    assert note  # non-empty honesty note
    assert "punt" in note.lower() or "could not" in note.lower()


def test_reconcile_unverified_success_claim_downgrades_to_false():
    ok, note = reconcile_ok(
        True,
        "I fixed the off-by-one bug and all tests pass.",
        tests_passed=False,
    )
    assert ok is False
    assert note
    assert "verif" in note.lower() or "not verified" in note.lower()


def test_reconcile_verified_success_claim_stays_ok_true():
    ok, note = reconcile_ok(
        True,
        "I fixed the off-by-one bug and all tests pass.",
        tests_passed=True,
    )
    assert ok is True
    assert note == ""


def test_reconcile_honest_non_claim_success_unchanged():
    ok, note = reconcile_ok(
        True,
        "Here is the square function you asked for.",
        tests_passed=False,
    )
    assert ok is True
    assert note == ""


def test_reconcile_punt_takes_precedence_over_success_claim():
    # An answer that both claims success AND punts is a punt → ok=False.
    ok, note = reconcile_ok(
        True,
        "I fixed it but the file is too large to verify, provide a smaller snippet.",
        tests_passed=False,
    )
    assert ok is False
    assert note


def test_reconcile_already_false_left_false():
    ok, note = reconcile_ok(
        False,
        "Here is the square function.",
        tests_passed=False,
    )
    assert ok is False
    assert note == ""


def test_reconcile_punt_when_ok_already_false_stays_false_no_double_note():
    ok, note = reconcile_ok(
        False,
        "I could not process the file, it is too large.",
        tests_passed=False,
    )
    assert ok is False
    # ok was already False; no downgrade was needed, so no note to append.
    assert note == ""
