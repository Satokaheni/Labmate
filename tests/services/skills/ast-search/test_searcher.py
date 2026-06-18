import pytest

from searcher import AstSearcher, Diff, Match


@pytest.fixture
def searcher():
    return AstSearcher()


@pytest.mark.mocked
def test_find_code_matches_pattern(searcher, py_file):
    matches = searcher.find_code("requests.get($URL)", "python", str(py_file))
    # Two real calls: requests.get(u) and requests.get('https://example.com').
    assert len(matches) == 2
    assert all(isinstance(m, Match) for m in matches)
    assert all(m.file == str(py_file) for m in matches)
    assert all(m.line >= 1 for m in matches)


@pytest.mark.mocked
def test_find_code_captures_meta_var(searcher, py_file):
    matches = searcher.find_code("requests.get($URL)", "python", str(py_file))
    captured = {m.meta_vars.get("$URL") for m in matches}
    assert "u" in captured
    assert "'https://example.com'" in captured


@pytest.mark.mocked
def test_find_code_ignores_string_literals(searcher, py_file):
    matches = searcher.find_code("requests.get($URL)", "python", str(py_file))
    # The string "call requests.get(here) in a string" must NOT match.
    assert all("in a string" not in m.text for m in matches)
    assert len(matches) == 2


@pytest.mark.mocked
def test_find_code_walks_directory(searcher, py_dir):
    matches = searcher.find_code("requests.get($URL)", "python", str(py_dir))
    # a.py and b.py match; ignore.txt is not a .py file and is skipped.
    assert len(matches) == 2
    files = {m.file for m in matches}
    assert files == {str(py_dir / "a.py"), str(py_dir / "b.py")}


@pytest.mark.mocked
def test_rewrite_returns_unified_diff(searcher, py_file):
    original = py_file.read_text(encoding="utf-8")
    diff = searcher.rewrite(
        "requests.get($URL)", "session.get($URL)", "python", str(py_file)
    )
    assert isinstance(diff, Diff)
    assert diff.matches == 2
    assert "session.get" in diff.unified_diff
    assert diff.unified_diff.startswith("---") or "@@" in diff.unified_diff


@pytest.mark.mocked
def test_rewrite_does_not_write_to_disk(searcher, py_file):
    original = py_file.read_text(encoding="utf-8")
    searcher.rewrite("requests.get($URL)", "session.get($URL)", "python", str(py_file))
    # File on disk is unchanged — rewrite is preview-only.
    assert py_file.read_text(encoding="utf-8") == original


@pytest.mark.mocked
def test_find_by_rule_matches(searcher, py_file):
    rule = """
language: python
rule:
  pattern: requests.get($URL)
"""
    matches = searcher.find_by_rule(rule, str(py_file))
    assert len(matches) == 2
    assert all(isinstance(m, Match) for m in matches)


@pytest.mark.mocked
def test_find_by_rule_requires_language(searcher, py_file):
    rule = "rule:\n  pattern: requests.get($URL)\n"
    with pytest.raises(ValueError, match="language"):
        searcher.find_by_rule(rule, str(py_file))


@pytest.mark.mocked
def test_find_code_typescript(searcher, ts_file):
    matches = searcher.find_code("foo($$$ARGS)", "typescript", str(ts_file))
    # foo(1) and foo(2, 3) match; the string literal 'foo(99) ...' does not.
    assert len(matches) == 2
    assert all("inside string" not in m.text for m in matches)


@pytest.mark.mocked
def test_unsupported_language_raises(searcher, py_file):
    with pytest.raises(ValueError, match="Unsupported language"):
        searcher.find_code("x", "cobol", str(py_file))
