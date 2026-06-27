import pytest
from tests.live.skill_harness import declared_tools, runnable_manifests

pytestmark = pytest.mark.live


def test_declared_tools_parses_code_sandbox():
    tools = declared_tools("code-sandbox")
    assert {"run_python", "run_shell", "run_tests", "install_packages"} <= tools


def test_declared_tools_unknown_skill_is_empty():
    assert declared_tools("does-not-exist") == set()


def test_runnable_manifests_includes_code_sandbox():
    names = {m.name for m in runnable_manifests()}
    assert "code-sandbox" in names
    # instruction-only skills are excluded
    assert "academic-writing" not in names
