import json
from unittest.mock import MagicMock

import pytest

from semantic_scholar import SemanticScholarClient

NORMALIZED_KEYS = {
    "paper_id",
    "title",
    "authors",
    "year",
    "doi",
    "citation_count",
    "venue",
    "abstract",
    "tldr",
    "open_access_url",
}


@pytest.fixture
def client(monkeypatch):
    c = SemanticScholarClient()
    c._ss = MagicMock()
    return c


@pytest.mark.mocked
def test_normalize_has_consistent_keys(client, raw_paper):
    norm = client._normalize_paper(raw_paper)
    assert set(norm.keys()) == NORMALIZED_KEYS
    assert norm["paper_id"] == "abc123"
    assert norm["doi"] == "10.5555/3295222.3295349"
    assert norm["citation_count"] == 100000
    assert norm["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert norm["tldr"] == "Proposes the Transformer architecture."
    assert norm["open_access_url"] == "https://arxiv.org/pdf/1706.03762"


@pytest.mark.mocked
def test_normalize_handles_missing_fields(client):
    norm = client._normalize_paper({"paperId": "x"})
    assert set(norm.keys()) == NORMALIZED_KEYS
    assert norm["authors"] == []
    assert norm["citation_count"] == 0
    assert norm["doi"] is None
    assert norm["tldr"] is None


@pytest.mark.mocked
def test_search_returns_normalized_papers(client, raw_paper):
    client._ss.search_paper.return_value = [raw_paper, raw_paper]
    papers = client.search("transformers", limit=2)
    assert len(papers) == 2
    for p in papers:
        assert set(p.keys()) == NORMALIZED_KEYS
        assert p["title"] == "Attention Is All You Need"


@pytest.mark.mocked
def test_search_respects_limit(client, raw_paper):
    client._ss.search_paper.return_value = [raw_paper] * 10
    papers = client.search("x", limit=3)
    assert len(papers) == 3


@pytest.mark.mocked
def test_search_passes_year_from(client, raw_paper):
    client._ss.search_paper.return_value = [raw_paper]
    client.search("x", limit=5, year_from=2020)
    _, kwargs = client._ss.search_paper.call_args
    assert kwargs["year"] == "2020-"


@pytest.mark.mocked
def test_search_omits_year_when_none(client, raw_paper):
    client._ss.search_paper.return_value = [raw_paper]
    client.search("x", limit=5)
    _, kwargs = client._ss.search_paper.call_args
    assert "year" not in kwargs


@pytest.mark.mocked
def test_get_citations_unwraps_citing_paper(client, raw_citation_row):
    client._ss.get_paper_citations.return_value = [raw_citation_row]
    papers = client.get_citations("abc123", limit=20)
    assert len(papers) == 1
    assert papers[0]["title"] == "BERT"
    assert set(papers[0].keys()) == NORMALIZED_KEYS


@pytest.mark.mocked
def test_get_references_unwraps_cited_paper(client, raw_reference_row):
    client._ss.get_paper_references.return_value = [raw_reference_row]
    papers = client.get_references("abc123", limit=20)
    assert len(papers) == 1
    assert papers[0]["title"] == "Neural Machine Translation"
    assert papers[0]["year"] == 2014


@pytest.mark.mocked
def test_find_similar_returns_normalized(client, raw_paper):
    client._ss.get_recommended_papers.return_value = [raw_paper]
    papers = client.find_similar("abc123", limit=10)
    assert len(papers) == 1
    assert set(papers[0].keys()) == NORMALIZED_KEYS


@pytest.mark.mocked
def test_get_paper_returns_single_normalized(client, raw_paper):
    client._ss.get_paper.return_value = raw_paper
    paper = client.get_paper("abc123")
    assert set(paper.keys()) == NORMALIZED_KEYS
    assert paper["venue"] == "NeurIPS"


@pytest.mark.mocked
def test_jsonl_round_trips(client, raw_paper):
    """Sanity: normalized papers serialize to one valid JSON object per line."""
    client._ss.search_paper.return_value = [raw_paper, raw_paper]
    papers = client.search("x", limit=2)
    jsonl = "\n".join(json.dumps(p) for p in papers)
    lines = jsonl.splitlines()
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert set(obj.keys()) == NORMALIZED_KEYS
