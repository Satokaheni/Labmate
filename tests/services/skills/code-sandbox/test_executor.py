import pytest

pytestmark = pytest.mark.mocked


def test_run_python_passes_security_options(patched_executor):
    executor, client, container = patched_executor
    executor.run_python("print('hi')")

    _, kwargs = client.containers.create.call_args
    assert kwargs["network_disabled"] is True
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["cpu_quota"] == 50000
    assert kwargs["read_only"] is True
    assert kwargs["user"] == "nobody"
    assert "/tmp" in kwargs["tmpfs"]
    assert kwargs["pids_limit"] == 128


def test_run_python_times_out(patched_executor):
    executor, client, container = patched_executor
    container.wait.side_effect = Exception("ReadTimeout")
    result = executor.run_python("while True: pass", timeout=1)
    assert result.timed_out is True
    assert result.exit_code == -1
    container.kill.assert_called_once()


def test_run_python_captures_exit_code(patched_executor):
    executor, client, container = patched_executor
    container.wait.return_value = {"StatusCode": 3}
    result = executor.run_python("import sys; sys.exit(3)")
    assert result.exit_code == 3
    assert result.timed_out is False


def test_run_python_separates_streams(patched_executor):
    executor, client, container = patched_executor

    def logs(stdout=True, stderr=False):
        return b"OUT" if stdout else b"ERR"

    container.logs.side_effect = logs
    result = executor.run_python("print('OUT')")
    assert result.stdout == "OUT"
    assert result.stderr == "ERR"


def test_container_removed_on_success(patched_executor):
    executor, client, container = patched_executor
    executor.run_python("print('hi')")
    container.remove.assert_called_once_with(force=True)


def test_container_removed_on_error(patched_executor):
    executor, client, container = patched_executor
    container.start.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        executor.run_python("print('hi')")
    container.remove.assert_called_once_with(force=True)


def test_run_tests_parses_counts(patched_executor):
    executor, client, container = patched_executor
    container.logs.side_effect = lambda stdout=True, stderr=False: (
        b"... 2 passed, 1 failed, 1 error in 0.12s" if stdout else b""
    )
    result = executor.run_tests("tests/")
    assert result.passed == 2
    assert result.failed == 1
    assert result.errors == 1


def test_run_tests_rejects_unknown_framework(patched_executor):
    executor, _, _ = patched_executor
    with pytest.raises(ValueError):
        executor.run_tests("tests/", framework="unittest")


def test_run_python_with_packages_enables_network(patched_executor):
    executor, client, container = patched_executor
    executor.run_python("import requests", packages=["requests"])
    _, kwargs = client.containers.create.call_args
    assert kwargs["network_disabled"] is False


def test_run_python_no_packages_disables_network(patched_executor):
    executor, client, container = patched_executor
    executor.run_python("print('hi')")
    _, kwargs = client.containers.create.call_args
    assert kwargs["network_disabled"] is True


# LocalSubprocessExecutor mocked tests (those that mock subprocess calls, not run real ones)
# Tests that require actual subprocess execution have been moved to tests/live/test_local_executor_subprocess_live.py


@pytest.mark.mocked
def test_run_tests_forwards_k_expression(local_executor, monkeypatch):
    """Test that run_tests accepts and forwards the expr (-k) parameter to pytest.

    This test monkeypatches _run_process to capture the argv and verify that
    the -k expression is correctly threaded through to the pytest command.
    """
    # Spy on _run_process to capture the argv
    captured_cmd = []
    from executor import ExecutionResult

    def spy_run_process(cmd, timeout, cwd):
        captured_cmd.append(cmd)
        # Return a fake result with no tests found (to avoid subprocess issues)
        return ExecutionResult(
            stdout="",
            stderr="no tests ran",
            exit_code=0,
            duration_ms=0,
            timed_out=False,
            backend="local",
            sandboxed=False,
        )

    monkeypatch.setattr(local_executor, "_run_process", spy_run_process)

    # Call run_tests with expr parameter
    local_executor.run_tests("/fake/test_sample.py", expr="alpha", timeout=30)

    # Verify that -k and the expr value are in the captured command
    assert len(captured_cmd) == 1
    cmd = captured_cmd[0]
    assert "-k" in cmd, f"Expected '-k' in {cmd}"
    assert "alpha" in cmd, f"Expected 'alpha' in {cmd}"
    # Verify they're adjacent (proper pytest -k <expr> formatting)
    k_index = cmd.index("-k")
    assert k_index + 1 < len(cmd), "Expected -k to have an argument"
    assert cmd[k_index + 1] == "alpha", f"Expected 'alpha' after '-k', got {cmd[k_index + 1]}"


@pytest.mark.mocked
def test_run_tests_without_expr_omits_k(local_executor, monkeypatch):
    """Test that run_tests omits the -k flag when expr is None."""
    captured_cmd = []
    from executor import ExecutionResult

    def spy_run_process(cmd, timeout, cwd):
        captured_cmd.append(cmd)
        return ExecutionResult(
            stdout="",
            stderr="no tests ran",
            exit_code=0,
            duration_ms=0,
            timed_out=False,
            backend="local",
            sandboxed=False,
        )

    monkeypatch.setattr(local_executor, "_run_process", spy_run_process)

    # Call run_tests WITHOUT expr parameter
    local_executor.run_tests("/fake/test_sample.py", timeout=30)

    # Verify that -k is NOT in the captured command
    assert len(captured_cmd) == 1
    cmd = captured_cmd[0]
    assert "-k" not in cmd, f"Expected no '-k' in {cmd} when expr is None"


# ── cross-platform local execution (regression: preexec rlimits must be best-effort) ──


def test_local_executor_runs_python_on_this_platform(local_executor):
    """The LOCAL executor must run real code on ANY OS. Its preexec_fn applies
    POSIX rlimits that are best-effort — RLIMIT_AS raises 'current limit exceeds
    maximum limit' on macOS, and aborting the preexec used to make code-sandbox
    unusable off Linux ('Exception occurred in preexec_fn'). Containment is the
    process group + timeout, not the rlimits, so a rejected limit must degrade."""
    r = local_executor.run_python("print(2 + 2)")
    assert r.exit_code == 0, r.stderr
    assert r.stdout.strip() == "4"


def test_local_executor_mmap_import_not_broken(local_executor):
    """A wrongly-enforced RLIMIT_AS breaks mmap-heavy imports; ensure a normal
    stdlib import still runs (i.e. the address-space limit didn't wedge it)."""
    r = local_executor.run_python("import json, os, sys, ctypes; print('ok')")
    assert r.exit_code == 0, r.stderr
    assert "ok" in r.stdout


def test_local_executor_runs_shell(local_executor):
    r = local_executor.run_shell("echo hello-xplat")
    assert r.exit_code == 0, r.stderr
    assert r.stdout.strip() == "hello-xplat"


def test_local_executor_still_times_out(local_executor):
    """The timeout + process-group kill (the REAL containment) still works even
    though the rlimits are now best-effort."""
    r = local_executor.run_shell("sleep 5", timeout=1)
    assert r.timed_out is True
    assert r.exit_code == -1
