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


from services.orchestrator.verification_stop import build_verify_nudge


def test_build_verify_nudge_lists_files_sorted_and_is_deterministic():
    files = {"src/b.py", "src/a.py"}
    msg1 = build_verify_nudge(files)
    msg2 = build_verify_nudge(files)
    # Deterministic: same input -> identical output (prefix-cache friendly).
    assert msg1 == msg2
    # Files appear sorted, so set ordering cannot perturb the text.
    assert "src/a.py" in msg1
    assert "src/b.py" in msg1
    assert msg1.index("src/a.py") < msg1.index("src/b.py")


def test_build_verify_nudge_instructs_run_then_fix_then_finish():
    msg = build_verify_nudge({"src/app.py"})
    lowered = msg.lower()
    # Must tell the model to run tests, read failures, fix, and only then finish.
    assert "test" in lowered
    assert "fail" in lowered or "failure" in lowered
    assert "finish" in lowered


def test_build_verify_nudge_handles_empty_set():
    # Defensive: caller only invokes this when files exist, but it must not crash.
    msg = build_verify_nudge(set())
    assert isinstance(msg, str) and msg
