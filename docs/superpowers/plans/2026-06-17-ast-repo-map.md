# ast-repo-map MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the ast-repo-map Python MCP server that gives Labmate's brain a token-budgeted, PageRank-ranked symbol map of the repository.

**Architecture:** A Python MCP stdio server exposes two tools. RepoMapper uses tree-sitter to parse files (with mtime-keyed cache), builds a file-level call graph with networkx, runs personalized PageRank boosting actively-edited files, and emits JSONL up to the token budget. The MCP server entry point wires the stdio transport; all logging goes to stderr exclusively.

**Tech Stack:** Python 3.11+, `mcp` SDK, `py-tree-sitter>=0.25`, `tree-sitter-language-pack>=0.7.2`, `networkx`, `transformers` (AutoTokenizer for token counting)

---

## Critical constraints (apply to every task)

- **stdout is sacred.** All logging uses `logging` configured with `stream=sys.stderr`. NEVER `print()`. stdout carries JSON-RPC 2.0 framing; any stray byte corrupts the stream silently and produces misleading `Parse error` symptoms downstream.
- **Never tiktoken.** Token counting uses `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")`. Gemma uses SentencePiece; tiktoken counts are wrong and cause context overflow.
- **Token budget is a hard cap, not a guideline.** Truncate with an explicit marker; never silently drop or overflow.
- **Tree-sitter must be error-tolerant.** Parsing broken/in-progress code returns a partial tree with ERROR nodes and must NOT raise. Symbols outside the error region are still extracted.
- The server is a **child process** spawned by the SkillRegistry over stdio. It must not assume a TTY, must not print banners, and reads all paths relative to `repo_root`.

---

## Phase 0 — Scaffolding

### Task 0.1 — Create directory structure

- [ ] Create the skill server and test directories.

```bash
mkdir -p /Users/zachstallbohm/Work/gemma/services/skills/ast-repo-map
mkdir -p /Users/zachstallbohm/Work/gemma/tests/services/skills/ast-repo-map
```

### Task 0.2 — Write requirements.txt

- [ ] Create `services/skills/ast-repo-map/requirements.txt`:

```text
mcp>=1.0.0
py-tree-sitter>=0.25
tree-sitter-language-pack>=0.7.2
networkx>=3.0
transformers>=4.40.0
```

### Task 0.3 — Write SKILL.md

- [ ] Create `services/skills/ast-repo-map/SKILL.md` (frontmatter from spec 3.3, body model-agnostic markdown, no absolute paths):

```markdown
---
name: ast-repo-map
description: >
  Builds a ranked repository map for code navigation. Use when the agent needs
  to understand the structure of a codebase, locate symbols, or select which
  files to edit. Emits a token-budgeted JSONL of the most important function
  and class definitions ranked by PageRank over the call graph.
trigger: "Use when starting a new task requiring codebase orientation"
tools:
  - ast.repo-map.get_repo_map
  - ast.repo-map.get_symbols
version: "0.2.0"
license: MIT
requires: []
---

# AST Repo Map Skill

You have access to the `ast.repo-map` MCP server which provides structured,
language-aware codebase navigation without reading raw source files.

## When to Use

Use this skill at the beginning of any task that requires understanding existing
code structure:

- Locating a function or class by name across a large codebase
- Identifying which files will be affected by a change
- Building context before editing so you do not over-read files

## Available Tools

### `ast.repo-map.get_repo_map`

Returns a JSONL list of the most important symbols in the repository, ranked
by personalized PageRank and bounded by a configurable token budget.

```json
{
  "chat_files": ["src/service.py"],
  "max_tokens": 2000
}
```

Each output line: `{"name": "...", "kind": "function|class|method", "signature": "...", "parent": "...", "loc": "path/to/file:42"}`

### `ast.repo-map.get_symbols`

Returns all symbols defined in a specific file.

```json
{ "file": "src/service.py" }
```

## Workflow

1. Call `get_repo_map` with the files you are actively editing in `chat_files`.
2. Review the JSONL output to understand the symbol landscape.
3. Use `loc` fields to target reads precisely — read only the functions you
   need, not entire files.
4. Re-call `get_repo_map` after edits so the map reflects current state.

## Limitations

- Does not resolve types across files — use the `ts-refactor` skill for
  type-aware TypeScript operations.
- Symbol map may lag by ~1 second after file edits (cache is mtime-keyed).
- Token budget is hard-capped; large monorepos will see truncation.
```

---

## Phase 1 — RepoMapper: parsing layer (spec 6.1)

### Task 1.1 — Create repo_mapper.py with imports, logging, and Tag dataclass

- [ ] Create `services/skills/ast-repo-map/repo_mapper.py` with the module header. Logging is configured to stderr at module import so even import-time messages are safe:

```python
"""RepoMapper: tree-sitter parse + mtime cache + networkx PageRank repo map.

CRITICAL: this module is loaded inside an MCP stdio child process.
NEVER print() or write to stdout. All logging goes to sys.stderr.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
from tree_sitter_language_pack import get_language, get_parser

log = logging.getLogger("ast.repo-map")  # handlers are configured to stderr in server.py


# Map file extensions to tree-sitter-language-pack language names.
EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
}


@dataclass
class Tag:
    name: str
    kind: str          # 'def' | 'ref'
    file: str          # repo-relative path
    line: int          # 1-based line number
    signature: str | None = None
    parent: str | None = None
```

### Task 1.2 — Add the tags.scm query strings keyed by language

- [ ] Append the S-expression query map to `repo_mapper.py`. These extract definitions and references. Each capture name starting with `name.definition.` is a def; `name.reference.` is a ref:

```python
# Minimal tags queries per language. Capture names follow the tree-sitter
# tags.scm convention: name.definition.<kind> for defs, name.reference.<kind>
# for refs. The <kind> suffix becomes Tag.kind detail via the capture name.
TAGS_QUERIES: dict[str, str] = {
    "python": """
(function_definition
  name: (identifier) @name.definition.function) @definition.function
(class_definition
  name: (identifier) @name.definition.class) @definition.class
(call
  function: (identifier) @name.reference.call)
(call
  function: (attribute attribute: (identifier) @name.reference.call))
""",
    "typescript": """
(function_declaration
  name: (identifier) @name.definition.function) @definition.function
(class_declaration
  name: (type_identifier) @name.definition.class) @definition.class
(method_definition
  name: (property_identifier) @name.definition.method) @definition.method
(call_expression
  function: (identifier) @name.reference.call)
""",
    "rust": """
(function_item
  name: (identifier) @name.definition.function) @definition.function
(struct_item
  name: (type_identifier) @name.definition.class) @definition.class
(call_expression
  function: (identifier) @name.reference.call)
""",
}
# tsx/javascript reuse the typescript query.
TAGS_QUERIES["tsx"] = TAGS_QUERIES["typescript"]
TAGS_QUERIES["javascript"] = TAGS_QUERIES["typescript"]
TAGS_QUERIES["go"] = """
(function_declaration
  name: (identifier) @name.definition.function) @definition.function
(call_expression
  function: (identifier) @name.reference.call)
"""
```

### Task 1.3 — RepoMapper.__init__ with the mtime cache

- [ ] Append the class skeleton with the cache. Cache value is `{"mtime": float, "tags": list[Tag]}`:

```python
class RepoMapper:
    """Parses a repository, caches by mtime, ranks symbols by PageRank."""

    def __init__(self, repo_root: str) -> None:
        self.repo_root = Path(repo_root).resolve()
        # path (repo-relative str) -> {"mtime": float, "tags": list[Tag]}
        self._cache: dict[str, dict] = {}
        # parse-call counter, used by tests to assert cache hits
        self._parse_count = 0
```

### Task 1.4 — RepoMapper._lang_for helper

- [ ] Append a helper that resolves a language name from a file path, returning `None` for unsupported extensions:

```python
    def _lang_for(self, path: str) -> str | None:
        return EXT_TO_LANG.get(Path(path).suffix)
```

### Task 1.5 — RepoMapper._parse_file with mtime cache and error tolerance

- [ ] Append `_parse_file`. `path` is repo-relative. It checks the cache by mtime, re-parses on miss, and NEVER raises on broken source (tree-sitter returns a partial tree). Increments `_parse_count` only on an actual parse:

```python
    def _parse_file(self, path: str) -> list[Tag]:
        abs_path = (self.repo_root / path).resolve()
        try:
            mtime = abs_path.stat().st_mtime
        except OSError as exc:
            log.warning("cannot stat %s: %s", path, exc)
            return []

        cached = self._cache.get(path)
        if cached is not None and cached["mtime"] == mtime:
            return cached["tags"]

        lang_name = self._lang_for(path)
        if lang_name is None or lang_name not in TAGS_QUERIES:
            self._cache[path] = {"mtime": mtime, "tags": []}
            return []

        try:
            source = abs_path.read_bytes()
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
            return []

        parser = get_parser(lang_name)
        language = get_language(lang_name)
        # tree-sitter is error-tolerant: broken code yields a partial tree
        # with ERROR nodes and does NOT raise.
        tree = parser.parse(source)
        self._parse_count += 1

        tags = self._extract_tags(language, tree, source, path)
        self._cache[path] = {"mtime": mtime, "tags": tags}
        return tags
```

### Task 1.6 — RepoMapper._extract_tags using the tags query

- [ ] Append `_extract_tags`. It runs the language query, walks captures, derives `kind` ('def'/'ref') and a kind-detail from the capture name, extracts the symbol name text, the 1-based line, and a one-line signature from the source line:

```python
    def _extract_tags(
        self, language, tree, source: bytes, path: str
    ) -> list[Tag]:
        lang_name = self._lang_for(path)
        query = language.query(TAGS_QUERIES[lang_name])
        tags: list[Tag] = []
        # captures() returns dict[capture_name, list[Node]] in py-tree-sitter 0.25+
        captures = query.captures(tree.root_node)
        for cap_name, nodes in captures.items():
            if cap_name.startswith("name.definition."):
                kind = "def"
                detail = cap_name.split(".")[-1]  # function|class|method
            elif cap_name.startswith("name.reference."):
                kind = "ref"
                detail = cap_name.split(".")[-1]
            else:
                continue  # skip @definition.* wrapper captures
            for node in nodes:
                name = source[node.start_byte:node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                line = node.start_point[0] + 1  # 0-based row -> 1-based line
                signature = self._signature_line(source, node) if kind == "def" else None
                tags.append(
                    Tag(
                        name=name,
                        kind=kind,
                        file=path,
                        line=line,
                        signature=signature,
                        parent=detail if detail != "function" else None,
                    )
                )
        return tags
```

> Note: `parent` here carries the kind-detail (class/method) for context per the spec record shape; `function` defs leave it null. Tests assert structure, not this exact convention.

### Task 1.7 — RepoMapper._signature_line helper

- [ ] Append the signature extractor: the trimmed text of the source line containing the definition name node:

```python
    @staticmethod
    def _signature_line(source: bytes, node) -> str:
        text = source.decode("utf-8", errors="replace")
        lines = text.splitlines()
        row = node.start_point[0]
        if 0 <= row < len(lines):
            return lines[row].strip()
        return ""
```

### Task 1.8 — RepoMapper._all_source_files helper

- [ ] Append a helper that walks `repo_root` for supported source files, returning repo-relative paths and skipping common noise directories:

```python
    _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                  "dist", "build", ".mypy_cache", ".pytest_cache"}

    def _all_source_files(self) -> list[str]:
        files: list[str] = []
        for root, dirs, names in os.walk(self.repo_root):
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS]
            for n in names:
                if Path(n).suffix in EXT_TO_LANG:
                    abs_p = Path(root) / n
                    rel = str(abs_p.relative_to(self.repo_root))
                    files.append(rel)
        return files
```

---

## Phase 2 — RepoMapper: graph + ranking (spec 6.2)

### Task 2.1 — RepoMapper.build_graph

- [ ] Append `build_graph`. For each reference to symbol S, add a directed edge `ref_file -> def_file` for each file that defines S. Edge weights accumulate on repeated references:

```python
    def build_graph(self, all_tags: list[Tag]) -> nx.DiGraph:
        # symbol name -> set of files that define it
        defs: dict[str, set[str]] = {}
        for t in all_tags:
            if t.kind == "def":
                defs.setdefault(t.name, set()).add(t.file)

        graph = nx.DiGraph()
        # ensure every file with any tag is a node, even if isolated
        for t in all_tags:
            graph.add_node(t.file)

        for t in all_tags:
            if t.kind != "ref":
                continue
            for def_file in defs.get(t.name, ()):
                if def_file == t.file:
                    continue  # ignore self-references within a file
                if graph.has_edge(t.file, def_file):
                    graph[t.file][def_file]["weight"] += 1.0
                else:
                    graph.add_edge(t.file, def_file, weight=1.0)
        return graph
```

### Task 2.2 — RepoMapper.rank with personalized PageRank

- [ ] Append `rank`. Build a personalization vector weighting `chat_files` ~50x other files, then run `nx.pagerank`. Handle the empty-graph case. Normalize chat_files to repo-relative for matching:

```python
    CHAT_FILE_BOOST = 50.0  # tune empirically; do not blindly copy Aider's constant

    def rank(self, graph: nx.DiGraph, chat_files: list[str]) -> dict[str, float]:
        if graph.number_of_nodes() == 0:
            return {}
        chat_set = {self._normalize(f) for f in chat_files}
        personalization = {
            node: (self.CHAT_FILE_BOOST if node in chat_set else 1.0)
            for node in graph.nodes()
        }
        try:
            return nx.pagerank(
                graph, personalization=personalization, weight="weight"
            )
        except nx.PowerIterationFailedConvergence:
            log.warning("pagerank failed to converge; using uniform ranks")
            n = graph.number_of_nodes()
            return {node: 1.0 / n for node in graph.nodes()}
```

### Task 2.3 — RepoMapper._normalize helper

- [ ] Append a path normalizer so absolute or `./`-prefixed chat_files match repo-relative graph nodes:

```python
    def _normalize(self, path: str) -> str:
        p = Path(path)
        if p.is_absolute():
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                return str(p)
        return str(Path(path))  # collapses "./x" -> "x"
```

---

## Phase 3 — RepoMapper: token-budgeted serialization (spec 6.2)

### Task 3.1 — Add a lazy Gemma tokenizer property

- [ ] Append a cached tokenizer property. CRITICAL: Gemma SentencePiece tokenizer, never tiktoken. Import is local so the module loads even if transformers is heavy:

```python
    _tokenizer = None  # class-level cache shared across instances

    @property
    def tokenizer(self):
        if RepoMapper._tokenizer is None:
            from transformers import AutoTokenizer
            # Gemma uses SentencePiece. NEVER use tiktoken here.
            RepoMapper._tokenizer = AutoTokenizer.from_pretrained(
                "google/gemma-4-9b-it"
            )
        return RepoMapper._tokenizer

    def _count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text))
```

### Task 3.2 — RepoMapper.get_repo_map

- [ ] Append the public `get_repo_map`. Parse all files, build graph, rank, sort definition tags by descending `ranks[tag.file]`, then emit JSONL until the token budget is exhausted, appending a truncation marker when symbols remain:

```python
    def get_repo_map(self, chat_files: list[str], max_tokens: int) -> str:
        all_tags: list[Tag] = []
        for path in self._all_source_files():
            all_tags.extend(self._parse_file(path))

        graph = self.build_graph(all_tags)
        ranks = self.rank(graph, chat_files)

        def_tags = [t for t in all_tags if t.kind == "def"]
        def_tags.sort(key=lambda t: ranks.get(t.file, 0.0), reverse=True)

        lines: list[str] = []
        used = 0
        emitted = 0
        for t in def_tags:
            record = {
                "name": t.name,
                "kind": t.parent or "function" if t.parent else t.parent or "function",
                "signature": t.signature,
                "parent": t.parent,
                "loc": f"{t.file}:{t.line}",
            }
            # canonical kind: method/class from detail, else function
            record["kind"] = t.parent if t.parent in ("class", "method") else "function"
            line = json.dumps(record, ensure_ascii=False)
            cost = self._count_tokens(line + "\n")
            if used + cost > max_tokens:
                break
            lines.append(line)
            used += cost
            emitted += 1

        omitted = len(def_tags) - emitted
        if omitted > 0:
            lines.append(f"// ... {omitted} symbols omitted")
        return "\n".join(lines)
```

> Self-check note for the implementer: simplify the `kind` assignment to the single canonical line below; the duplicated assignment above is intentionally shown so you replace it. Final form:
>
> ```python
> record = {
>     "name": t.name,
>     "kind": t.parent if t.parent in ("class", "method") else "function",
>     "signature": t.signature,
>     "parent": t.parent,
>     "loc": f"{t.file}:{t.line}",
> }
> line = json.dumps(record, ensure_ascii=False)
> ```

### Task 3.3 — Fix get_repo_map kind assignment to the clean single form

- [ ] Replace the record-building block inside `get_repo_map` with the clean version (removes the duplicated `kind` line):

```python
        for t in def_tags:
            record = {
                "name": t.name,
                "kind": t.parent if t.parent in ("class", "method") else "function",
                "signature": t.signature,
                "parent": t.parent,
                "loc": f"{t.file}:{t.line}",
            }
            line = json.dumps(record, ensure_ascii=False)
            cost = self._count_tokens(line + "\n")
            if used + cost > max_tokens:
                break
            lines.append(line)
            used += cost
            emitted += 1
```

### Task 3.4 — RepoMapper.get_symbols

- [ ] Append `get_symbols`: parse one file, return JSONL of its definition tags only:

```python
    def get_symbols(self, file: str) -> str:
        rel = self._normalize(file)
        tags = self._parse_file(rel)
        lines = []
        for t in tags:
            if t.kind != "def":
                continue
            record = {
                "name": t.name,
                "kind": t.parent if t.parent in ("class", "method") else "function",
                "signature": t.signature,
                "parent": t.parent,
                "loc": f"{t.file}:{t.line}",
            }
            lines.append(json.dumps(record, ensure_ascii=False))
        return "\n".join(lines)
```

---

## Phase 4 — MCP server entry point

### Task 4.1 — Create server.py with logging, imports, app

- [ ] Create `services/skills/ast-repo-map/server.py`. Logging is configured to stderr FIRST, before anything else can emit:

```python
"""MCP stdio server for the ast.repo-map skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
All logging is configured to sys.stderr below.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# CRITICAL: stderr only. Configure before importing/using anything that logs.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("ast.repo-map.server")

from repo_mapper import RepoMapper  # noqa: E402 (after logging is configured)

app: Server = Server("ast.repo-map")
mapper: RepoMapper | None = None
```

### Task 4.2 — Implement list_tools

- [ ] Append the `list_tools` handler exposing both tools with self-contained JSON Schemas:

```python
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_repo_map",
            description=(
                "Return a JSONL list of the most important symbols in the "
                "repository, ranked by personalized PageRank and bounded by a "
                "token budget."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files the agent is actively editing.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Hard cap on output tokens.",
                    },
                },
                "required": ["chat_files", "max_tokens"],
            },
        ),
        Tool(
            name="get_symbols",
            description="Return all symbols defined in a specific file as JSONL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Repo-relative path to a source file.",
                    }
                },
                "required": ["file"],
            },
        ),
    ]
```

### Task 4.3 — Implement call_tool

- [ ] Append the `call_tool` dispatcher. It lazily constructs the RepoMapper from `REPO_ROOT` env (default cwd), routes by tool name, and returns the JSONL string as TextContent. Errors are logged to stderr and returned as content rather than crashing the server:

```python
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global mapper
    if mapper is None:
        repo_root = os.getenv("REPO_ROOT", os.getcwd())
        mapper = RepoMapper(repo_root)
        log.info("RepoMapper initialized at %s", repo_root)

    try:
        if name == "get_repo_map":
            result = mapper.get_repo_map(
                chat_files=arguments["chat_files"],
                max_tokens=arguments["max_tokens"],
            )
        elif name == "get_symbols":
            result = mapper.get_symbols(file=arguments["file"])
        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=f"unknown tool: {name}")]
    except Exception as exc:  # never let one bad call crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=f"error: {exc!r}")]

    return [TextContent(type="text", text=result)]
```

### Task 4.4 — Implement main and entry guard

- [ ] Append the async `main` and the `__main__` guard:

```python
async def main() -> None:
    log.info("starting ast.repo-map MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Phase 5 — Tests (mocked, no real repos, no GPU)

### Task 5.1 — Create conftest.py with tokenizer + fixtures

- [ ] Create `tests/services/skills/ast-repo-map/conftest.py`. It puts the server dir on `sys.path` and stubs the Gemma tokenizer with a deterministic whitespace counter so tests need no network/model download:

```python
import sys
from pathlib import Path

import pytest

SERVER_DIR = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "ast-repo-map"
)
sys.path.insert(0, str(SERVER_DIR))


class _FakeTokenizer:
    """Deterministic stand-in for the Gemma tokenizer.

    One token per whitespace-separated chunk. Avoids downloading
    google/gemma-4-9b-it in CI while preserving budget semantics.
    """

    def encode(self, text: str) -> list[int]:
        return list(range(len(text.split())))


@pytest.fixture
def repo_mapper(tmp_path):
    import repo_mapper as rm
    rm.RepoMapper._tokenizer = _FakeTokenizer()  # bypass transformers load
    return rm


@pytest.fixture
def sample_repo(tmp_path):
    """A tiny multi-file Python repo with cross-file references."""
    (tmp_path / "util.py").write_text(
        "def helper():\n    return 1\n\n"
        "class Widget:\n    def build(self):\n        return helper()\n"
    )
    (tmp_path / "service.py").write_text(
        "from util import helper, Widget\n\n"
        "def run():\n    w = Widget()\n    return helper() + w.build()\n"
    )
    return tmp_path
```

### Task 5.2 — Test: Tags are extracted

- [ ] Create `tests/services/skills/ast-repo-map/test_repo_mapper.py` with the first test:

```python
import json

import pytest


@pytest.mark.mocked
def test_extracts_definition_tags(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    tags = mapper._parse_file("util.py")
    names = {t.name for t in tags if t.kind == "def"}
    assert "helper" in names
    assert "Widget" in names
    assert any(t.kind == "ref" for t in tags)
```

### Task 5.3 — Test: get_symbols returns only defs as JSONL

- [ ] Append:

```python
@pytest.mark.mocked
def test_get_symbols_returns_defs_jsonl(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    out = mapper.get_symbols("util.py")
    records = [json.loads(line) for line in out.splitlines() if line]
    names = {r["name"] for r in records}
    assert "helper" in names and "Widget" in names
    for r in records:
        assert set(r) == {"name", "kind", "signature", "parent", "loc"}
        assert r["loc"].startswith("util.py:")
```

### Task 5.4 — Test: PageRank boosts chat_files

- [ ] Append. Assert the chat file outranks (or ties at top) other files via the personalization vector:

```python
@pytest.mark.mocked
def test_pagerank_boosts_chat_files(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    all_tags = []
    for f in ("util.py", "service.py"):
        all_tags.extend(mapper._parse_file(f))
    graph = mapper.build_graph(all_tags)

    boosted = mapper.rank(graph, chat_files=["service.py"])
    neutral = mapper.rank(graph, chat_files=[])
    # boosting service.py raises its own rank relative to the unboosted run
    assert boosted["service.py"] > neutral["service.py"]
```

### Task 5.5 — Test: output respects token budget

- [ ] Append. With the fake tokenizer (1 token/word), a tiny budget must produce fewer/equal tokens than the cap:

```python
@pytest.mark.mocked
def test_output_within_token_budget(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    out = mapper.get_repo_map(chat_files=["service.py"], max_tokens=10)
    data_lines = [l for l in out.splitlines() if not l.startswith("// ...")]
    total = sum(len(l.split()) + 1 for l in data_lines)  # +1 for newline word? see note
    # budget is a HARD cap: emitted data tokens never exceed max_tokens
    emitted_cost = sum(mapper._count_tokens(l + "\n") for l in data_lines)
    assert emitted_cost <= 10
```

### Task 5.6 — Test: truncation marker appears

- [ ] Append. A budget too small to fit all symbols must produce the `// ... N symbols omitted` marker:

```python
@pytest.mark.mocked
def test_truncation_marker_appears(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    out = mapper.get_repo_map(chat_files=["service.py"], max_tokens=1)
    assert any(l.startswith("// ...") and "symbols omitted" in l
               for l in out.splitlines())
```

### Task 5.7 — Test: mtime cache avoids re-parsing

- [ ] Append. Two identical calls parse once; touching the file (new mtime) forces a re-parse:

```python
@pytest.mark.mocked
def test_mtime_cache_parses_once(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    mapper._parse_file("util.py")
    first = mapper._parse_count
    mapper._parse_file("util.py")  # identical mtime -> cache hit
    assert mapper._parse_count == first

    # bump mtime -> cache miss -> one more parse
    import os, time
    p = sample_repo / "util.py"
    os.utime(p, (time.time() + 10, time.time() + 10))
    mapper._parse_file("util.py")
    assert mapper._parse_count == first + 1
```

### Task 5.8 — Test: broken code does not raise (error tolerance)

- [ ] Append. A syntactically broken file must still parse and return whatever tags are recoverable, without raising:

```python
@pytest.mark.mocked
def test_broken_code_is_error_tolerant(repo_mapper, tmp_path):
    (tmp_path / "broken.py").write_text(
        "def good():\n    return 1\n\ndef bad(  :\n    oops\n"
    )
    mapper = repo_mapper.RepoMapper(str(tmp_path))
    tags = mapper._parse_file("broken.py")  # must not raise
    names = {t.name for t in tags if t.kind == "def"}
    assert "good" in names
```

### Task 5.9 — Run the suite

- [ ] Run only the mocked tests and confirm green:

```bash
cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/skills/ast-repo-map -m mocked -v
```

---

## Phase 6 — Verification checklist

- [ ] Grep the server dir for stdout violations — must return nothing:

```bash
grep -rnE '(^|[^.])\bprint\(' /Users/zachstallbohm/Work/gemma/services/skills/ast-repo-map
```

- [ ] Grep for forbidden tiktoken — must return nothing:

```bash
grep -rn 'tiktoken' /Users/zachstallbohm/Work/gemma/services/skills/ast-repo-map
```

- [ ] Confirm tokenizer model id is the Gemma SentencePiece tokenizer:

```bash
grep -rn 'google/gemma-4-9b-it' /Users/zachstallbohm/Work/gemma/services/skills/ast-repo-map/repo_mapper.py
```

- [ ] Smoke-test the server starts and speaks JSON-RPC over stdio (initialize handshake). It should print nothing to stdout except framed JSON:

```bash
cd /Users/zachstallbohm/Work/gemma/services/skills/ast-repo-map && \
REPO_ROOT="$PWD" python - <<'PY'
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=["server.py"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("TOOLS:", [t.name for t in tools.tools], file=sys.stderr)

asyncio.run(main())
PY
```

---

## Spec coverage map (self-review)

| Spec requirement (6.1 / 6.2) | Task |
|---|---|
| py-tree-sitter + tree-sitter-language-pack | 1.1, 1.5 |
| mtime-keyed cache `dict[path -> {mtime, tree, tags}]` | 1.3, 1.5, 5.7 |
| Re-parse only changed files; cache hit otherwise | 1.5, 5.7 |
| tags.scm S-expression queries for defs + refs | 1.2, 1.6 |
| Error-tolerant parsing (ERROR nodes, no raise) | 1.5, 5.8 |
| stderr-only logging, never stdout | 1.1, 4.1, Phase 6 |
| Extract Tag (def + ref) objects | 1.1, 1.6 |
| networkx directed file-level graph: ref_file -> def_file | 2.1 |
| Personalized PageRank, chat_files ~50x | 2.2, 5.4 |
| Sort defs by descending ranks[tag.file] | 3.2 |
| JSONL until max_tokens exhausted | 3.2/3.3, 5.5 |
| Record shape {name,kind,signature,parent,loc} | 3.2/3.3, 5.3 |
| `// ... N symbols omitted` truncation marker | 3.2, 5.6 |
| Gemma tokenizer for counting, never tiktoken | 3.1, Phase 6 |
| Hard token cap (not a guideline) | 3.2, 5.5 |
| Two tools exposed via MCP | 4.2 |
| stdio transport child process | 4.1, 4.4, 6 |
| SKILL.md frontmatter (spec 3.3) | 0.3 |

---

## Notes for the implementer

- `py-tree-sitter>=0.25` returns `query.captures()` as `dict[str, list[Node]]`. If the installed version returns the older `list[(Node, str)]` form, adapt `_extract_tags` accordingly (iterate tuples instead of dict items) — this is the one API surface most likely to differ across patch versions.
- The fake tokenizer in tests counts whitespace chunks; the real Gemma tokenizer counts more tokens per line, so production budgets are conservative relative to tests. That is the safe direction (under-fill, never overflow).
- `REPO_ROOT` is read from the environment by `call_tool`; the SkillRegistry sets it when spawning the subprocess. Default is the process cwd.
