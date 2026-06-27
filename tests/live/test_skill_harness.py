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


def test_dist_stale_detects_newer_src(tmp_path):
    import os
    from tests.live.skill_harness import _dist_stale

    dist = tmp_path / "dist" / "index.js"
    dist.parent.mkdir()
    dist.write_text("compiled")
    src = tmp_path / "src"
    src.mkdir()
    ts = src / "index.ts"
    ts.write_text("source")

    os.utime(dist, (1000, 1000))
    os.utime(ts, (2000, 2000))  # src newer than dist -> stale
    assert _dist_stale(dist, src) is True

    os.utime(dist, (3000, 3000))  # dist newer than src -> fresh
    assert _dist_stale(dist, src) is False


def test_dist_stale_missing_paths_not_stale(tmp_path):
    from tests.live.skill_harness import _dist_stale
    assert _dist_stale(tmp_path / "nope.js", tmp_path / "nosrc") is False


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
