import pytest
from bm25_index import BM25Index


@pytest.mark.mocked
def test_bm25_ranks_keyword_file_first(sample_repo):
    idx = BM25Index(str(sample_repo))
    idx.build()
    results = idx.search("parse_config key", top_k=5)
    assert results, "expected at least one hit"
    assert results[0][0] == "config.py"
    assert results[0][1] > 0.0


@pytest.mark.mocked
def test_bm25_empty_index(tmp_path):
    idx = BM25Index(str(tmp_path))
    idx.build()  # no code files
    assert idx.search("anything") == []
