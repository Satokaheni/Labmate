import pytest

import citation_verifier

VALID_BIB = """@article{devlin2018bert,
  title = {BERT: Pre-training of Deep Bidirectional Transformers},
  author = {Devlin, Jacob and Chang, Ming-Wei},
  year = {2018},
  doi = {10.18653/v1/N19-1423}
}"""

FAKE_BIB = """@article{fake2099,
  title = {Quantum Telepathy in Large Language Models},
  author = {Nobody, A.},
  year = {2099}
}"""

WRONG_AUTHOR_BIB = """@article{devlin2018bert,
  title = {BERT: Pre-training of Deep Bidirectional Transformers},
  author = {Wrongname, Q.},
  year = {2018},
  doi = {10.18653/v1/N19-1423}
}"""


@pytest.mark.mocked
def test_exact_match_for_valid_doi(monkeypatch):
    monkeypatch.setattr(citation_verifier, "_resolve", lambda e: {
        "title": "BERT: Pre-training of Deep Bidirectional Transformers",
        "author": "Devlin, Jacob and Chang, Ming-Wei",
        "year": "2018", "doi": "10.18653/v1/N19-1423", "source": "crossref",
    })
    results = citation_verifier.verify_citations([VALID_BIB])
    assert results[0].verdict == "exact_match"
    assert results[0].field_errors == []
    assert results[0].source == "crossref"


@pytest.mark.mocked
def test_major_hallucination_for_fabricated_paper(monkeypatch):
    monkeypatch.setattr(citation_verifier, "_resolve", lambda e: None)
    results = citation_verifier.verify_citations([FAKE_BIB])
    assert results[0].verdict == "major_hallucination"
    assert results[0].source is None


@pytest.mark.mocked
def test_minor_hallucination_wrong_author(monkeypatch):
    monkeypatch.setattr(citation_verifier, "_resolve", lambda e: {
        "title": "BERT: Pre-training of Deep Bidirectional Transformers",
        "author": "Devlin, Jacob and Chang, Ming-Wei",
        "year": "2018", "doi": "10.18653/v1/N19-1423", "source": "crossref",
    })
    results = citation_verifier.verify_citations([WRONG_AUTHOR_BIB])
    assert results[0].verdict == "minor_hallucination"
    assert "author" in results[0].field_errors
    assert results[0].normalized_bibtex is not None
