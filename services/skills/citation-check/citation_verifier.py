"""CitationVerifier — CiteCheck (arXiv:2605.27700) bibliography hallucination
detection via the deterministic cascade (DOI→Crossref→arXiv→Semantic Scholar)."""
from __future__ import annotations

import logging
import sys
from difflib import SequenceMatcher

import bibtexparser
from habanero import Crossref
from semanticscholar import SemanticScholar

from models import CitationCheckResult

# All diagnostics to stderr — stdout is reserved for JSON-RPC.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("citation-check.citation")

_cr = Crossref()
_s2 = SemanticScholar()


def _parse_entries(bib_str: str) -> list[dict]:
    """Parse a single BibTeX string into a list of normalized field dicts."""
    db = bibtexparser.loads(bib_str)
    entries = []
    for e in db.entries:
        entries.append(
            {
                "id": e.get("ID", "unknown"),
                "title": e.get("title", "").strip("{} ").strip(),
                "author": e.get("author", "").strip(),
                "year": e.get("year", "").strip(),
                "doi": e.get("doi", "").strip(),
                "raw": e,
            }
        )
    return entries


def _from_crossref_item(item: dict, source: str) -> dict:
    authors = item.get("author", []) or []
    author = " and ".join(
        f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in authors
    )
    title = (item.get("title") or [""])[0]
    issued = item.get("issued", {}).get("date-parts", [[None]])
    year = str(issued[0][0]) if issued and issued[0] and issued[0][0] else ""
    doi = item.get("DOI", "")
    return {"title": title, "author": author, "year": year, "doi": doi, "source": source}


def _resolve(entry: dict) -> dict | None:
    # 1. DOI → Crossref (authoritative)
    if entry["doi"]:
        try:
            item = _cr.works(ids=entry["doi"]).get("message")
            if item:
                return _from_crossref_item(item, "crossref")
        except Exception as exc:  # noqa: BLE001
            log.warning("crossref DOI lookup failed: %s", exc)
    # 2. Title → Crossref
    if entry["title"]:
        try:
            res = _cr.works(query_bibliographic=entry["title"], limit=1)
            items = res.get("message", {}).get("items", [])
            if items:
                return _from_crossref_item(items[0], "crossref")
        except Exception as exc:  # noqa: BLE001
            log.warning("crossref title lookup failed: %s", exc)
    # 3. arXiv id embedded in DOI/title (deterministic prefix check) — optional stage
    # 4. Title → Semantic Scholar
    if entry["title"]:
        try:
            results = _s2.search_paper(entry["title"], limit=1)
            if results:
                p = results[0]
                author = " and ".join(a.name for a in (p.authors or []))
                return {
                    "title": p.title or "",
                    "author": author,
                    "year": str(p.year or ""),
                    "doi": (p.externalIds or {}).get("DOI", "") or "",
                    "source": "semantic_scholar",
                }
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic scholar lookup failed: %s", exc)
    return None


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _last_names(author: str) -> set[str]:
    names = set()
    for part in author.replace(" and ", ";").replace(",", ";").split(";"):
        tok = part.strip().split()
        if tok:
            names.add(tok[-1].lower() if "," not in part else tok[0].lower())
    return {n for n in names if n}


def _classify(entry: dict, resolved: dict | None) -> CitationCheckResult:
    if resolved is None:
        return CitationCheckResult(
            entry_id=entry["id"],
            verdict="major_hallucination",
            field_errors=["entry not found in any source"],
            source=None,
        )

    field_errors: list[str] = []
    if entry["title"] and _norm(entry["title"]) != _norm(resolved["title"]):
        # title mismatch is severe — likely a different paper
        ratio = SequenceMatcher(None, _norm(entry["title"]), _norm(resolved["title"])).ratio()
        if ratio < 0.6:
            return CitationCheckResult(
                entry_id=entry["id"],
                verdict="major_hallucination",
                field_errors=["title"],
                source=resolved["source"],
            )
        field_errors.append("title")

    if entry["year"] and resolved["year"] and entry["year"] != resolved["year"]:
        field_errors.append("year")
    if entry["author"] and resolved["author"]:
        if not (_last_names(entry["author"]) & _last_names(resolved["author"])):
            field_errors.append("author")
    if entry["doi"] and resolved["doi"] and _norm(entry["doi"]) != _norm(resolved["doi"]):
        field_errors.append("doi")

    verdict = "exact_match" if not field_errors else "minor_hallucination"
    return CitationCheckResult(
        entry_id=entry["id"],
        verdict=verdict,
        field_errors=field_errors,
        source=resolved["source"],
        normalized_bibtex=_to_bibtex(entry["id"], resolved),
    )


def _to_bibtex(entry_id: str, r: dict) -> str:
    return (
        f"@article{{{entry_id},\n"
        f"  title = {{{r['title']}}},\n"
        f"  author = {{{r['author']}}},\n"
        f"  year = {{{r['year']}}},\n"
        f"  doi = {{{r['doi']}}}\n}}"
    )


def verify_citations(bibliography: list[str]) -> list[CitationCheckResult]:
    results: list[CitationCheckResult] = []
    for bib_str in bibliography:
        for entry in _parse_entries(bib_str):
            resolved = _resolve(entry)
            results.append(_classify(entry, resolved))
    return results
