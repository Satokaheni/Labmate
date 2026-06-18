# repo-graph MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the repo-graph Python MCP server that provides line-level cross-file reference/call edges for repository code navigation.

**Architecture:** RepoGraphBuilder uses tree-sitter to extract definition/reference/call edges from all files in a repo, stores them in a SQLite database via GraphStore, and the MCP server exposes search and traversal tools. SQLite is chosen over MongoDB to keep this skill self-contained with no extra container dependency. All logging goes to stderr.

**Tech Stack:** Python 3.11+, `mcp` SDK, `py-tree-sitter>=0.25`, `tree-sitter-language-pack>=0.7.2`, `networkx`, SQLite (stdlib), `pytest`

---

## Critical constraints (apply to every task)

- **stdout is sacred.** All logging uses `logging` configured with `stream=sys.stderr`. NEVER `print()`. stdout carries JSON-RPC 2.0 framing; any stray byte corrupts the stream silently and produces misleading `Parse error` symptoms downstream.
- **Never tiktoken.** If any token counting is required, use `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")`. Gemma uses SentencePiece; tiktoken counts are wrong.
- **Reuse the existing AST stack.** Grammars come from `tree-sitter-language-pack` (already used by `ast-repo-map`). Do not vendor or compile grammars by hand.
- **SQLite is the only persistence.** No MongoDB, no extra container. The DB file lives under the repo's `.labmate/` directory (created if absent), keyed by `repo_path`.
- **This skill complements `ast-repo-map`.** That skill does file-level PageRank ranking; this one provides line-level cross-file reference/call edges. Do not duplicate file-ranking logic here.
- **Tree-sitter must be error-tolerant.** Parsing broken/in-progress code returns a partial tree with ERROR nodes and must NOT raise. Symbols outside the error region are still extracted.
- The server is a **child process** spawned by the SkillRegistry over stdio. No TTY, no banners; all paths are resolved relative to `repo_path`.

---

## Edge model (shared vocabulary for every task)

A single `Edge` dataclass is the unit of the graph. Both `graph_builder.py` and `graph_store.py` use it verbatim — do not introduce a second shape.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Edge:
    src_file: str       # repo-relative path of the referencing site
    src_line: int       # 1-based line of the reference
    src_symbol: str     # enclosing definition name at the reference site ("" if module-level)
    dst_file: str       # repo-relative path where the symbol is defined
    dst_line: int       # 1-based line of the definition
    dst_symbol: str     # the referenced symbol's name
    kind: str           # 'call' | 'import' | 'type_ref' | 'inherit'
```

Resolution rule: a reference (call/type/inherit/import) is matched to a definition by symbol name across the repo's definition table. Unresolved references (no matching definition) are dropped — the graph only records edges between known code elements.

---

## Phase 0 — Scaffolding

### Task 0.1 — Create directory structure

- [ ] Create the skill server and test directories.

```bash
mkdir -p /Users/zachstallbohm/Work/gemma/services/skills/repo-graph
mkdir -p /Users/zachstallbohm/Work/gemma/tests/services/skills/repo-graph
```

### Task 0.2 — Write requirements.txt

- [ ] Create `services/skills/repo-graph/requirements.txt`:

```text
mcp>=1.0.0
py-tree-sitter>=0.25
tree-sitter-language-pack>=0.7.2
networkx>=3.0
```

> `sqlite3` is stdlib — not listed. `transformers` is only added if token counting is later needed.

### Task 0.3 — Write SKILL.md

- [ ] Create `services/skills/repo-graph/SKILL.md` with the frontmatter below, then a model-agnostic markdown body (no absolute paths):

```markdown
---
name: repo-graph
description: >
  Line-level repository code graph providing cross-file reference and call edges.
  Use when you need to find all callers of a function, all references to a symbol,
  or understand the dependency structure of a codebase at function granularity.
  Complements ast-repo-map (file-level ranking) with precise cross-file edges.
trigger: "Use when tracing function calls, finding all usages, or understanding cross-file dependencies"
tools:
  - repo_graph.build
  - repo_graph.search
  - repo_graph.get_references
  - repo_graph.get_callers
  - repo_graph.get_callees
version: "0.1.0"
license: MIT
requires: []
---

# Repo Graph Skill

You have access to the `repo_graph` MCP server which builds a line-level code
graph of a repository: cross-file edges connecting where each symbol is defined
to every place it is referenced or called.

## When to Use

- Find every caller of a function before changing its signature
- Find all references to a symbol across files (impact analysis)
- Trace what a function calls (its dependencies)
- Search for symbols by name when you do not know the file

This complements `ast-repo-map`: that skill ranks *which files* matter; this
skill answers *how symbols connect* at function granularity.

## Available Tools

### `repo_graph.build(repo_path)`
Build or update the graph for a repo. Run this once before querying. Returns
summary stats (files parsed, definitions, edges by kind).

### `repo_graph.search(query, top_k=10)`
Find symbols matching `query` by name. Returns JSONL of matches with
`file:line` locations.

### `repo_graph.get_references(file, symbol)`
Every site that references `symbol` defined in `file`. Returns JSONL.

### `repo_graph.get_callers(file, symbol)`
Functions/methods that call `symbol`. Returns JSONL.

### `repo_graph.get_callees(file, symbol)`
Functions/methods that `symbol` calls. Returns JSONL.

## Workflow

1. Call `repo_graph.build` once at the start.
2. Use `search` to locate a symbol, then `get_callers` / `get_callees` /
   `get_references` to traverse the graph.
```

---

## Phase 1 — GraphStore (SQLite persistence)

### Task 1.1 — Define the schema and connection

- [ ] Create `services/skills/repo-graph/graph_store.py` with the import block, logger, and schema DDL.

```python
import json
import logging
import os
import sqlite3
import sys

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("repo-graph")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS definitions (
    file   TEXT NOT NULL,
    line   INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    PRIMARY KEY (file, symbol, line)
);
CREATE TABLE IF NOT EXISTS edges (
    src_file   TEXT NOT NULL,
    src_line   INTEGER NOT NULL,
    src_symbol TEXT NOT NULL,
    dst_file   TEXT NOT NULL,
    dst_line   INTEGER NOT NULL,
    dst_symbol TEXT NOT NULL,
    kind       TEXT NOT NULL,
    PRIMARY KEY (src_file, src_line, dst_file, dst_line, dst_symbol, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_symbol, dst_file);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_symbol, src_file);
CREATE INDEX IF NOT EXISTS idx_defs_symbol ON definitions(symbol);
"""
```

### Task 1.2 — Implement `GraphStore.__init__`

- [ ] Open the SQLite DB and apply the schema. Store rows as dicts via `row_factory`.

```python
class GraphStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        log.info("GraphStore opened at %s", db_path)

    def close(self) -> None:
        self._conn.close()
```

### Task 1.3 — Implement `upsert_edges` (and definitions)

- [ ] Persist both the definition table (for resolution + search) and the edge table. Clear prior rows for a clean rebuild.

```python
    def upsert_definitions(self, defs: list[dict]) -> None:
        self._conn.execute("DELETE FROM definitions")
        self._conn.executemany(
            "INSERT OR REPLACE INTO definitions (file, line, symbol) VALUES (?, ?, ?)",
            [(d["file"], d["line"], d["symbol"]) for d in defs],
        )
        self._conn.commit()

    def upsert_edges(self, edges: list) -> None:
        self._conn.execute("DELETE FROM edges")
        self._conn.executemany(
            """INSERT OR REPLACE INTO edges
               (src_file, src_line, src_symbol, dst_file, dst_line, dst_symbol, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(e.src_file, e.src_line, e.src_symbol,
              e.dst_file, e.dst_line, e.dst_symbol, e.kind) for e in edges],
        )
        self._conn.commit()
        log.info("upserted %d edges", len(edges))
```

### Task 1.4 — Implement `search`

- [ ] Substring/prefix match over the definitions table, bounded by `top_k`.

```python
    def search(self, query: str, top_k: int = 10) -> list[dict]:
        rows = self._conn.execute(
            """SELECT file, line, symbol FROM definitions
               WHERE symbol LIKE ?
               ORDER BY (symbol = ?) DESC, length(symbol) ASC
               LIMIT ?""",
            (f"%{query}%", query, top_k),
        ).fetchall()
        return [dict(r) for r in rows]
```

### Task 1.5 — Implement `get_references`

- [ ] All edges whose destination is the given symbol+file (every site that uses it).

```python
    def get_references(self, file: str, symbol: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT src_file, src_line, src_symbol, kind
               FROM edges WHERE dst_symbol = ? AND dst_file = ?
               ORDER BY src_file, src_line""",
            (symbol, file),
        ).fetchall()
        return [dict(r) for r in rows]
```

### Task 1.6 — Implement `get_callers`

- [ ] References filtered to `kind = 'call'`, returning the enclosing caller symbol.

```python
    def get_callers(self, file: str, symbol: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT src_file, src_line, src_symbol, kind
               FROM edges WHERE dst_symbol = ? AND dst_file = ? AND kind = 'call'
               ORDER BY src_file, src_line""",
            (symbol, file),
        ).fetchall()
        return [dict(r) for r in rows]
```

### Task 1.7 — Implement `get_callees`

- [ ] Call edges *originating* from the given symbol (what it calls).

```python
    def get_callees(self, file: str, symbol: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT dst_file, dst_line, dst_symbol, kind
               FROM edges WHERE src_symbol = ? AND src_file = ? AND kind = 'call'
               ORDER BY dst_file, dst_line""",
            (symbol, file),
        ).fetchall()
        return [dict(r) for r in rows]
```

---

## Phase 2 — RepoGraphBuilder (tree-sitter extraction)

### Task 2.1 — Imports, logger, and `Edge` dataclass

- [ ] Create `services/skills/repo-graph/graph_builder.py` with the shared `Edge` dataclass and language detection.

```python
import logging
import os
import sys
from dataclasses import dataclass

from tree_sitter_language_pack import get_parser

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("repo-graph")

_LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".rs": "rust",
}

@dataclass(frozen=True)
class Edge:
    src_file: str
    src_line: int
    src_symbol: str
    dst_file: str
    dst_line: int
    dst_symbol: str
    kind: str
```

### Task 2.2 — `RepoGraphBuilder.__init__` and file discovery

- [ ] Walk the repo, skipping VCS/build/cache dirs, collecting only files with a known grammar.

```python
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".labmate", "dist", "build", ".venv"}

class RepoGraphBuilder:
    def __init__(self, repo_root: str):
        self.repo_root = os.path.abspath(repo_root)
        self._definitions: dict[str, list[tuple[str, int]]] = {}  # symbol -> [(file, line)]

    def _iter_source_files(self):
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                ext = os.path.splitext(fn)[1]
                if ext in _LANG_BY_EXT:
                    yield os.path.join(dirpath, fn)

    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self.repo_root)
```

### Task 2.3 — `build` orchestration (two passes)

- [ ] First pass collects all definitions (needed to resolve references); second pass extracts and resolves edges.

```python
    def build(self) -> list[Edge]:
        files = list(self._iter_source_files())
        # Pass 1: definitions
        self._definitions.clear()
        for path in files:
            for sym, line in self._extract_definitions(path):
                self._definitions.setdefault(sym, []).append((self._rel(path), line))
        # Pass 2: edges
        edges: list[Edge] = []
        for path in files:
            edges.extend(self._extract_edges(path))
        log.info("build: %d files, %d defs, %d edges",
                 len(files), sum(len(v) for v in self._definitions.values()), len(edges))
        return edges

    def definitions(self) -> list[dict]:
        return [{"file": f, "line": ln, "symbol": s}
                for s, locs in self._definitions.items() for (f, ln) in locs]
```

### Task 2.4 — `_extract_definitions` (per file, error-tolerant)

- [ ] Parse a file; collect function/class/method definition names and their lines. Never raise on parse errors.

```python
    def _extract_definitions(self, path: str) -> list[tuple[str, int]]:
        lang = _LANG_BY_EXT[os.path.splitext(path)[1]]
        try:
            with open(path, "rb") as fh:
                src = fh.read()
            tree = get_parser(lang).parse(src)
        except Exception as exc:  # noqa: BLE001 - never crash the builder
            log.warning("parse failed for %s: %s", path, exc)
            return []
        out: list[tuple[str, int]] = []
        _DEF_KINDS = {"function_definition", "class_definition",
                      "function_declaration", "method_definition", "function_item",
                      "struct_item", "class_declaration"}

        def walk(node):
            if node.type in _DEF_KINDS:
                name = node.child_by_field_name("name")
                if name is not None:
                    out.append((src[name.start_byte:name.end_byte].decode("utf-8", "replace"),
                                name.start_point[0] + 1))
            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return out
```

### Task 2.5 — `_extract_edges` (per file, resolve against definition table)

- [ ] Find call / inheritance / type / import references, determine the enclosing definition (`src_symbol`), and resolve each to a known definition. Drop unresolved references.

```python
    def _extract_edges(self, path: str) -> list[Edge]:
        lang = _LANG_BY_EXT[os.path.splitext(path)[1]]
        try:
            with open(path, "rb") as fh:
                src = fh.read()
            tree = get_parser(lang).parse(src)
        except Exception as exc:  # noqa: BLE001
            log.warning("parse failed for %s: %s", path, exc)
            return []

        rel = self._rel(path)
        edges: list[Edge] = []
        _DEF_KINDS = {"function_definition", "class_definition",
                      "function_declaration", "method_definition", "function_item",
                      "struct_item", "class_declaration"}

        def enclosing_symbol(node) -> str:
            cur = node.parent
            while cur is not None:
                if cur.type in _DEF_KINDS:
                    name = cur.child_by_field_name("name")
                    if name is not None:
                        return src[name.start_byte:name.end_byte].decode("utf-8", "replace")
                cur = cur.parent
            return ""

        def emit(name_node, kind):
            name = src[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")
            targets = self._definitions.get(name)
            if not targets:
                return  # unresolved reference -> dropped
            for dst_file, dst_line in targets:
                if dst_file == rel and dst_line == name_node.start_point[0] + 1:
                    continue  # skip self-definition edge
                edges.append(Edge(
                    src_file=rel, src_line=name_node.start_point[0] + 1,
                    src_symbol=enclosing_symbol(name_node),
                    dst_file=dst_file, dst_line=dst_line, dst_symbol=name, kind=kind,
                ))

        def walk(node):
            if node.type in ("call", "call_expression"):
                fn = node.child_by_field_name("function") or (node.children[0] if node.children else None)
                if fn is not None:
                    callee = fn.child_by_field_name("attribute") or fn
                    if callee.type in ("identifier", "property_identifier"):
                        emit(callee, "call")
            elif node.type in ("import_from_statement", "import_statement", "import_declaration"):
                for ident in node.children:
                    if ident.type in ("dotted_name", "identifier"):
                        emit(ident, "import")
            elif node.type in ("type", "type_identifier"):
                emit(node, "type_ref")
            elif node.type in ("argument_list", "superclasses", "class_heritage"):
                for ch in node.children:
                    if ch.type in ("identifier", "type_identifier"):
                        emit(ch, "inherit")
            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return edges
```

> Node-type names vary by grammar; the sets above cover Python/TS/JS/Rust. Keep extraction permissive — over-matching is fine because unresolved names are dropped at `emit`.

---

## Phase 3 — MCP server entry point

### Task 3.1 — Server scaffold, logger, helpers

- [ ] Create `services/skills/repo-graph/server.py`. Wire stdio transport; logging to stderr only; resolve the per-repo DB path.

```python
import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from graph_builder import RepoGraphBuilder
from graph_store import GraphStore

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("repo-graph")

app = Server("repo-graph")

def _db_path(repo_path: str) -> str:
    return os.path.join(os.path.abspath(repo_path), ".labmate", "repo_graph.sqlite")

def _jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in rows)
```

### Task 3.2 — Declare the five tools (`list_tools`)

- [ ] Register all five tools with JSON Schema. Names must match the SKILL.md frontmatter exactly.

```python
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="repo_graph.build",
             description="Build/update the line-level code graph for a repo. Returns summary stats.",
             inputSchema={"type": "object",
                          "properties": {"repo_path": {"type": "string"}},
                          "required": ["repo_path"]}),
        Tool(name="repo_graph.search",
             description="Search symbols by name. Returns JSONL of file:line matches.",
             inputSchema={"type": "object",
                          "properties": {"query": {"type": "string"},
                                         "top_k": {"type": "integer", "default": 10}},
                          "required": ["query"]}),
        Tool(name="repo_graph.get_references",
             description="All sites that reference a symbol. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"file": {"type": "string"},
                                         "symbol": {"type": "string"}},
                          "required": ["file", "symbol"]}),
        Tool(name="repo_graph.get_callers",
             description="Functions/methods that call a symbol. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"file": {"type": "string"},
                                         "symbol": {"type": "string"}},
                          "required": ["file", "symbol"]}),
        Tool(name="repo_graph.get_callees",
             description="Functions/methods this symbol calls. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"file": {"type": "string"},
                                         "symbol": {"type": "string"}},
                          "required": ["file", "symbol"]}),
    ]
```

### Task 3.3 — Implement `call_tool` dispatch

- [ ] One dispatcher routes all five tools. `build` writes to SQLite; the read tools open the store and query. The DB path is derived from the last-built repo (cached in a module global), with `build` setting it.

```python
_LAST_REPO: dict[str, str] = {}  # holds {"path": <repo_path>}

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "repo_graph.build":
        repo_path = arguments["repo_path"]
        builder = RepoGraphBuilder(repo_path)
        edges = builder.build()
        store = GraphStore(_db_path(repo_path))
        store.upsert_definitions(builder.definitions())
        store.upsert_edges(edges)
        store.close()
        _LAST_REPO["path"] = repo_path
        by_kind: dict[str, int] = {}
        for e in edges:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        stats = {"repo_path": repo_path,
                 "definitions": len(builder.definitions()),
                 "edges": len(edges), "by_kind": by_kind}
        return [TextContent(type="text", text=json.dumps(stats))]

    repo_path = _LAST_REPO.get("path")
    if repo_path is None:
        return [TextContent(type="text",
                            text=json.dumps({"error": "call repo_graph.build first"}))]
    store = GraphStore(_db_path(repo_path))
    try:
        if name == "repo_graph.search":
            rows = store.search(arguments["query"], int(arguments.get("top_k", 10)))
        elif name == "repo_graph.get_references":
            rows = store.get_references(arguments["file"], arguments["symbol"])
        elif name == "repo_graph.get_callers":
            rows = store.get_callers(arguments["file"], arguments["symbol"])
        elif name == "repo_graph.get_callees":
            rows = store.get_callees(arguments["file"], arguments["symbol"])
        else:
            return [TextContent(type="text",
                                text=json.dumps({"error": f"unknown tool {name}"}))]
    finally:
        store.close()
    return [TextContent(type="text", text=_jsonl(rows))]
```

### Task 3.4 — `main()` stdio entry point

- [ ] Run the server over stdio. No banner on stdout.

```python
async def _run() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())

def main() -> None:
    import asyncio
    asyncio.run(_run())

if __name__ == "__main__":
    main()
```

---

## Phase 4 — Tests (mocked, `tmp_path`)

### Task 4.1 — `conftest.py` fixtures

- [ ] Create `tests/services/skills/repo-graph/conftest.py` that writes a small two-file Python repo with a known cross-file call relationship into `tmp_path`.

```python
import sys
from pathlib import Path

import pytest

# make the skill modules importable
sys.path.insert(0, str(Path(__file__).resolve().parents[4]
                       / "services" / "skills" / "repo-graph"))

@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "helpers.py").write_text(
        "def helper():\n    return 1\n"
    )
    (tmp_path / "main.py").write_text(
        "from helpers import helper\n"
        "\n"
        "def run():\n"
        "    return helper()\n"
    )
    return tmp_path
```

### Task 4.2 — `test_graph_builder.py`: build extracts call edge

- [ ] Verify `build()` yields a `call` edge from `run` (main.py) to `helper` (helpers.py).

```python
import pytest
from graph_builder import RepoGraphBuilder

@pytest.mark.mocked
def test_build_extracts_cross_file_call(sample_repo):
    edges = RepoGraphBuilder(str(sample_repo)).build()
    calls = [e for e in edges if e.kind == "call" and e.dst_symbol == "helper"]
    assert any(e.src_symbol == "run" and e.dst_file == "helpers.py" for e in calls)
```

### Task 4.3 — `test_graph_builder.py`: definitions collected

- [ ] Verify both `helper` and `run` appear in the definition table.

```python
@pytest.mark.mocked
def test_definitions_collected(sample_repo):
    b = RepoGraphBuilder(str(sample_repo))
    b.build()
    names = {d["symbol"] for d in b.definitions()}
    assert {"helper", "run"} <= names
```

### Task 4.4 — `test_graph_builder.py`: malformed file does not raise

- [ ] Add a syntactically broken file; `build()` must still return edges for the valid files.

```python
@pytest.mark.mocked
def test_broken_file_tolerated(sample_repo):
    (sample_repo / "broken.py").write_text("def oops(:\n  pass\n")
    edges = RepoGraphBuilder(str(sample_repo)).build()
    assert any(e.dst_symbol == "helper" for e in edges)
```

### Task 4.5 — `test_graph_store.py`: callers round-trip

- [ ] Build, persist, and verify `get_callers` returns `run` as a caller of `helper`.

```python
import pytest
from graph_builder import RepoGraphBuilder
from graph_store import GraphStore

@pytest.mark.mocked
def test_get_callers(sample_repo, tmp_path):
    b = RepoGraphBuilder(str(sample_repo))
    edges = b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    store.upsert_edges(edges)
    callers = store.get_callers("helpers.py", "helper")
    assert any(c["src_symbol"] == "run" for c in callers)
    store.close()
```

### Task 4.6 — `test_graph_store.py`: callees round-trip

- [ ] Verify `get_callees("main.py", "run")` includes `helper`.

```python
@pytest.mark.mocked
def test_get_callees(sample_repo, tmp_path):
    b = RepoGraphBuilder(str(sample_repo))
    edges = b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    store.upsert_edges(edges)
    callees = store.get_callees("main.py", "run")
    assert any(c["dst_symbol"] == "helper" for c in callees)
    store.close()
```

### Task 4.7 — `test_graph_store.py`: references include all sites

- [ ] Verify `get_references` returns at least one reference to `helper` (the call site; the import is also acceptable).

```python
@pytest.mark.mocked
def test_get_references(sample_repo, tmp_path):
    b = RepoGraphBuilder(str(sample_repo))
    edges = b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    store.upsert_edges(edges)
    refs = store.get_references("helpers.py", "helper")
    assert len(refs) >= 1
    assert all("src_file" in r and "src_line" in r for r in refs)
    store.close()
```

### Task 4.8 — `test_graph_store.py`: search respects top_k and shape

- [ ] Verify `search` returns JSONL-able dicts and honors `top_k`.

```python
@pytest.mark.mocked
def test_search_limit_and_shape(sample_repo, tmp_path):
    b = RepoGraphBuilder(str(sample_repo))
    b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    rows = store.search("hel", top_k=1)
    assert len(rows) <= 1
    if rows:
        assert {"file", "line", "symbol"} <= set(rows[0].keys())
    store.close()
```

### Task 4.9 — `test_graph_store.py`: no stdout pollution

- [ ] Building and querying must emit nothing to stdout (logs go to stderr).

```python
@pytest.mark.mocked
def test_no_stdout_pollution(sample_repo, tmp_path, capsys):
    b = RepoGraphBuilder(str(sample_repo))
    edges = b.build()
    store = GraphStore(str(tmp_path / "db" / "g.sqlite"))
    store.upsert_definitions(b.definitions())
    store.upsert_edges(edges)
    store.search("helper", 5)
    store.close()
    captured = capsys.readouterr()
    assert captured.out == ""
```

### Task 4.10 — Run the suite

- [ ] Install deps and run the mocked tests; all must pass.

```bash
pip install -r /Users/zachstallbohm/Work/gemma/services/skills/repo-graph/requirements.txt
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skills/repo-graph -m mocked -v
```

---

## Phase 5 — Verification

### Task 5.1 — stdout-purity smoke test of the server

- [ ] Confirm the server starts and its stdout carries only JSON-RPC (no banner). Send an `initialize` request and assert the first stdout byte is `{`.

```bash
cd /Users/zachstallbohm/Work/gemma/services/skills/repo-graph
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  | python server.py 2>/dev/null | head -c1
# expect: {
```

### Task 5.2 — Self-review checklist

- [ ] All five tools (`build`, `search`, `get_references`, `get_callers`, `get_callees`) are declared in `list_tools`, dispatched in `call_tool`, listed in SKILL.md frontmatter, and exercised by tests.
- [ ] `Edge`, `GraphStore`, `RepoGraphBuilder` names are used consistently across `graph_builder.py`, `graph_store.py`, `server.py`, and tests.
- [ ] No `print()` anywhere; all logging via `logging` to `sys.stderr`.
- [ ] No `tiktoken`, no `chromadb.PersistentClient`, no MongoDB.
- [ ] SQLite DB path is per-repo under `.labmate/`; `.labmate` is in `_SKIP_DIRS`.
- [ ] No placeholder/TODO bodies — every method has a working implementation.
