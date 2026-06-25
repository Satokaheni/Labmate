import pytest

pytestmark = pytest.mark.mocked


def test_run_parses_counts_and_score(fake_runner):
    from mutation_runner import MutationRunner

    runner = MutationRunner(runner=fake_runner())
    result = runner.run("src/calc.py", "tests/test_calc.py")

    assert result.killed_count == 18
    assert result.total_count == 22          # 18 killed + 4 survived
    assert result.mutation_score == pytest.approx(18 / 22, abs=1e-3)


def test_run_collects_surviving_diffs(fake_runner):
    from mutation_runner import MutationRunner

    runner = MutationRunner(runner=fake_runner())
    result = runner.run("src/calc.py", "tests/test_calc.py")

    assert len(result.surviving_mutants) >= 1
    assert any("return a - b" in d for d in result.surviving_mutants)


def test_exit_code_1_is_not_an_error(fake_runner):
    from mutation_runner import MutationRunner

    runner = MutationRunner(runner=fake_runner(run_code=1))
    result = runner.run("src/calc.py", "tests/test_calc.py")  # must not raise

    assert result.total_count > 0


def test_run_pytest_passing():
    """run_tests re-runs an existing suite as-is and reports a pass."""
    from mutation_runner import run_pytest, CommandResult

    def runner(argv, cwd, timeout):
        assert "pytest" in argv  # plain pytest, no generation
        return CommandResult(0, "3 passed in 0.04s\n", "")

    res = run_pytest("tests/test_calc.py", runner=runner)
    assert res["passed"] is True
    assert res["passed_count"] == 3
    assert res["failed_count"] == 0
    assert "3 passed" in res["summary"]


def test_run_pytest_failing_is_not_passed():
    from mutation_runner import run_pytest, CommandResult

    def runner(argv, cwd, timeout):
        return CommandResult(1, "1 failed, 2 passed in 0.05s\n", "")

    res = run_pytest("tests/test_calc.py", runner=runner)
    assert res["passed"] is False
    assert res["passed_count"] == 2
    assert res["failed_count"] == 1


def test_run_pytest_counts_errors_as_failures():
    from mutation_runner import run_pytest, CommandResult

    def runner(argv, cwd, timeout):
        return CommandResult(2, "1 error in 0.03s\n", "")

    res = run_pytest("tests/test_calc.py", runner=runner)
    assert res["passed"] is False
    assert res["failed_count"] == 1


def test_zero_total_guards_division(fake_runner):
    from mutation_runner import MutationRunner

    runner = MutationRunner(runner=fake_runner(run_out="🎉 0  🙁 0\n", results_out=""))
    result = runner.run("src/calc.py", "tests/test_calc.py")

    assert result.total_count == 0
    assert result.mutation_score == 0.0
