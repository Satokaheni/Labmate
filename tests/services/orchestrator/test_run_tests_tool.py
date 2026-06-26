# tests/services/orchestrator/test_run_tests_tool.py
from __future__ import annotations

import pytest

from services.orchestrator.local_tools import (
    RUN_TESTS_DEFAULT_CMD,
    RUN_TESTS_TIMEOUT_MS_DEFAULT,
    build_run_tests_command,
    shape_run_tests_result,
)


def test_build_run_tests_command_defaults_to_pytest():
    cmd, timeout_ms = build_run_tests_command({})
    assert cmd == "pytest"
    assert timeout_ms == RUN_TESTS_TIMEOUT_MS_DEFAULT


def test_build_run_tests_command_appends_path():
    cmd, _ = build_run_tests_command({"path": "tests/test_math.py"})
    assert cmd == "pytest tests/test_math.py"


def test_build_run_tests_command_appends_k_expr():
    cmd, _ = build_run_tests_command({"path": "tests/", "expr": "factorial"})
    assert cmd == "pytest tests/ -k factorial"


def test_build_run_tests_command_quotes_multiword_expr():
    cmd, _ = build_run_tests_command({"expr": "add or sub"})
    # multi-word -k expression must be quoted as a single shell argument
    assert cmd == "pytest -k 'add or sub'"


def test_build_run_tests_command_honors_env_cmd(monkeypatch):
    monkeypatch.setenv("LABMATE_TEST_CMD", "python -m pytest")
    cmd, _ = build_run_tests_command({"path": "tests/"})
    assert cmd == "python -m pytest tests/"


def test_build_run_tests_command_honors_env_timeout(monkeypatch):
    monkeypatch.setenv("LABMATE_TEST_TIMEOUT_MS", "5000")
    _, timeout_ms = build_run_tests_command({})
    assert timeout_ms == 5000


def test_build_run_tests_command_arg_timeout_overrides_env(monkeypatch):
    monkeypatch.setenv("LABMATE_TEST_TIMEOUT_MS", "5000")
    _, timeout_ms = build_run_tests_command({"timeout_ms": 9000})
    assert timeout_ms == 9000


def test_default_cmd_constant_is_pytest():
    assert RUN_TESTS_DEFAULT_CMD == "pytest"


def test_shape_run_tests_result_ok_on_zero_exit():
    out = shape_run_tests_result(0, "3 passed in 0.04s")
    assert out == {"ok": True, "exit_code": 0, "raw_output": "3 passed in 0.04s"}


def test_shape_run_tests_result_not_ok_on_nonzero_exit():
    out = shape_run_tests_result(1, "1 failed in 0.02s")
    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert "1 failed" in out["raw_output"]


def test_shape_run_tests_result_preserves_raw_failure_text():
    raw = "E   assert 1 == 2\n1 failed in 0.02s"
    out = shape_run_tests_result(1, raw)
    # RAW failure assertion text must survive verbatim (no summarization).
    assert "assert 1 == 2" in out["raw_output"]


def test_shape_run_tests_result_tail_truncates_huge_output():
    raw = "x" * 20000
    out = shape_run_tests_result(0, raw)
    assert len(out["raw_output"]) == 8000
    # tail kept, not head
    assert out["raw_output"] == raw[-8000:]
