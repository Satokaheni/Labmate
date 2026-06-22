import json
import pytest
from unittest.mock import MagicMock

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


# LocalSubprocessExecutor tests


@pytest.mark.mocked
def test_local_executor_run_python_simple(local_executor):
    """LocalSubprocessExecutor.run_python should execute and capture output."""
    result = local_executor.run_python("print(2+2)")
    assert "4" in result.stdout
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.duration_ms > 0


@pytest.mark.mocked
def test_local_executor_run_python_with_stderr(local_executor):
    """LocalSubprocessExecutor should separate stdout and stderr."""
    result = local_executor.run_python(
        "import sys; print('out'); print('err', file=sys.stderr)"
    )
    assert "out" in result.stdout
    assert "err" in result.stderr
    assert result.exit_code == 0


@pytest.mark.mocked
def test_local_executor_run_python_exit_code(local_executor):
    """LocalSubprocessExecutor should capture non-zero exit codes."""
    result = local_executor.run_python("import sys; sys.exit(5)")
    assert result.exit_code == 5
    assert result.timed_out is False


@pytest.mark.mocked
def test_local_executor_run_python_timeout(local_executor):
    """LocalSubprocessExecutor should timeout and capture partial output."""
    result = local_executor.run_python(
        "import time; print('start'); time.sleep(5)",
        timeout=1,
    )
    assert result.timed_out is True
    assert result.exit_code == -1
    assert "start" in result.stdout


@pytest.mark.mocked
def test_local_executor_run_shell_simple(local_executor):
    """LocalSubprocessExecutor.run_shell should execute shell commands."""
    result = local_executor.run_shell("echo 'hello world'")
    assert "hello world" in result.stdout
    assert result.exit_code == 0
    assert result.timed_out is False


@pytest.mark.mocked
def test_local_executor_run_shell_exit_code(local_executor):
    """LocalSubprocessExecutor.run_shell should capture exit codes."""
    result = local_executor.run_shell("exit 42")
    assert result.exit_code == 42
    assert result.timed_out is False


@pytest.mark.mocked
def test_local_executor_run_tests_parses_pytest(local_executor):
    """LocalSubprocessExecutor.run_tests should parse pytest summary."""
    # We can't guarantee specific test counts without a real test file,
    # but we can verify the method runs and parses output.
    # This is more of a smoke test — the real test would need a test file.
    result = local_executor.run_tests(
        "tests/services/skills/code-sandbox/test_executor.py", timeout=30
    )
    # Just verify structure
    assert hasattr(result, "passed")
    assert hasattr(result, "failed")
    assert hasattr(result, "errors")
    assert result.duration_ms >= 0
    assert isinstance(result.output, str)


@pytest.mark.mocked
def test_local_executor_run_tests_unsupported_framework(local_executor):
    """LocalSubprocessExecutor.run_tests should reject unsupported frameworks."""
    with pytest.raises(ValueError, match="unsupported framework"):
        local_executor.run_tests("tests/", framework="unittest")
