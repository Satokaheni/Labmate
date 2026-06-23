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
  - search_papers
  - get_citations
  - get_references
  - find_similar
  - get_paper
version: "0.1.0"
license: MIT
requires: []
---

# citation-graph

Citation graph traversal over Semantic Scholar (primary) with an OpenAlex
fallback. Powers literature-review snowball sampling and feeds `paper-rag`
and `citation-check`.

## Tools

- `search_papers(query, limit=10, year_from=None)` — keyword/semantic
  search. Returns JSONL of papers.
- `get_citations(paper_id, limit=20)` — forward citations (who cites this). JSONL.
- `get_references(paper_id, limit=20)` — backward citations (what this cites). JSONL.
- `find_similar(paper_id, limit=10)` — SPECTER-embedding similar papers. JSONL.
- `get_paper(paper_id)` — full metadata (abstract, venue, tldr, open_access_url). One JSON line.

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
