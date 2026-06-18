import pytest
from graph_builder import RepoGraphBuilder
from graph_store import GraphStore


@pytest.mark.mocked
def test_get_callers(sample_repo, tmp_path):
    b = RepoGraphBuilder(str(sample_repo))
    edges = b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    store.upsert_edges(edges)
    callers = store.get_callers("helpers.py", "helper")
    assert any(c["src_symbol"] == "run" for c in callers)
    store.close()


@pytest.mark.mocked
def test_get_callees(sample_repo, tmp_path):
    b = RepoGraphBuilder(str(sample_repo))
    edges = b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    store.upsert_edges(edges)
    callees = store.get_callees("main.py", "run")
    assert any(c["dst_symbol"] == "helper" for c in callees)
    store.close()


@pytest.mark.mocked
def test_get_references(sample_repo, tmp_path):
    b = RepoGraphBuilder(str(sample_repo))
    edges = b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    store.upsert_edges(edges)
    refs = store.get_references("helpers.py", "helper")
    assert len(refs) >= 1
    assert all("src_file" in r and "src_line" in r for r in refs)
    store.close()


@pytest.mark.mocked
def test_search_limit_and_shape(sample_repo, tmp_path):
    b = RepoGraphBuilder(str(sample_repo))
    b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    rows = store.search("hel", top_k=1)
    assert len(rows) <= 1
    if rows:
        assert {"file", "line", "symbol"} <= set(rows[0].keys())
    store.close()


@pytest.mark.mocked
def test_no_stdout_pollution(sample_repo, tmp_path, capsys):
    b = RepoGraphBuilder(str(sample_repo))
    edges = b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    store.upsert_edges(edges)
    store.search("helper", 5)
    store.close()
    captured = capsys.readouterr()
    assert captured.out == ""
