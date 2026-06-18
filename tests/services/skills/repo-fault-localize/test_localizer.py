import pytest
from localizer import FaultLocalizer


@pytest.mark.mocked
def test_locate_files_shape(sample_repo, patch_gemma):
    patch_gemma({"rank the files": (
        '[{"file": "config.py", "score": 0.95, '
        '"reason": "defines parse_config named in the bug"}]'
    )})
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.locate_files("parse_config crashes on None data", top_k=3)
    assert rows
    assert rows[0]["file"] == "config.py"
    assert {"file", "score", "reason"} <= set(rows[0].keys())
    assert 0.0 <= rows[0]["score"] <= 1.0


@pytest.mark.mocked
def test_locate_files_bm25_fallback(sample_repo, patch_gemma):
    patch_gemma({})  # all prompts -> "[]"
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.locate_files("parse_config key", top_k=3)
    assert rows
    assert any(r["file"] == "config.py" for r in rows)
    assert all(0.0 <= r["score"] <= 1.0 for r in rows)


@pytest.mark.mocked
def test_locate_symbols(sample_repo, patch_gemma):
    patch_gemma({"select the ones most likely":
                 '[{"symbol": "parse_config", "reason": "uses .get on possibly-None"}]'})
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.locate_symbols("parse_config crashes", "config.py")
    assert rows
    r = rows[0]
    assert r["symbol"] == "parse_config"
    assert r["file"] == "config.py"
    assert r["kind"] == "function"
    assert r["start_line"] >= 1 and r["end_line"] >= r["start_line"]
    assert {"file", "symbol", "kind", "start_line", "end_line", "reason"} <= set(r.keys())


@pytest.mark.mocked
def test_locate_symbols_fallback(sample_repo, patch_gemma):
    patch_gemma({})  # -> "[]"
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.locate_symbols("anything", "config.py")
    names = {r["symbol"] for r in rows}
    assert {"parse_config", "unrelated_helper"} <= names


@pytest.mark.mocked
def test_suggest_edit_sites_clamped(sample_repo, patch_gemma):
    # LLM returns a wildly out-of-range end_line; must be clamped to file bounds.
    patch_gemma({"identify the specific line ranges":
                 '[{"file": "config.py", "start_line": 3, "end_line": 9999, '
                 '"reason": "add a None check before .get"}]'})
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.suggest_edit_sites("None crash", "config.py", ["parse_config"])
    assert rows
    r = rows[0]
    assert {"file", "start_line", "end_line", "reason"} <= set(r.keys())
    assert r["start_line"] >= 1
    assert r["end_line"] <= 3  # parse_config spans lines 1-3 in the fixture
    assert r["end_line"] >= r["start_line"]


@pytest.mark.mocked
def test_broken_file_tolerated(sample_repo, patch_gemma):
    (sample_repo / "broken.py").write_text("def oops(:\n  pass\n")
    patch_gemma({})
    loc = FaultLocalizer(str(sample_repo))
    # Should not raise; symbols may be empty or partial.
    loc.locate_symbols("x", "broken.py")


@pytest.mark.mocked
def test_no_stdout_pollution(sample_repo, patch_gemma, capsys):
    patch_gemma({"rank the files":
                 '[{"file": "config.py", "score": 0.9, "reason": "x"}]',
                 "select the ones most likely":
                 '[{"symbol": "parse_config", "reason": "x"}]',
                 "identify the specific line ranges":
                 '[{"file": "config.py", "start_line": 1, "end_line": 3, "reason": "x"}]'})
    loc = FaultLocalizer(str(sample_repo))
    files = loc.locate_files("parse_config None crash", top_k=2)
    syms = loc.locate_symbols("parse_config None crash", files[0]["file"])
    loc.suggest_edit_sites("parse_config None crash", files[0]["file"],
                           [s["symbol"] for s in syms])
    captured = capsys.readouterr()
    assert captured.out == ""
