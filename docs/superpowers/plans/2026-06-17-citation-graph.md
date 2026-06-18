# citation-graph MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the citation-graph Python MCP server for literature review via Semantic Scholar and OpenAlex citation traversal.

**Architecture:** SemanticScholarClient wraps the `semanticscholar` Python library with consistent field normalization. All 5 tools return JSONL for predictable response sizes. Rate limiting (0.6s inter-request delay) prevents hitting the free-tier limit. API key is optional (env var). All logging to stderr.

**Tech Stack:** Python 3.11+, `mcp` SDK, `semanticscholar>=0.8`, `httpx`, `pydantic>=2`, `pytest`

---

## Critical rules (do not violate)

- **stdout is sacred**: every log line goes to `sys.stderr`. NEVER `print()` and NEVER attach a `StreamHandler` to stdout. stdout carries JSON-RPC 2.0 only — a single stray byte corrupts the stream silently.
- Use `httpx` for OpenAlex calls (async-capable, not `requests`).
- Semantic Scholar API key via `SS_API_KEY = os.getenv("SS_API_KEY", "")` — optional, free tier works without. Pass `None` (not `""`) to the library when unset.
- Rate limiting: 100 requests / 5 min unauthenticated. Add `asyncio.sleep(0.6)` between calls in batch/multi-call operations.
- All tool results are JSONL (one JSON object per line) — never a single JSON array.
- `_normalize_paper` is the ONLY place that maps raw API shapes to our field names. Every method that returns paper dicts must route through it so callers always see the same keys.

---

## Normalized paper schema

Every paper dict returned by `_normalize_paper` has exactly these keys (missing source fields become `None` or `0`, never absent):

```python
{
    "paper_id": str,         # Semantic Scholar paperId
    "title": str | None,
    "authors": list[str],    # author display names, possibly empty
    "year": int | None,
    "doi": str | None,       # from externalIds.DOI
    "citation_count": int,   # defaults to 0
    "venue": str | None,
    "abstract": str | None,
    "tldr": str | None,      # tldr.text if present
    "open_access_url": str | None,  # openAccessPdf.url if present
}
```

---

## Task 1: Create directory structure and requirements.txt

- [ ] Create directories `services/skills/citation-graph/` and `tests/services/skills/citation-graph/`
- [ ] Create `services/skills/citation-graph/requirements.txt`:

```
mcp
semanticscholar>=0.8
httpx
pydantic>=2
```

- [ ] Add a dev requirement note: tests need `pytest` and `pytest-asyncio` (installed at repo level, not in this file).

---

## Task 2: Implement `semantic_scholar.py` — client skeleton + `_normalize_paper`

- [ ] Create `services/skills/citation-graph/semantic_scholar.py` with logging wired to stderr and the normalization helper.

```python
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
```

---

## Task 3: Implement `search` on `SemanticScholarClient`

- [ ] Add `search` to `semantic_scholar.py`. Apply the `year_from` filter via the library's `year` parameter (`"{year_from}-"` open-ended range) and normalize every result.

```python
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
```

---

## Task 4: Implement `get_citations` and `get_references`

- [ ] Add both methods. Each returns normalized paper dicts. The library returns wrapper objects whose `.paper` (citations) / referenced paper holds the actual paper; handle both wrapper and bare-paper shapes defensively.

```python
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
```

---

## Task 5: Implement `find_similar` and `get_paper`

- [ ] Add both methods. `find_similar` uses the recommendations / similar-paper endpoint backed by SPECTER embeddings; `get_paper` returns one normalized paper.

```python
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
```

---

## Task 6: Implement `openalexclient.py` — OpenAlex fallback via httpx

- [ ] Create `services/skills/citation-graph/openalexclient.py`. This is the fallback/secondary source used when Semantic Scholar returns nothing or errors. Use `httpx` (async), normalize to the same schema, and set a polite `User-Agent` with the user email (OpenAlex "polite pool").

```python
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
```

---

## Task 7: Implement `server.py` — MCP server entry point

- [ ] Create `services/skills/citation-graph/server.py`. Register the 5 tools, each returning JSONL (one normalized paper per line; `get_paper` returns a single line). Insert `asyncio.sleep(0.6)` is not needed per single tool call, but include the helper for any future batch path and call it before each Semantic Scholar request to stay polite.

```python
import asyncio
import json
import logging
import sys

from mcp.server.fastmcp import FastMCP

from semantic_scholar import SemanticScholarClient

log = logging.getLogger("citation-graph.server")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

mcp = FastMCP("citation-graph")
_ss = SemanticScholarClient()

# Free-tier politeness: serialize SS calls and space them out.
_rate_lock = asyncio.Lock()


async def _throttle() -> None:
    async with _rate_lock:
        await asyncio.sleep(0.6)


def _to_jsonl(papers: list[dict]) -> str:
    return "\n".join(json.dumps(p, ensure_ascii=False) for p in papers)


@mcp.tool()
async def search_papers(query: str, limit: int = 10, year_from: int | None = None) -> str:
    """Keyword/semantic search. Returns JSONL of papers
    (title/authors/year/doi/citation_count/...)."""
    await _throttle()
    papers = await asyncio.to_thread(_ss.search, query, limit, year_from)
    return _to_jsonl(papers)


@mcp.tool()
async def get_citations(paper_id: str, limit: int = 20) -> str:
    """Papers that cite this paper (forward citations). Returns JSONL."""
    await _throttle()
    papers = await asyncio.to_thread(_ss.get_citations, paper_id, limit)
    return _to_jsonl(papers)


@mcp.tool()
async def get_references(paper_id: str, limit: int = 20) -> str:
    """Papers this paper cites (references / backward citations). Returns JSONL."""
    await _throttle()
    papers = await asyncio.to_thread(_ss.get_references, paper_id, limit)
    return _to_jsonl(papers)


@mcp.tool()
async def find_similar(paper_id: str, limit: int = 10) -> str:
    """Embedding-based (SPECTER) similar papers. Returns JSONL."""
    await _throttle()
    papers = await asyncio.to_thread(_ss.find_similar, paper_id, limit)
    return _to_jsonl(papers)


@mcp.tool()
async def get_paper(paper_id: str) -> str:
    """Full metadata for one paper (abstract, venue, tldr, open_access_url). Returns one JSON line."""
    await _throttle()
    paper = await asyncio.to_thread(_ss.get_paper, paper_id)
    return json.dumps(paper, ensure_ascii=False)


if __name__ == "__main__":
    log.info("starting citation-graph MCP server on stdio")
    mcp.run()  # FastMCP defaults to stdio transport
```

- [ ] Confirm the tool names exposed to MCP are `search_papers`, `get_citations`, `get_references`, `find_similar`, `get_paper` (FastMCP namespaces them under the `citation-graph` server, matching the `citation_graph.*` SKILL.md tool ids).

---

## Task 8: Write `SKILL.md`

- [ ] Create `services/skills/citation-graph/SKILL.md`:

```markdown
---
name: citation-graph
description: >
  Citation graph traversal using Semantic Scholar and OpenAlex. Use for literature
  review snowball sampling: search for papers, get all papers that cite a given paper
  (forward citations), get all papers it references (backward citations), or find
  semantically similar papers. Feeds paper-rag (for ingestion) and citation-check
  (for hallucination verification).
trigger: "Use when finding related papers, doing literature review, or tracing citation chains"
tools:
  - citation_graph.search_papers
  - citation_graph.get_citations
  - citation_graph.get_references
  - citation_graph.find_similar
  - citation_graph.get_paper
version: "0.1.0"
license: MIT
requires: []
---

# citation-graph

Citation graph traversal over Semantic Scholar (primary) with an OpenAlex
fallback. Powers literature-review snowball sampling and feeds `paper-rag`
and `citation-check`.

## Tools

- `citation_graph.search_papers(query, limit=10, year_from=None)` — keyword/semantic
  search. Returns JSONL of papers.
- `citation_graph.get_citations(paper_id, limit=20)` — forward citations (who cites this). JSONL.
- `citation_graph.get_references(paper_id, limit=20)` — backward citations (what this cites). JSONL.
- `citation_graph.find_similar(paper_id, limit=10)` — SPECTER-embedding similar papers. JSONL.
- `citation_graph.get_paper(paper_id)` — full metadata (abstract, venue, tldr, open_access_url). One JSON line.

## Paper ID formats accepted

Semantic Scholar paperId, `DOI:10.xxxx/...`, `arXiv:2106.xxxxx`, `CorpusId:12345`.

## Output schema

Every paper line has: `paper_id`, `title`, `authors`, `year`, `doi`,
`citation_count`, `venue`, `abstract`, `tldr`, `open_access_url`.

## Configuration

- `SS_API_KEY` (optional) — Semantic Scholar API key. Free tier works unauthenticated
  (100 requests / 5 min).

## Notes

- All output is JSONL (one JSON object per line), never a single array.
- All logging goes to stderr; stdout carries JSON-RPC only.
```

---

## Task 9: Write test fixtures — `conftest.py`

- [ ] Create `tests/services/skills/citation-graph/conftest.py`. Make the skill importable, and provide raw-shaped fixtures plus a patched `SemanticScholarClient`.

```python
import os
import sys

import pytest

# Make the skill package importable.
SKILL_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../../../services/skills/citation-graph")
)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)


@pytest.fixture
def raw_paper():
    """A raw Semantic Scholar paper object (dict shape)."""
    return {
        "paperId": "abc123",
        "title": "Attention Is All You Need",
        "authors": [{"name": "Ashish Vaswani"}, {"name": "Noam Shazeer"}],
        "year": 2017,
        "externalIds": {"DOI": "10.5555/3295222.3295349", "ArXiv": "1706.03762"},
        "citationCount": 100000,
        "venue": "NeurIPS",
        "abstract": "The dominant sequence transduction models...",
        "tldr": {"text": "Proposes the Transformer architecture."},
        "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762"},
    }


@pytest.fixture
def raw_citation_row():
    """A forward-citation wrapper row (citingPaper shape)."""
    return {
        "citingPaper": {
            "paperId": "cite1",
            "title": "BERT",
            "authors": [{"name": "Jacob Devlin"}],
            "year": 2018,
            "externalIds": {"DOI": "10.18653/v1/N19-1423"},
            "citationCount": 80000,
            "venue": "NAACL",
            "abstract": "We introduce BERT...",
            "tldr": None,
            "openAccessPdf": None,
        }
    }


@pytest.fixture
def raw_reference_row():
    """A backward-reference wrapper row (citedPaper shape)."""
    return {
        "citedPaper": {
            "paperId": "ref1",
            "title": "Neural Machine Translation",
            "authors": [{"name": "Dzmitry Bahdanau"}],
            "year": 2014,
            "externalIds": {"DOI": "10.0000/nmt"},
            "citationCount": 30000,
            "venue": "ICLR",
            "abstract": None,
            "tldr": None,
            "openAccessPdf": None,
        }
    }
```

---

## Task 10: Write `test_semantic_scholar.py`

- [ ] Create `tests/services/skills/citation-graph/test_semantic_scholar.py`. Mock the underlying `SemanticScholar` library methods; assert normalization and JSONL behavior. All tests `@pytest.mark.mocked`.

```python
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
```

---

## Task 11: Verify and run

- [ ] Install deps into the test environment: `pip install -r services/skills/citation-graph/requirements.txt pytest pytest-asyncio`
- [ ] Run the mocked tests: `pytest tests/services/skills/citation-graph/ -m mocked -v` — all green.
- [ ] Smoke-test the server starts on stdio without writing to stdout:
  `python services/skills/citation-graph/server.py < /dev/null` should log "starting citation-graph MCP server on stdio" to **stderr** and emit nothing on stdout before EOF closes it.
- [ ] Confirm no `print()` and no stdout `StreamHandler` anywhere in the skill: `grep -rn "print(" services/skills/citation-graph/` returns nothing.

---

## Done criteria

- [ ] All 5 tools registered and return JSONL (single line for `get_paper`).
- [ ] `_normalize_paper` is the sole shaping path; every returned dict has the 10 schema keys.
- [ ] `year_from` maps to the `"{year}-"` range for Semantic Scholar.
- [ ] All logging on stderr; stdout carries JSON-RPC only.
- [ ] Mocked test suite passes with no network access.
