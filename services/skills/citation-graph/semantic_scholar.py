import asyncio
import logging
import os
import sys

from semanticscholar import SemanticScholar

# stdout is sacred — log to stderr only.
log = logging.getLogger("citation-graph.ss")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# Fields we request from Semantic Scholar. Keep in one place.
PAPER_FIELDS = [
    "paperId",
    "title",
    "authors",
    "year",
    "externalIds",
    "citationCount",
    "venue",
    "abstract",
    "tldr",
    "openAccessPdf",
]


class SemanticScholarClient:
    def __init__(self) -> None:
        api_key = os.getenv("SS_API_KEY", "") or None
        if api_key:
            log.info("SemanticScholar client initialized with API key")
        else:
            log.info("SemanticScholar client initialized (unauthenticated free tier)")
        self._ss = SemanticScholar(api_key=api_key)

    def _normalize_paper(self, raw: dict) -> dict:
        """Map a raw Semantic Scholar paper object to our stable schema.

        `raw` may be a dict or a semanticscholar Paper model; both expose
        items via .get / attribute access. We coerce to dict-like reads.
        """
        def g(key, default=None):
            if isinstance(raw, dict):
                return raw.get(key, default)
            return getattr(raw, key, default)

        authors = g("authors") or []
        author_names = []
        for a in authors:
            name = a.get("name") if isinstance(a, dict) else getattr(a, "name", None)
            if name:
                author_names.append(name)

        external_ids = g("externalIds") or {}
        doi = external_ids.get("DOI") if isinstance(external_ids, dict) else None

        tldr_obj = g("tldr") or {}
        tldr = tldr_obj.get("text") if isinstance(tldr_obj, dict) else None

        oa = g("openAccessPdf") or {}
        oa_url = oa.get("url") if isinstance(oa, dict) else None

        return {
            "paper_id": g("paperId"),
            "title": g("title"),
            "authors": author_names,
            "year": g("year"),
            "doi": doi,
            "citation_count": g("citationCount") or 0,
            "venue": g("venue"),
            "abstract": g("abstract"),
            "tldr": tldr,
            "open_access_url": oa_url,
        }

    def _unwrap(self, item) -> dict:
        """Citation/reference rows wrap the paper under .paper / ['citingPaper']
        / ['citedPaper']. Return the inner paper object."""
        for attr in ("paper", "citingPaper", "citedPaper"):
            if isinstance(item, dict) and item.get(attr):
                return item[attr]
            inner = getattr(item, attr, None)
            if inner:
                return inner
        return item

    def search(
        self, query: str, limit: int = 10, year_from: int | None = None
    ) -> list[dict]:
        kwargs = {"limit": limit, "fields": PAPER_FIELDS}
        if year_from is not None:
            kwargs["year"] = f"{year_from}-"
        log.info("search query=%r limit=%d year_from=%s", query, limit, year_from)
        results = self._ss.search_paper(query, **kwargs)
        papers = []
        for raw in results:
            papers.append(self._normalize_paper(raw))
            if len(papers) >= limit:
                break
        return papers

    def get_citations(self, paper_id: str, limit: int = 20) -> list[dict]:
        log.info("get_citations paper_id=%r limit=%d", paper_id, limit)
        rows = self._ss.get_paper_citations(paper_id, fields=PAPER_FIELDS, limit=limit)
        out = []
        for row in rows:
            out.append(self._normalize_paper(self._unwrap(row)))
            if len(out) >= limit:
                break
        return out

    def get_references(self, paper_id: str, limit: int = 20) -> list[dict]:
        log.info("get_references paper_id=%r limit=%d", paper_id, limit)
        rows = self._ss.get_paper_references(paper_id, fields=PAPER_FIELDS, limit=limit)
        out = []
        for row in rows:
            out.append(self._normalize_paper(self._unwrap(row)))
            if len(out) >= limit:
                break
        return out

    def find_similar(self, paper_id: str, limit: int = 10) -> list[dict]:
        log.info("find_similar paper_id=%r limit=%d", paper_id, limit)
        rows = self._ss.get_recommended_papers(paper_id, fields=PAPER_FIELDS, limit=limit)
        out = []
        for raw in rows:
            out.append(self._normalize_paper(raw))
            if len(out) >= limit:
                break
        return out

    def get_paper(self, paper_id: str) -> dict:
        log.info("get_paper paper_id=%r", paper_id)
        raw = self._ss.get_paper(paper_id, fields=PAPER_FIELDS)
        return self._normalize_paper(raw)
