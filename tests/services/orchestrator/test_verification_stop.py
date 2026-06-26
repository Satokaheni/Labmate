# tests/services/orchestrator/test_verification_stop.py
from __future__ import annotations

from services.orchestrator.verification_stop import needs_verification


def test_no_edits_never_needs_verification():
    # Edited nothing -> guard must not fire, regardless of tests_passed.
    assert needs_verification(set(), tests_passed=False, nudges_used=0, max_nudges=2) is False
    assert needs_verification(set(), tests_passed=True, nudges_used=0, max_nudges=2) is False


def test_edited_without_passing_tests_under_cap_needs_verification():
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=0, max_nudges=2) is True
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=1, max_nudges=2) is True


def test_edited_with_passing_tests_does_not_need_verification():
    assert needs_verification({"src/app.py"}, tests_passed=True, nudges_used=0, max_nudges=2) is False


def test_cap_reached_does_not_need_verification():
    # nudges_used == max_nudges -> stop nudging, accept finish honestly.
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=2, max_nudges=2) is False
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=3, max_nudges=2) is False


def test_zero_cap_never_nudges():
    assert needs_verification({"src/app.py"}, tests_passed=False, nudges_used=0, max_nudges=0) is False
