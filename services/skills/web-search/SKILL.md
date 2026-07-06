---
name: web-search
description: >
  Live web search and page fetching via self-hosted SearXNG. Use for information from the
  public internet: current events and recent news, recent papers, up-to-date external
  API/library documentation and changelogs, and locating a NAMED third-party
  tool/model/library/package by its homepage or GitHub repo (e.g. "search the web for
  recent papers on X", "where can I download Whisper?" → github.com/openai/whisper).
  Exception: do NOT use for questions about THIS project's own code, architecture, or
  harness — "how does X work here", "how do I integrate/interact with the harness or this
  repo" — those are answered from the local codebase (code_semantic_search / reading
  files), not the web. Prefer over dataset-search when the target is external software/a
  repo/a model rather than a dataset. Operates fully locally (no cloud search API).
trigger: "Use for public-internet information — current events, recent papers, external docs, or locating a named third-party tool/repo. Not for questions about this project's own code or architecture."
tools:
  - search
  - fetch_page
  - search_code
version: "0.1.0"
license: MIT
requires: []
---

# web-search

Wraps a self-hosted SearXNG instance (Docker container `lm-searxng`) to provide
live web search and page-content extraction.

## Tools

### search(query, limit=10, categories=["general"])
Returns JSONL; one `SearchResult` per line: `title`, `url`, `snippet`, `source`,
`published_date`.

### fetch_page(url, max_length=8000)
Fetches a URL and extracts main text via cheerio. Returns JSON: `url`, `title`,
`text` (truncated to `max_length`), `truncated`.

### search_code(query, limit=5)
Searches the SearXNG `code` category (GitHub / StackOverflow). Returns JSONL.

## Configuration

- `SEARXNG_URL` (default `http://localhost:8080`) — base URL of the SearXNG instance.
  Inside the Docker network this is `http://searxng:8080`.

## Offline behavior

If SearXNG is unreachable, every tool returns a structured error object
(`{"error": "...", "detail": "..."}`) with `isError: true` rather than crashing.
