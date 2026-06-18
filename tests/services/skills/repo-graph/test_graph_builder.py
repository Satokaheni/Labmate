import pytest
from graph_builder import RepoGraphBuilder


@pytest.mark.mocked
def test_build_extracts_cross_file_call(sample_repo):
    edges = RepoGraphBuilder(str(sample_repo)).build()
    calls = [e for e in edges if e.kind == "call" and e.dst_symbol == "helper"]
    assert any(e.src_symbol == "run" and e.dst_file == "helpers.py" for e in calls)


@pytest.mark.mocked
def test_definitions_collected(sample_repo):
    b = RepoGraphBuilder(str(sample_repo))
    b.build()
    names = {d["symbol"] for d in b.definitions()}
    assert {"helper", "run"} <= names


@pytest.mark.mocked
def test_broken_file_tolerated(sample_repo):
    (sample_repo / "broken.py").write_text("def oops(:\n  pass\n")
    edges = RepoGraphBuilder(str(sample_repo)).build()
    assert any(e.dst_symbol == "helper" for e in edges)
