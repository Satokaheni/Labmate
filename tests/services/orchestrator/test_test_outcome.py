from services.orchestrator.test_outcome import classify_test_attempt, TestOutcome


def test_passing_run():
    o = classify_test_attempt('{"ok": true, "exit_code": 0, "raw_output": "3 passed"}')
    assert o.ran and o.passed and not o.infra_error


def test_failing_run():
    o = classify_test_attempt('{"ok": false, "exit_code": 1, "raw_output": "1 failed, 2 passed"}')
    assert o.ran and not o.passed and not o.infra_error


def test_infra_error_explicit_error_key():
    o = classify_test_attempt('{"error": "no test runner available"}')
    assert o.infra_error and not o.ran and not o.passed
    assert "no test runner" in o.reason


def test_infra_error_skill_unavailable_in_raw_output():
    o = classify_test_attempt('{"ok": false, "exit_code": 1, "raw_output": "skill_unavailable: no tool"}')
    assert o.infra_error and not o.ran


def test_infra_error_timeout():
    o = classify_test_attempt('{"ok": false, "exit_code": 1, "raw_output": "timeout"}')
    assert o.infra_error


def test_infra_error_exec_run_pytest_blocked():
    o = classify_test_attempt(
        '{"ok": false, "exit_code": 1, "raw_output": '
        '"exec_run: this command looks like code execution and is not allowed"}')
    assert o.infra_error


def test_no_tests_collected_is_infra_not_a_real_fail():
    o = classify_test_attempt('{"ok": false, "exit_code": 1, "raw_output": "no tests ran in 0.01s"}')
    assert o.infra_error


def test_garbage_is_infra_error():
    o = classify_test_attempt("not json at all")
    assert o.infra_error
