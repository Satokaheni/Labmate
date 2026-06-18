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
