# Semantic Codegraph Search Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add semantic/vector search to Labmate's codegraph so the agent can find relevant symbols by meaning, not just by name/keyword.

**Context:** The existing codegraph is an external npm package (v0.9.9) with a SQLite DB at `.codegraph/codegraph.db`. It has 2,794 nodes and 4,599 edges, uses FTS5 for keyword search, and runs a file watcher that auto-syncs in ~20–50ms. There are no vector columns — it's purely lexical. The goal is a shadow indexer that reads from codegraph's SQLite, embeds each symbol, stores in Chroma, and exposes a `code_semantic_search` MCP tool the orchestrator can call alongside the existing structural codegraph tools.

**Architecture:**
- A new Python service `services/codegraph-embedder/` reads from `.codegraph/codegraph.db`, embeds each node's `kind + name + signature + docstring`, and upserts into a new Chroma collection `code_symbols`.
- A 5-second poll on the `files` table (`indexed_at` changes) triggers incremental re-embedding of changed files.
- The service also exposes an MCP stdio server with one tool: `code_semantic_search(query, k)` — hybrid BM25 + dense over `code_symbols`, reranked with the existing cross-encoder.
- The orchestrator's MCPClientManager connects to this new MCP server at startup alongside the existing mcp-bridge.

**Tech Stack:** Python, SQLite (read-only, Motor not needed), the existing `Embedder` + `Reranker` from `services/memory/`, Chroma HTTP client (same instance as memory service), `rank_bm25`, `mcp` Python SDK (stdio transport).

**Reuse these existing components:**
- `services/memory/embedder.py` — `Embedder` class, async, batched, Redis-cached
- `services/memory/reranker.py` — `Reranker` class, BAAI/bge-reranker-v2-m3
- `services/memory/storage_manager.py` — Chroma collection creation pattern (`get_or_create_collection`)
- `services/memory/context_manager.py` — `hybrid_retrieve` logic as a template for code search

---

## Files

| File | Change |
|------|--------|
| `services/codegraph-embedder/__init__.py` | New (empty) |
| `services/codegraph-embedder/indexer.py` | New — reads SQLite nodes, embeds, upserts to Chroma `code_symbols`, polls for changes |
| `services/codegraph-embedder/search.py` | New — `hybrid_code_search(query, k)` combining BM25 + dense + rerank |
| `services/codegraph-embedder/server.py` | New — MCP stdio server exposing `code_semantic_search` tool |
| `services/codegraph-embedder/requirements.txt` | New — mcp, rank_bm25, chromadb-client, aiohttp, aiosqlite |
| `services/memory/storage_manager.py` | Add `code_symbols` collection to `_setup_chroma()` |
| `infrastructure/local/start.sh` | Start codegraph-embedder MCP server alongside other services |

---

## Embedding text per node

```python
def node_to_text(row: dict) -> str:
    parts = [f"{row['kind']} {row['qualified_name'] or row['name']}"]
    if row["signature"]:
        parts.append(row["signature"])
    if row["docstring"]:
        parts.append(row["docstring"])
    parts.append(f"in {row['file_path']}")
    return "\n".join(parts)
```

Chroma metadata per node:
```json
{
  "node_id": "...",
  "file_path": "services/orchestrator/main.py",
  "kind": "function",
  "name": "run_task",
  "qualified_name": "CodingOrchestrator.run_task",
  "language": "python",
  "start_line": 699,
  "end_line": 740
}
```

---

## Task 1: Add `code_symbols` Chroma collection

**Files:**
- Modify: `services/memory/storage_manager.py`

### Step 1: Add `code_symbols` to `_setup_chroma`

Find the loop in `_setup_chroma()` (around line 51-57) that creates `("episodic", "semantic", "procedural")` and extend it:

```python
for col in ("episodic", "semantic", "procedural", "code_symbols"):
    col = await self.chroma.get_or_create_collection(
        col,
        metadata={"embed_model": EMBED_MODEL, "hnsw:space": "cosine"},
    )
```

That's the only change. The collection is empty until the indexer fills it.

---

## Task 2: Shadow indexer (`indexer.py`)

**Files:**
- Create: `services/codegraph-embedder/indexer.py`

```python
"""Reads codegraph SQLite, embeds symbols, upserts to Chroma code_symbols."""
from __future__ import annotations
import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite
import aiohttp

log = logging.getLogger("codegraph_embedder")

DB_PATH    = Path(".codegraph/codegraph.db")
POLL_SECS  = 5
BATCH_SIZE = 64
CHROMA_URL = "http://localhost:8000"   # same Chroma instance as memory service
COLLECTION = "code_symbols"


def node_to_text(row: dict) -> str:
    parts = [f"{row['kind']} {row['qualified_name'] or row['name']}"]
    if row["signature"]:
        parts.append(row["signature"])
    if row["docstring"]:
        parts.append(row["docstring"])
    parts.append(f"in {row['file_path']}")
    return "\n".join(parts)


class CodeGraphIndexer:
    def __init__(self, embedder, chroma_col):
        self._embedder = embedder    # services/memory/embedder.Embedder instance
        self._col      = chroma_col  # chromadb async collection
        self._seen_files: dict[str, float] = {}  # path → indexed_at

    async def full_index(self) -> int:
        """Embed all nodes on startup. Returns count embedded."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, kind, name, qualified_name, file_path, language, "
                "start_line, end_line, signature, docstring FROM nodes"
            )
            rows = [dict(r) for r in await cursor.fetchall()]

        count = 0
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            texts = [node_to_text(r) for r in batch]
            vecs  = await self._embedder.embed(texts)
            await self._col.upsert(
                ids        = [r["id"] for r in batch],
                embeddings = vecs,
                documents  = texts,
                metadatas  = [{
                    "node_id":        r["id"],
                    "file_path":      r["file_path"] or "",
                    "kind":           r["kind"] or "",
                    "name":           r["name"] or "",
                    "qualified_name": r["qualified_name"] or "",
                    "language":       r["language"] or "",
                    "start_line":     r["start_line"] or 0,
                    "end_line":       r["end_line"] or 0,
                } for r in batch],
            )
            count += len(batch)
        log.info("full_index: %d nodes embedded", count)
        return count

    async def _changed_files(self) -> list[str]:
        """Return file paths whose indexed_at changed since last poll."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT path, indexed_at FROM files")
            rows = [dict(r) for r in await cur.fetchall()]

        changed = []
        for r in rows:
            prev = self._seen_files.get(r["path"])
            if prev is None or r["indexed_at"] != prev:
                self._seen_files[r["path"]] = r["indexed_at"]
                changed.append(r["path"])
        return changed

    async def incremental_update(self, changed_paths: list[str]) -> None:
        """Re-embed nodes from changed files; delete nodes from missing files."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join("?" * len(changed_paths))
            cur = await db.execute(
                f"SELECT id, kind, name, qualified_name, file_path, language, "
                f"start_line, end_line, signature, docstring FROM nodes "
                f"WHERE file_path IN ({placeholders})",
                changed_paths,
            )
            rows = [dict(r) for r in await cur.fetchall()]

        # Delete existing vectors for these files then re-upsert
        for path in changed_paths:
            existing = await self._col.get(where={"file_path": path})
            if existing["ids"]:
                await self._col.delete(ids=existing["ids"])

        if rows:
            texts = [node_to_text(r) for r in rows]
            vecs  = await self._embedder.embed(texts)
            await self._col.upsert(
                ids=        [r["id"] for r in rows],
                embeddings= vecs,
                documents=  texts,
                metadatas=  [{
                    "node_id":        r["id"],
                    "file_path":      r["file_path"] or "",
                    "kind":           r["kind"] or "",
                    "name":           r["name"] or "",
                    "qualified_name": r["qualified_name"] or "",
                    "language":       r["language"] or "",
                    "start_line":     r["start_line"] or 0,
                    "end_line":       r["end_line"] or 0,
                } for r in rows],
            )
            log.info("incremental_update: %d nodes re-embedded from %d files",
                     len(rows), len(changed_paths))

    async def watch(self) -> None:
        """Poll loop — runs forever, checks for changed files every POLL_SECS."""
        while True:
            await asyncio.sleep(POLL_SECS)
            try:
                changed = await self._changed_files()
                if changed:
                    await self.incremental_update(changed)
            except Exception as exc:
                log.warning("poll error (non-fatal): %s", exc)
```

---

## Task 3: Hybrid code search (`search.py`)

**Files:**
- Create: `services/codegraph-embedder/search.py`

```python
"""Hybrid BM25 + dense search over code_symbols Chroma collection."""
from __future__ import annotations
from rank_bm25 import BM25Okapi


async def hybrid_code_search(
    query: str,
    chroma_col,
    embedder,
    reranker,
    k: int = 8,
) -> list[dict]:
    """
    1. Dense: embed query → Chroma top-50
    2. BM25: tokenise candidates → BM25 top-50
    3. RRF fusion (k=60)
    4. Rerank top-20 with cross-encoder → return top k
    """
    # 1. Dense retrieval
    vec = (await embedder.embed([query]))[0]
    results = await chroma_col.query(
        query_embeddings=[vec],
        n_results=50,
        include=["documents", "metadatas", "distances"],
    )
    docs      = results["documents"][0]
    metadatas = results["metadatas"][0]
    ids       = results["ids"][0]

    if not docs:
        return []

    # 2. BM25 on the dense candidates
    tokenised = [d.lower().split() for d in docs]
    bm25      = BM25Okapi(tokenised)
    bm25_scores = bm25.get_scores(query.lower().split())

    # 3. RRF fusion
    dense_rank = {id_: i for i, id_ in enumerate(ids)}
    bm25_rank  = {ids[i]: rank for rank, i in enumerate(
        sorted(range(len(ids)), key=lambda x: bm25_scores[x], reverse=True)
    )}

    rrf: dict[str, float] = {}
    for id_ in ids:
        rrf[id_] = (
            1.0 / (60 + dense_rank.get(id_, 999)) +
            1.0 / (60 + bm25_rank.get(id_, 999))
        )

    shortlist_ids = sorted(rrf, key=rrf.__getitem__, reverse=True)[:20]
    idx_map       = {id_: i for i, id_ in enumerate(ids)}

    shortlist_docs  = [docs[idx_map[i]]      for i in shortlist_ids]
    shortlist_meta  = [metadatas[idx_map[i]] for i in shortlist_ids]

    # 4. Cross-encoder rerank
    scores = await reranker.rerank(query, shortlist_docs)
    ranked = sorted(zip(scores, shortlist_docs, shortlist_meta), reverse=True)

    return [
        {
            "score":          score,
            "text":           doc,
            "file_path":      meta["file_path"],
            "kind":           meta["kind"],
            "name":           meta["name"],
            "qualified_name": meta["qualified_name"],
            "start_line":     meta["start_line"],
            "end_line":       meta["end_line"],
        }
        for score, doc, meta in ranked[:k]
    ]
```

---

## Task 4: MCP server (`server.py`)

**Files:**
- Create: `services/codegraph-embedder/server.py`

```python
"""MCP stdio server — exposes code_semantic_search tool."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sys

import chromadb
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from services.memory.embedder import Embedder
from services.memory.reranker import Reranker
from .indexer import CodeGraphIndexer
from .search  import hybrid_code_search

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("codegraph_mcp")

CHROMA_URL  = os.getenv("CHROMA_URL",  "http://localhost:8000")
REDIS_URL   = os.getenv("REDIS_URL",   "redis://localhost:6379/0")
COLLECTION  = "code_symbols"

server = Server("codegraph-semantic")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(
        name="code_semantic_search",
        description=(
            "Search the codebase by meaning. Returns the top-k symbols "
            "(functions, classes, methods) most semantically relevant to the query. "
            "Use when you need to find code by what it does rather than what it's named."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language description of what to find"},
                "k":     {"type": "integer", "default": 8, "description": "Number of results (max 20)"},
            },
            "required": ["query"],
        },
    )]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "code_semantic_search":
        raise ValueError(f"unknown tool: {name}")

    results = await hybrid_code_search(
        query    = arguments["query"],
        chroma_col = server.state["col"],
        embedder   = server.state["embedder"],
        reranker   = server.state["reranker"],
        k          = min(int(arguments.get("k", 8)), 20),
    )
    text = json.dumps(results, indent=2)
    return [TextContent(type="text", text=text)]


async def main() -> None:
    # Initialise shared resources
    chroma   = chromadb.AsyncHttpClient(host=CHROMA_URL.split("//")[1].split(":")[0],
                                        port=int(CHROMA_URL.split(":")[-1]))
    col      = await chroma.get_or_create_collection(
        COLLECTION, metadata={"hnsw:space": "cosine"}
    )
    embedder = Embedder(redis_url=REDIS_URL)
    reranker = Reranker()
    indexer  = CodeGraphIndexer(embedder, col)

    server.state = {"col": col, "embedder": embedder, "reranker": reranker}

    # Full index on startup, then watch for changes
    await indexer.full_index()
    asyncio.create_task(indexer.watch())

    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Task 5: `requirements.txt`

**Files:**
- Create: `services/codegraph-embedder/requirements.txt`

```
mcp>=1.0
aiosqlite>=0.20
rank_bm25>=0.2.2
chromadb>=0.5
aiohttp>=3.9
```

Note: `sentence-transformers` and `FlagEmbedding` are already installed as part of the memory service dependencies.

---

## Task 6: Wire into start.sh

**Files:**
- Modify: `infrastructure/local/start.sh`

Add after the mcp-bridge start block:

```bash
info "starting codegraph semantic indexer..."
nohup python -m services.codegraph_embedder.server \
  > "$LOGS/codegraph-embedder.log" 2>&1 &
echo $! > "$PIDS/codegraph-embedder.pid"
```

Also register the MCP server in the orchestrator's MCPClientManager config so it sees the new `code_semantic_search` tool:

```python
StdioServerParameters(
    command="python",
    args=["-m", "services.codegraph_embedder.server"],
    env={**os.environ},
)
```

---

## Verification

```bash
# 1. Start the indexer in isolation and confirm it indexes all nodes
cd /Users/zachstallbohm/Work/Labmate
python -m services.codegraph_embedder.server 2>&1 | head -20
# Expected: "full_index: 2794 nodes embedded"

# 2. Query the MCP tool directly (MCP inspector or curl to Chroma)
# Check Chroma collection count:
curl -s http://localhost:8000/api/v1/collections/code_symbols | jq '.count'
# Expected: ~2794

# 3. Run a semantic query through the orchestrator:
# "find the function that handles WebSocket authentication"
# Expected: returns ws_gateway/server.py _ws_loop near line 162 (auth handshake)

# 4. Edit a file and wait 5s — verify incremental update:
# touch services/orchestrator/main.py
# After 5s, check that main.py nodes are re-indexed

# 5. Run existing tests (should be unaffected):
python -m pytest tests/services/memory/ -v
```

---

## Notes

- `aiosqlite` opens the codegraph SQLite in **read-only** mode — the indexer never writes to codegraph's DB
- Embedding model stays `BAAI/bge-small-en-v1.5` (384 dims) for consistency with existing collections; a code-specific model (`nomic-ai/nomic-embed-code`, 768 dims) is a straightforward future upgrade
- The poll approach (5s) is simpler than hooking into the daemon's Unix socket, and 5s latency is fine for code search
- The `code_symbols` collection is per-workspace scoped via `file_path` metadata — if multi-workspace support is needed later, add a `workspace_id` metadata field and filter on it

---

## Gherkin Scenarios

These scenarios describe expected system behavior at the feature level. Use them for acceptance testing and to verify the implementation matches intent.

```gherkin
Feature: code_symbols Chroma collection initialisation

  Scenario: Collection is created alongside existing memory collections
    Given Chroma is available at the configured URL
    When StorageManager._setup_chroma runs
    Then a "code_symbols" collection exists
    And the collection uses cosine similarity ("hnsw:space": "cosine")

  Scenario: Existing collections are not dropped when code_symbols is added
    Given "episodic", "semantic", and "procedural" collections already exist
    When StorageManager initialises
    Then all four collections exist and the existing ones are unchanged


Feature: Shadow codegraph indexer

  Scenario: Full index embeds all nodes on startup
    Given a codegraph SQLite database with 2794 nodes
    When CodeGraphIndexer.full_index is called
    Then all 2794 nodes are embedded
    And upserted into code_symbols in batches of 64
    And 2794 is returned

  Scenario: Embedding text includes kind, qualified name, signature, and file path
    Given a node of kind "function" named "run_task" with a signature and docstring
    When node_to_text is called
    Then the result starts with "function run_task"
    And includes the signature text
    And ends with "in <file_path>"

  Scenario: Incremental update re-indexes only changed files
    Given the poll finds that "services/orchestrator/main.py" has a new indexed_at
    When incremental_update is called with ["services/orchestrator/main.py"]
    Then old vectors for that file are deleted from code_symbols
    And fresh embeddings for nodes in that file are upserted

  Scenario: Files unchanged since last poll are not re-indexed
    Given a file whose indexed_at has not changed
    When the poll cycle runs
    Then no delete or upsert is issued for that file

  Scenario: Watch loop calls incremental_update only when changes are detected
    Given the indexer has run full_index
    When the poll detects no changed files
    Then incremental_update is not called


Feature: Hybrid BM25 + dense code search

  Scenario: Query returns results from the dense retrieval step
    Given code_symbols contains embedded nodes
    When hybrid_code_search is called with query "handle WebSocket authentication"
    Then results include nodes with names related to WebSocket handling
    And each result has file_path, kind, name, qualified_name, start_line, end_line

  Scenario: RRF fusion combines dense rank and BM25 rank
    Given 50 dense candidates retrieved from Chroma
    When hybrid_code_search computes RRF scores
    Then each candidate score accounts for both its dense rank and its BM25 rank
    And the top-20 shortlist is passed to the reranker

  Scenario: Reranker orders the shortlist by cross-encoder score
    Given a 20-candidate shortlist from RRF
    When the reranker scores each candidate
    Then results are sorted highest cross-encoder score first

  Scenario: Result count is capped at k
    Given code_symbols contains many nodes
    When hybrid_code_search is called with k=5
    Then at most 5 results are returned

  Scenario: Empty Chroma result returns empty list without error
    Given Chroma returns no documents for the query
    When hybrid_code_search is called
    Then an empty list is returned


Feature: MCP code_semantic_search tool

  Scenario: Tool is advertised by the MCP server
    Given the codegraph-embedder MCP server is running
    When the MCP client calls list_tools
    Then "code_semantic_search" is in the tool list
    And the inputSchema requires the "query" property
    And the "k" property defaults to 8

  Scenario: Tool returns ranked results for a natural-language query
    Given the indexer has completed a full_index
    When code_semantic_search is called with query "parse PDF attachments" and k 5
    Then up to 5 results are returned
    And each result contains file_path, name, kind, start_line, end_line, score

  Scenario: k is capped at 20 regardless of user input
    Given the MCP tool receives k=100
    When call_tool is executed
    Then hybrid_code_search is called with k=20

  Scenario: Unknown tool name raises ValueError
    Given the MCP server is running
    When call_tool is invoked with name "nonexistent_tool"
    Then a ValueError is raised with message "unknown tool: nonexistent_tool"


Feature: Service startup integration

  Scenario: Indexer completes full_index before serving MCP requests
    Given the codegraph MCP server starts
    When main() runs
    Then full_index completes
    And the watch loop is started as a background task
    And the MCP stdio server begins serving

  Scenario: Orchestrator discovers the code_semantic_search tool at startup
    Given the codegraph-embedder MCP server is running
    When the orchestrator's MCPClientManager initialises
    Then code_semantic_search appears in the list of available tools
```
