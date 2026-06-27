import pytest
from tests.live.skill_harness import declared_tools, runnable_manifests, result_text, result_is_error

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


class _C:
    def __init__(self, text): self.text = text


class _R:
    def __init__(self, content, is_error=False):
        self.content = content
        self.isError = is_error


def test_result_text_joins_content():
    r = _R([_C("hello"), _C("world")])
    assert result_text(r) == "hello\nworld"


def test_result_is_error_reads_flag():
    assert result_is_error(_R([], is_error=True)) is True
    assert result_is_error(_R([_C("ok")])) is False
