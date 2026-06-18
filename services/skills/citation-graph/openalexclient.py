import logging
import sys

import httpx

log = logging.getLogger("citation-graph.openalex")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

OPENALEX_BASE = "https://api.openalex.org"
# Polite pool: identify the client. Replace with a service mailbox if available.
USER_AGENT = "labmate-citation-graph/0.1 (mailto:labmate@example.com)"


class OpenAlexClient:
    def __init__(self) -> None:
        self._headers = {"User-Agent": USER_AGENT}

    async def search(
        self, query: str, limit: int = 10, year_from: int | None = None
    ) -> list[dict]:
        params = {"search": query, "per-page": min(limit, 200)}
        if year_from is not None:
            params["filter"] = f"from_publication_date:{year_from}-01-01"
        log.info("openalex search query=%r limit=%d year_from=%s", query, limit, year_from)
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(f"{OPENALEX_BASE}/works", params=params)
            resp.raise_for_status()
            data = resp.json()
        return [self._normalize_work(w) for w in data.get("results", [])[:limit]]

    def _normalize_work(self, w: dict) -> dict:
        """Map an OpenAlex Work to the shared paper schema."""
        authorships = w.get("authorships") or []
        authors = [
            a.get("author", {}).get("display_name")
            for a in authorships
            if a.get("author", {}).get("display_name")
        ]
        doi = w.get("doi")
        if doi and doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]

        oa = w.get("open_access") or {}
        oa_url = oa.get("oa_url")

        # OpenAlex stores abstract as an inverted index; reconstruct if present.
        abstract = None
        inv = w.get("abstract_inverted_index")
        if inv:
            positions = {}
            for word, idxs in inv.items():
                for i in idxs:
                    positions[i] = word
            abstract = " ".join(positions[i] for i in sorted(positions))

        return {
            "paper_id": w.get("id"),
            "title": w.get("display_name"),
            "authors": authors,
            "year": w.get("publication_year"),
            "doi": doi,
            "citation_count": w.get("cited_by_count") or 0,
            "venue": (w.get("primary_location") or {}).get("source", {}).get("display_name")
            if w.get("primary_location")
            else None,
            "abstract": abstract,
            "tldr": None,
            "open_access_url": oa_url,
        }
