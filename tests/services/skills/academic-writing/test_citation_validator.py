import pytest

import conftest as ct
from citation_validator import CitationValidator, CitationResult, deduplicate_keys


def _entry(key, title, author, doi=None):
    doi_line = f"  doi = {{{doi}}},\n" if doi else ""
    return (f"@article{{{key},\n  title = {{{title}}},\n"
            f"  author = {{{author}}},\n{doi_line}}}")


def _crossref_response(title, authors):
    return {"message": {
        "title": [title],
        "author": [{"family": a.split()[-1], "given": a.split()[0]} for a in authors],
        "issued": {"date-parts": [[2024]]},
        "DOI": "10.1/x",
    }}


def test_valid_doi_returns_crossref(monkeypatch):
    entry = _entry("smith2024", "Deep Nets For Things", "Jane Smith", doi="10.1/x")
    cr = ct.FakeCrossref(response=_crossref_response("Deep Nets For Things", ["Jane Smith"]))
    v = CitationValidator(crossref=cr, semantic_scholar=ct.FakeSemanticScholar())
    [res] = v.validate([entry])
    assert res.valid is True
    assert res.source == "crossref"
    assert res.normalized_bibtex is not None


def test_doi_mismatch_flags_for_review(monkeypatch):
    entry = _entry("smith2024", "A Totally Different Title", "Jane Smith", doi="10.1/x")
    cr = ct.FakeCrossref(response=_crossref_response("Deep Nets For Things", ["Jane Smith"]))
    v = CitationValidator(crossref=cr, semantic_scholar=ct.FakeSemanticScholar())
    [res] = v.validate([entry])
    assert res.valid is False
    assert res.flagged_for_review is True
    assert "mismatch" in (res.conflict_reason or "")


def test_no_identifier_falls_to_semantic_scholar(monkeypatch):
    entry = _entry("doe2023", "Some Real Paper", "John Doe and Jane Roe")
    hits = [ct.FakeHit("Some Real Paper", ["John Doe", "Jane Roe"], year=2023)]
    v = CitationValidator(crossref=ct.FakeCrossref(), semantic_scholar=ct.FakeSemanticScholar(hits=hits))
    [res] = v.validate([entry])
    assert res.valid is True
    assert res.source == "semantic_scholar"


def test_no_identifier_no_author_overlap_flags(monkeypatch):
    entry = _entry("doe2023", "Some Real Paper", "John Doe")
    hits = [ct.FakeHit("Some Real Paper", ["Completely Different Author"])]
    v = CitationValidator(crossref=ct.FakeCrossref(), semantic_scholar=ct.FakeSemanticScholar(hits=hits))
    [res] = v.validate([entry])
    assert res.valid is False
    assert res.flagged_for_review is True


def test_deduplicate_keys_appends_suffixes():
    r1 = CitationResult(entry_id="smith2024", valid=True, source="crossref",
                        normalized_bibtex="@article{smith2024,\n  title = {A}\n}")
    r2 = CitationResult(entry_id="smith2024", valid=True, source="crossref",
                        normalized_bibtex="@article{smith2024,\n  title = {B}\n}")
    out = deduplicate_keys([r1, r2])
    assert out[0].entry_id == "smith2024"
    assert out[1].entry_id == "smith2024a"
    assert "{smith2024a," in out[1].normalized_bibtex
