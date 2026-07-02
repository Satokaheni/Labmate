# session_search — full-text/regex recall over past conversation turns

**Date:** 2026-07-02

## The gap

`memory_search` does VECTOR search over distilled semantic reflections (Chroma).
It excels at fuzzy concept matching but cannot recall verbatim wording, exact
identifiers, or the literal exchange from a specific past session.
There was no way to ask "what exactly did we say about X in session Y?"

## Precedent

`hermes-agent` exposes a cheap `session_search` over its raw session transcripts —
preferred over vector RAG for exact-recall tasks because it requires zero
embeddings and zero LLM calls, returns real text, and is deterministic.

## Design

**Tool `session_search`** — params: `query` (required), `k` (1..20, default 8),
`mode` ("text"|"regex", default "text"), `session_id` (optional scope).
Results are cache-safe (appended to the message TAIL, not the prefix).

**`StorageManager.search_turns`** — queries the `chat_turns` MongoDB collection
(camelCase fields: `sessionId`, `seq`, `text`, `createdAt`):
- `mode="text"` → `$text` full-text search ranked by `textScore`
- `mode="regex"` → `$regex` + `$options:"i"`, sorted by `createdAt` desc
- Empty query → `[]` fast-path; any exception → `[]` (never raises)

**`SessionSearch` wrapper** — `services/orchestrator/session_search.py`.
Mirrors `memory_search.py` exactly: injectable store, try/except guard, ranked
snippet format `[i] (session <sid>, turn <seq>) <text>`, per-snippet cap 600
chars, total cap 4000 chars. `store=None` → sentinel string.

**Indexes** — `db_indexes.ensure_indexes` adds:
- `chat_turns: [("text", "text")]` — required for `$text` mode
- `chat_turns: [("sessionId", 1), ("seq", 1)]` — session-scoped queries

## Why cheap

No embeddings, no LLM, no Chroma. One Mongo `find` per call. The `$text` index
is maintained by Mongo natively. `session_search` iterations are refunded in the
`IterationBudget` (same as `memory_search`) so they never starve editing budget.
