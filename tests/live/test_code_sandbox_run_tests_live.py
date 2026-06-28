import os
import sys
import pytest

pytestmark = pytest.mark.live

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "services", "skills", "code-sandbox"))
from executor import LocalSubprocessExecutor  # noqa: E402


def test_real_pytest_passes(tmp_path):
    t = tmp_path / "test_pass.py"
    t.write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    res = LocalSubprocessExecutor().run_tests(str(t))
    assert res.passed == 1
    assert res.failed == 0
    assert res.errors == 0
    assert not res.timed_out


def test_real_pytest_reports_failure(tmp_path):
    t = tmp_path / "test_fail.py"
    t.write_text("def test_bad():\n    assert 1 == 2\n")
    res = LocalSubprocessExecutor().run_tests(str(t))
    assert res.failed == 1
    assert res.passed == 0


def test_real_pytest_honors_k_expr(tmp_path):
    t = tmp_path / "test_two.py"
    t.write_text("def test_alpha():\n    assert True\n\ndef test_beta():\n    assert True\n")
    res = LocalSubprocessExecutor().run_tests(str(t), expr="alpha")
    assert res.passed == 1
