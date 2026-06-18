---
name: paper-rag
description: >
  Agentic RAG over scientific PDFs with inline citations verified against Semantic Scholar.
  Use when you need to answer questions grounded in a local PDF library, find evidence
  for a claim in ingested papers, or retrieve relevant passages for academic writing.
  Produces cited answers — each claim traceable to a source paper and page.
trigger: "Use when answering questions from a local PDF library or finding evidence for academic claims"
tools:
  - paper_rag.add_papers
  - paper_rag.query
  - paper_rag.search
  - paper_rag.list_papers
version: "0.1.0"
license: MIT
requires: [pdf-parse]
---

# paper-rag

Cited agentic RAG over scientific PDFs using PaperQA2, backed by the shared Chroma container.

## Tools

- `paper_rag.add_papers(paths: list[str]) -> str` — ingest PDFs (parse, embed, store). Returns JSON `{added, errors, count}`.
- `paper_rag.query(question: str, top_k: int = 5) -> str` — cited answer. Returns JSON `{question, answer, evidence, citations}`.
- `paper_rag.search(query: str, top_k: int = 10) -> str` — similarity search. Returns JSONL, one match per line.
- `paper_rag.list_papers() -> str` — list ingested papers. Returns JSON array of `{title, path, docname, citation}`.

## Environment

- `CHROMA_URL` (default `http://chroma:8000`) — Chroma container, client-server mode.
- `PAPER_RAG_COLLECTION` (default `paper_rag`).
- `PAPER_RAG_EMBED_MODEL` (default `st-all-MiniLM-L6-v2`) — local embedding model.

## Notes

- stdout is reserved for MCP JSON-RPC. All logs go to stderr.
- Embeddings are local-first; no OpenAI embeddings are used.
