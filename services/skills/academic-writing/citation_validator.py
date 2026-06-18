"""Deterministic-first citation validation cascade.

Citation hallucination rates of 11-57% across deployed LLMs make this cascade
non-optional. Every LLM-generated citation must pass before entering the bibliography.
Cascade order: DOI -> Crossref, arXiv ID -> arXiv API, title -> Semantic Scholar,
LLM fallback (advisory only, always flagged).
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

import bibtexparser
import requests
from habanero import Crossref
from pydantic import BaseModel
from semanticscholar import SemanticScholar

logger = logging.getLogger(__name__)

POLITE_EMAIL = "zach.stallbohm@gmail.com"  # Crossref polite pool
AUTHOR_OVERLAP_THRESHOLD = 0.60
TITLE_MATCH_THRESHOLD = 0.85
ARXIV_API = "http://export.arxiv.org/api/query"


class CitationResult(BaseModel):
    entry_id: str
    valid: bool
    source: str | None = None  # 'crossref' | 'arxiv' | 'semantic_scholar' | 'llm_fallback'
    flagged_for_review: bool = False
    normalized_bibtex: str | None = None
    conflict_reason: str | None = None


def _extract_identifier(rec: dict) -> tuple[str | None, str | None]:
    """Return (identifier, type) where type is 'doi' or 'arxiv'. ('', None) if none."""
    doi = rec.get("doi")
    if doi:
        return doi.strip(), "doi"
    eprint = rec.get("eprint") or rec.get("arxiv")
    if eprint:
        return eprint.strip(), "arxiv"
    url = rec.get("url", "")
    m = re.search(r"arxiv\.org/abs/([\w.]+)", url)
    if m:
        return m.group(1), "arxiv"
    m = re.search(r"doi\.org/(10\.\S+)", url)
    if m:
        return m.group(1), "doi"
    return None, None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _author_set(names: list[str]) -> set[str]:
    """Reduce author names to lowercase last-name tokens."""
    out = set()
    for n in names:
        n = n.strip()
        if not n:
            continue
        last = n.split(",")[0].strip() if "," in n else n.split()[-1]
        out.add(_norm(last))
    return {a for a in out if a}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def _bibtex_authors(rec: dict) -> list[str]:
    return [a.strip() for a in rec.get("author", "").split(" and ") if a.strip()]


def _crossref_title(meta: dict) -> str:
    titles = meta.get("message", {}).get("title", [])
    return titles[0] if titles else ""


def _crossref_authors(meta: dict) -> list[str]:
    out = []
    for a in meta.get("message", {}).get("author", []):
        family = a.get("family", "")
        given = a.get("given", "")
        if family:
            out.append(f"{family}, {given}".strip(", "))
    return out


def _crossref_to_bibtex(meta: dict, entry_id: str) -> str:
    msg = meta.get("message", {})
    title = _crossref_title(meta)
    authors = " and ".join(_crossref_authors(meta))
    year = ""
    parts = msg.get("issued", {}).get("date-parts", [[None]])
    if parts and parts[0] and parts[0][0]:
        year = str(parts[0][0])
    doi = msg.get("DOI", "")
    return (
        f"@article{{{entry_id},\n"
        f"  title = {{{title}}},\n"
        f"  author = {{{authors}}},\n"
        f"  year = {{{year}}},\n"
        f"  doi = {{{doi}}}\n}}"
    )


def _arxiv_lookup(arxiv_id: str) -> dict | None:
    """Query the arXiv Atom API. Returns {title, authors:[...]} or None."""
    try:
        resp = requests.get(
            ARXIV_API, params={"id_list": arxiv_id, "max_results": 1}, timeout=15
        )
        resp.raise_for_status()
    except requests.RequestException:
        logger.warning("arXiv lookup failed for %s", arxiv_id)
        return None
    body = resp.text
    title_m = re.search(r"<entry>.*?<title>(.*?)</title>", body, re.S)
    if not title_m:
        return None
    authors = re.findall(r"<author>\s*<name>(.*?)</name>", body, re.S)
    return {"title": title_m.group(1).strip(), "authors": [a.strip() for a in authors]}


def _arxiv_to_bibtex(meta: dict, entry_id: str) -> str:
    authors = " and ".join(meta["authors"])
    return (
        f"@article{{{entry_id},\n"
        f"  title = {{{meta['title']}}},\n"
        f"  author = {{{authors}}},\n"
        f"  archivePrefix = {{arXiv}}\n}}"
    )


def _ss_to_bibtex(hit, entry_id: str) -> str:
    authors = " and ".join(a["name"] for a in (getattr(hit, "authors", None) or []))
    year = getattr(hit, "year", "") or ""
    return (
        f"@article{{{entry_id},\n"
        f"  title = {{{getattr(hit, 'title', '')}}},\n"
        f"  author = {{{authors}}},\n"
        f"  year = {{{year}}}\n}}"
    )


class CitationValidator:
    """Deterministic-first cascade: DOI -> Crossref, arXiv -> arXiv API,
    title -> Semantic Scholar, LLM fallback (advisory, always flagged)."""

    def __init__(self, polite_email: str = POLITE_EMAIL,
                 crossref: Crossref | None = None,
                 semantic_scholar: SemanticScholar | None = None):
        self._cr = crossref or Crossref(mailto=polite_email)
        self._ss = semantic_scholar or SemanticScholar()

    def validate(self, bibtex_entries: list[str]) -> list[CitationResult]:
        return [self._validate_one(e) for e in bibtex_entries]

    def _validate_one(self, entry: str) -> CitationResult:
        try:
            parsed = bibtexparser.loads(entry).entries
            rec = parsed[0]
        except Exception:
            return CitationResult(
                entry_id="?", valid=False, flagged_for_review=True,
                conflict_reason="bibtexparser failed to parse entry",
            )
        if not rec:
            return CitationResult(
                entry_id="?", valid=False, flagged_for_review=True,
                conflict_reason="empty bibtex entry",
            )

        entry_id = rec.get("ID", "unknown")
        rec_authors = _author_set(_bibtex_authors(rec))
        ident, ident_type = _extract_identifier(rec)

        if ident and ident_type == "doi":
            r = self._check_doi(entry_id, ident, rec, rec_authors)
            if r is not None:
                return r
        elif ident and ident_type == "arxiv":
            return self._check_arxiv(entry_id, ident, rec, rec_authors)

        return self._check_semantic_scholar(entry_id, rec, rec_authors)

    def _check_doi(self, entry_id, doi, rec, rec_authors) -> CitationResult | None:
        try:
            meta = self._cr.works(ids=doi)
        except Exception:
            logger.warning("Crossref lookup failed for %s; falling through", doi)
            return None  # fall through to Semantic Scholar
        title_ok = _title_similarity(_crossref_title(meta), rec.get("title", "")) >= TITLE_MATCH_THRESHOLD
        author_ok = _overlap(_author_set(_crossref_authors(meta)), rec_authors) >= AUTHOR_OVERLAP_THRESHOLD
        if title_ok and author_ok:
            return CitationResult(
                entry_id=entry_id, valid=True, source="crossref",
                normalized_bibtex=_crossref_to_bibtex(meta, entry_id),
            )
        return CitationResult(
            entry_id=entry_id, valid=False, flagged_for_review=True,
            conflict_reason="DOI resolves but title/author mismatch",
        )

    def _check_arxiv(self, entry_id, arxiv_id, rec, rec_authors) -> CitationResult:
        meta = _arxiv_lookup(arxiv_id)
        if (meta
                and _title_similarity(meta["title"], rec.get("title", "")) >= TITLE_MATCH_THRESHOLD
                and _overlap(_author_set(meta["authors"]), rec_authors) >= AUTHOR_OVERLAP_THRESHOLD):
            return CitationResult(
                entry_id=entry_id, valid=True, source="arxiv",
                normalized_bibtex=_arxiv_to_bibtex(meta, entry_id),
            )
        return CitationResult(
            entry_id=entry_id, valid=False, flagged_for_review=True,
            conflict_reason="arXiv ID resolves but title/author mismatch",
        )

    def _check_semantic_scholar(self, entry_id, rec, rec_authors) -> CitationResult:
        title = rec.get("title", "")
        if title:
            try:
                hits = self._ss.search_paper(title, limit=3)
            except Exception:
                logger.warning("Semantic Scholar lookup failed for %r", title)
                hits = []
            for hit in hits:
                hit_authors = _author_set([a["name"] for a in (getattr(hit, "authors", None) or [])])
                if _overlap(hit_authors, rec_authors) >= AUTHOR_OVERLAP_THRESHOLD:
                    return CitationResult(
                        entry_id=entry_id, valid=True, source="semantic_scholar",
                        normalized_bibtex=_ss_to_bibtex(hit, entry_id),
                    )
        return CitationResult(
            entry_id=entry_id, valid=False, flagged_for_review=True,
            conflict_reason="No identifier found; title search returned no author-overlap match. "
                            "Likely hallucinated.",
        )


def deduplicate_keys(results: list[CitationResult]) -> list[CitationResult]:
    """Append a/b/c disambiguators to colliding bibtex keys among valid results.

    Mutates entry_id and normalized_bibtex of duplicates in place and returns the list.
    """
    seen: dict[str, int] = {}
    for r in results:
        if not r.valid:
            continue
        base = r.entry_id
        if base not in seen:
            seen[base] = 0
            continue
        seen[base] += 1
        suffix = chr(ord("a") + seen[base] - 1)
        new_key = f"{base}{suffix}"
        if r.normalized_bibtex:
            r.normalized_bibtex = r.normalized_bibtex.replace(f"{{{base},", f"{{{new_key},", 1)
        r.entry_id = new_key
    return results
