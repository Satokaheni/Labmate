# repo-fault-localize MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the repo-fault-localize Python MCP server implementing Agentless-style hierarchical fault localization: BM25 keyword search + LLM reranking at file level, then AST + LLM at function level, then edit-site suggestion.

**Architecture:** FaultLocalizer uses BM25Index (rank-bm25) for initial keyword matching, then calls Gemma 4 via litellm to rerank candidates with code context. locate_symbols uses tree-sitter to extract function signatures from the located file. Three tools compose into the Agentless three-stage pipeline. All logging to stderr.

**Tech Stack:** Python 3.11+, `mcp` SDK, `rank-bm25`, `litellm`, `py-tree-sitter>=0.25`, `tree-sitter-language-pack`, `pydantic>=2`, `pytest`

---

## Background — what we are reproducing

This skill implements the localization stage of **Agentless** (Xia et al., 40.67% SWE-bench Lite) and **ARISE** (arXiv:2605.03117). The core idea is *hierarchical narrowing*: instead of asking an LLM to reason over the whole repo at once, narrow the search in three staged passes, each consuming the output of the prior one.

```
issue text
   │
   ▼  Stage 1: locate_files   (BM25 candidates → LLM rerank with snippets)
top-k files
   │
   ▼  Stage 2: locate_symbols (tree-sitter signatures → LLM picks suspect funcs/classes)
suspect symbols
   │
   ▼  Stage 3: suggest_edit_sites (LLM picks line ranges within the symbols)
edit hunks (file, start_line, end_line, reason)
```

The three tools are independent MCP entry points so a worker (or a higher-level orchestrator) can stop at any stage, inspect, and re-drive the next stage with corrected inputs.

---

## Critical constraints (apply to every task)

- **stdout is sacred.** All logging uses `logging` configured with `stream=sys.stderr`. NEVER `print()`. stdout carries JSON-RPC 2.0 framing; any stray byte corrupts the stream silently and produces misleading `Parse error` symptoms downstream.
- **Never tiktoken.** If any token counting is required, use `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")`. Gemma uses SentencePiece; tiktoken counts are wrong.
- **LLM calls go through `GEMMA_BASE`.** `os.getenv("GEMMA_BASE", "http://localhost:8000/v1")`, model `os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")`, via `litellm.completion(model=f"openai/{GEMMA_MODEL}", api_base=GEMMA_BASE, api_key="not-needed", temperature=0.0)`. Centralize the call in one `_call_gemma` helper so tests can monkeypatch it.
- **Reuse the existing AST stack.** Grammars come from `tree-sitter-language-pack` (already used by `ast-repo-map` and `repo-graph`). Do not vendor or compile grammars by hand.
- **`repo-graph` is a runtime dependency.** This skill declares `requires: [repo-graph]` and uses its cross-file reference edges to expand file candidates (a file that references a BM25 hit is itself a candidate). Degrade gracefully: if the graph is unavailable, fall back to BM25-only candidates. Do NOT re-implement graph extraction here.
- **All output is JSONL.** Every tool returns a newline-separated list of JSON objects (one object per line). An empty result is the empty string.
- **LLM output is non-deterministic — parse defensively.** Strip code fences, locate the first JSON array, tolerate trailing prose. Never assume the model returned clean JSON.
- The server is a **child process** spawned by the SkillRegistry over stdio. No TTY, no banners; all paths are resolved relative to `repo_path`.

---

## Output shapes (shared vocabulary for every task)

Each tool emits JSONL. These are the exact field sets — do not introduce variants.

```python
# locate_files line
{"file": "src/foo.py", "score": 0.91, "reason": "defines parse_config, named in issue"}

# locate_symbols line
{"file": "src/foo.py", "symbol": "parse_config", "kind": "function",
 "start_line": 42, "end_line": 88, "reason": "handles the config key from the traceback"}

# suggest_edit_sites line
{"file": "src/foo.py", "start_line": 55, "end_line": 60,
 "reason": "missing None-check before .get() — matches AttributeError in issue"}
```

`score` is a float in [0,1] (LLM rerank confidence; BM25-only fallback normalizes raw BM25 scores to this range). `kind` is one of `function` | `class` | `method`.

---

## Phase 0 — Scaffolding

### Task 0.1 — Create directory structure

- [ ] Create the skill server and test directories.

```bash
mkdir -p /Users/zachstallbohm/Work/gemma/services/skills/repo-fault-localize
mkdir -p /Users/zachstallbohm/Work/gemma/tests/services/skills/repo-fault-localize
```

### Task 0.2 — Write requirements.txt

- [ ] Create `services/skills/repo-fault-localize/requirements.txt`:

```text
mcp>=1.0.0
rank-bm25>=0.2.2
litellm>=1.40
py-tree-sitter>=0.25
tree-sitter-language-pack>=0.7.2
pydantic>=2
```

> `transformers` is only added if token counting is later needed. `pytest` is a dev dependency installed in the test phase.

### Task 0.3 — Write SKILL.md

- [ ] Create `services/skills/repo-fault-localize/SKILL.md` with the frontmatter below, then a model-agnostic markdown body (no absolute paths):

```markdown
---
name: repo-fault-localize
description: >
  Hierarchical fault localization for bug fixing: narrows from repository → file →
  function → edit location using Agentless-style BM25+LLM ranking. Use as the first
  step in any bug-fix workflow to identify exactly where to make changes before editing.
  Three-stage pipeline: locate_files → locate_symbols → suggest_edit_sites.
trigger: "Use at the start of a bug-fix task to identify which files and functions to edit"
tools:
  - fault_localize.locate_files
  - fault_localize.locate_symbols
  - fault_localize.suggest_edit_sites
version: "0.1.0"
license: MIT
requires: [repo-graph]
---

# Repo Fault Localize Skill

You have access to the `fault_localize` MCP server which performs Agentless-style
hierarchical fault localization: given a bug description it narrows the search from
the whole repository down to specific edit sites in three staged passes.

## When to Use

- At the **start of any bug-fix task**, before opening or editing files.
- When a traceback or issue names a behavior but not a location.
- To produce a short, ranked list of edit candidates for a downstream patcher.

## Pipeline

1. `locate_files(issue, repo_path)` → top-k suspect files (BM25 + LLM rerank).
2. For the top file(s): `locate_symbols(issue, file, repo_path)` → suspect
   functions/classes with line ranges.
3. `suggest_edit_sites(issue, file, symbols)` → precise line-range edit hunks.

Stop at any stage to inspect, then drive the next stage with the chosen inputs.

## Available Tools

### `fault_localize.locate_files(issue, repo_path, top_k=5)`
Return the top-k files most likely to need edits. JSONL with `file`, `score`, `reason`.

### `fault_localize.locate_symbols(issue, file, repo_path)`
Within a located file, return the functions/classes most likely to contain the bug.
JSONL with `file`, `symbol`, `kind`, `start_line`, `end_line`, `reason`.

### `fault_localize.suggest_edit_sites(issue, file, symbols)`
Return specific line ranges (edit hunks) within the given symbols. JSONL with
`file`, `start_line`, `end_line`, `reason`.

## Workflow

Always run the stages in order. Feed the chosen file from stage 1 into stage 2,
and the chosen symbol names from stage 2 into stage 3.
```

---

## Phase 1 — BM25Index (keyword search over code)

### Task 1.1 — Imports, logger, constants

- [ ] Create `services/skills/repo-fault-localize/bm25_index.py` with the import block, stderr logger, and file-discovery constants.

```python
import logging
import os
import re
import sys

from rank_bm25 import BM25Okapi

# All diagnostics to stderr — stdout is reserved for JSON-RPC.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("fault-localize.bm25")

_CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java"}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".labmate", "dist",
              "build", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
_MAX_BYTES = 1_000_000  # skip files larger than ~1MB (generated/minified)

# split identifiers: camelCase, snake_case, dotted paths, punctuation
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _tokenize(text: str) -> list[str]:
    """Lowercased identifier tokens, with camelCase split into sub-words."""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", tok)
        out.append(tok.lower())
        out.extend(p.lower() for p in parts if p.lower() != tok.lower())
    return out
```

> camelCase splitting matters: an issue saying "parse config" should match `parseConfig`. Adding both the whole token and its sub-words keeps exact and split matches.

### Task 1.2 — `BM25Index.__init__` and file discovery

- [ ] Add the class with `__init__` storing repo path and an empty corpus, plus a `_iter_code_files` walker that skips VCS/build dirs and oversized files.

```python
class BM25Index:
    def __init__(self, repo_path: str):
        self._repo_path = os.path.abspath(repo_path)
        self._files: list[str] = []          # repo-relative paths, index-aligned with corpus
        self._corpus: list[list[str]] = []   # tokenized doc per file
        self._bm25: BM25Okapi | None = None

    def _iter_code_files(self):
        for dirpath, dirnames, filenames in os.walk(self._repo_path):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1] in _CODE_EXTS:
                    yield os.path.join(dirpath, fn)

    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self._repo_path)
```

### Task 1.3 — `BM25Index.build`

- [ ] Tokenize every code file (path components are tokenized too, since file/dir names are strong signals) and fit the BM25 model. Never raise on a single unreadable file.

```python
    def build(self) -> None:
        self._files.clear()
        self._corpus.clear()
        for path in self._iter_code_files():
            try:
                if os.path.getsize(path) > _MAX_BYTES:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError as exc:
                log.warning("skip unreadable file %s: %s", path, exc)
                continue
            rel = self._rel(path)
            # weight the path: file/dir names are high-signal for localization
            tokens = _tokenize(rel.replace(os.sep, " ")) * 3 + _tokenize(text)
            self._files.append(rel)
            self._corpus.append(tokens)
        if self._corpus:
            self._bm25 = BM25Okapi(self._corpus)
        log.info("BM25 index built over %d files", len(self._files))
```

### Task 1.4 — `BM25Index.search`

- [ ] Tokenize the query, score against the corpus, return the top-k `(file_path, score)` pairs sorted descending. Return empty if the index is empty.

```python
    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        if self._bm25 is None or not self._files:
            return []
        q = _tokenize(query)
        scores = self._bm25.get_scores(q)
        ranked = sorted(zip(self._files, scores), key=lambda p: p[1], reverse=True)
        return [(f, float(s)) for f, s in ranked[:top_k] if s > 0.0]
```

---

## Phase 2 — FaultLocalizer (hierarchical localization)

### Task 2.1 — Imports, logger, LLM config, `__init__`

- [ ] Create `services/skills/repo-fault-localize/localizer.py` with the import block, stderr logger, Gemma config, and the class constructor wiring in `BM25Index`.

```python
import json
import logging
import os
import sys

import litellm
from tree_sitter_language_pack import get_parser

from bm25_index import BM25Index

# All diagnostics to stderr — stdout is reserved for JSON-RPC.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("fault-localize")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")

_LANG_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".rs": "rust",
}


class FaultLocalizer:
    def __init__(self, repo_path: str):
        self._repo_path = os.path.abspath(repo_path)
        self._bm25 = BM25Index(repo_path)
        self._bm25_built = False

    def _ensure_index(self) -> None:
        if not self._bm25_built:
            self._bm25.build()
            self._bm25_built = True

    def _abs(self, rel_or_abs: str) -> str:
        if os.path.isabs(rel_or_abs):
            return rel_or_abs
        return os.path.join(self._repo_path, rel_or_abs)
```

### Task 2.2 — Centralized Gemma call + tolerant JSON parser

- [ ] Add `_call_gemma` (the single LLM entry point — tests monkeypatch this) and `_parse_json_array` (defensive parse). NEVER `print()`.

```python
    def _call_gemma(self, prompt: str) -> str:
        resp = litellm.completion(
            model=f"openai/{GEMMA_MODEL}",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        return resp["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_json_array(raw: str) -> list[dict]:
        s = raw.strip()
        if s.startswith("```"):
            s = s.split("```", 2)[1]
            if s.startswith("json"):
                s = s[4:]
        start, end = s.find("["), s.rfind("]")
        if start == -1 or end == -1:
            log.warning("no JSON array in LLM output")
            return []
        try:
            parsed = json.loads(s[start:end + 1])
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            log.warning("failed to parse LLM JSON array")
            return []
```

### Task 2.3 — File-snippet reader (context for the reranker)

- [ ] Add `_snippet`, which returns the first N lines of a file for use as LLM rerank context. Bounded so prompts stay small.

```python
    def _snippet(self, rel_path: str, max_lines: int = 40) -> str:
        try:
            with open(self._abs(rel_path), "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return ""
        head = lines[:max_lines]
        return "\n".join(head)
```

### Task 2.4 — `_rank_files`: BM25 candidates → LLM rerank

- [ ] Add `_rank_files`. It takes BM25 candidate paths, builds a prompt with a snippet per candidate, asks Gemma to rank and justify, and returns dicts with `file`, `score`, `reason`. Fall back to normalized BM25 scores if the LLM returns nothing.

```python
    _RANK_PROMPT = """You are a fault-localization expert. Given a bug report and a list \
of candidate files (with the top of each file shown), rank the files by how likely each \
is to contain the code that must be edited to fix the bug.

Return ONLY a JSON array, most-likely first, each element:
{{"file": "<path>", "score": <0..1 confidence>, "reason": "<one sentence>"}}
Only include files you believe are relevant. Do not invent paths.

BUG REPORT:
{issue}

CANDIDATE FILES:
{candidates}
"""

    def _rank_files(self, issue: str, candidates: list[tuple[str, float]]) -> list[dict]:
        if not candidates:
            return []
        blocks = []
        for path, _score in candidates:
            blocks.append(f"### {path}\n```\n{self._snippet(path)}\n```")
        prompt = self._RANK_PROMPT.format(issue=issue, candidates="\n\n".join(blocks))
        ranked = self._parse_json_array(self._call_gemma(prompt))

        valid_paths = {p for p, _ in candidates}
        out: list[dict] = []
        for item in ranked:
            f = item.get("file")
            if f not in valid_paths:
                continue
            try:
                score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            out.append({"file": f, "score": round(max(0.0, min(1.0, score)), 4),
                        "reason": str(item.get("reason", ""))})
        if out:
            return out
        # Fallback: normalized BM25 scores.
        log.warning("LLM rerank empty; falling back to BM25 order")
        top = candidates[0][1] or 1.0
        return [{"file": p, "score": round(s / top, 4), "reason": "BM25 keyword match"}
                for p, s in candidates]
```

### Task 2.5 — `locate_files` (Stage 1 public method)

- [ ] Add `locate_files`. Build the index, take a wider BM25 candidate set (e.g. `top_k * 4`, min 12), optionally expand via `repo-graph` references (best-effort), then LLM-rerank to `top_k`.

```python
    def locate_files(self, issue: str, top_k: int = 5) -> list[dict]:
        self._ensure_index()
        n_candidates = max(12, top_k * 4)
        candidates = self._bm25.search(issue, top_k=n_candidates)
        candidates = self._expand_with_graph(candidates)
        ranked = self._rank_files(issue, candidates)
        return ranked[:top_k]
```

### Task 2.6 — `_expand_with_graph` (best-effort repo-graph use)

- [ ] Add `_expand_with_graph`. Import the sibling `repo-graph` skill's builder if importable; add files that reference any BM25 hit as additional candidates (with a small synthetic score). Any failure logs to stderr and returns the input unchanged — this is the graceful-degradation path required by the constraints.

```python
    def _expand_with_graph(self, candidates: list[tuple[str, float]]) -> list[tuple[str, float]]:
        if not candidates:
            return candidates
        try:
            import importlib.util
            graph_dir = os.path.join(os.path.dirname(__file__), "..", "repo-graph")
            spec = importlib.util.spec_from_file_location(
                "_rg_builder", os.path.join(graph_dir, "graph_builder.py"))
            if spec is None or spec.loader is None:
                return candidates
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            builder = mod.RepoGraphBuilder(self._repo_path)
            edges = builder.build()
        except Exception as exc:  # noqa: BLE001 - graph is optional; never break stage 1
            log.warning("repo-graph expansion unavailable: %s", exc)
            return candidates

        hit_files = {p for p, _ in candidates}
        seen = dict(candidates)
        floor = min(s for _, s in candidates) if candidates else 0.0
        for e in edges:
            if e.dst_file in hit_files and e.src_file not in seen:
                seen[e.src_file] = floor * 0.5  # weaker synthetic score
        return sorted(seen.items(), key=lambda p: p[1], reverse=True)
```

### Task 2.7 — `_extract_symbols` (tree-sitter signatures)

- [ ] Add `_extract_symbols`. Parse the file with tree-sitter, collect function/class/method definitions with their name, kind, and 1-based start/end lines. Error-tolerant: never raise.

```python
    _DEF_KINDS = {
        "function_definition": "function", "function_declaration": "function",
        "function_item": "function", "method_definition": "method",
        "class_definition": "class", "class_declaration": "class",
        "struct_item": "class",
    }

    def _extract_symbols(self, rel_path: str) -> list[dict]:
        ext = os.path.splitext(rel_path)[1]
        lang = _LANG_BY_EXT.get(ext)
        if lang is None:
            return []
        try:
            with open(self._abs(rel_path), "rb") as fh:
                src = fh.read()
            tree = get_parser(lang).parse(src)
        except Exception as exc:  # noqa: BLE001 - never crash the localizer
            log.warning("parse failed for %s: %s", rel_path, exc)
            return []

        out: list[dict] = []

        def walk(node):
            kind = self._DEF_KINDS.get(node.type)
            if kind is not None:
                name = node.child_by_field_name("name")
                if name is not None:
                    out.append({
                        "file": rel_path,
                        "symbol": src[name.start_byte:name.end_byte].decode("utf-8", "replace"),
                        "kind": kind,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    })
            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return out
```

### Task 2.8 — `locate_symbols` (Stage 2 public method)

- [ ] Add `locate_symbols`. Extract all symbols from the file, present them to Gemma with the issue, and have it select and justify the suspect ones. Attach the true line ranges from the AST (never trust LLM line numbers here). Fall back to all symbols if the LLM returns nothing.

```python
    _SYMBOL_PROMPT = """You are a fault-localization expert. Given a bug report and the \
list of functions/classes defined in a file, select the ones most likely to contain the \
bug that must be fixed.

Return ONLY a JSON array, most-likely first, each element:
{{"symbol": "<name>", "reason": "<one sentence>"}}
Only include symbols from the provided list.

BUG REPORT:
{issue}

FILE: {file}
SYMBOLS:
{symbols}
"""

    def locate_symbols(self, issue: str, file: str) -> list[dict]:
        symbols = self._extract_symbols(file)
        if not symbols:
            return []
        by_name = {s["symbol"]: s for s in symbols}
        listing = "\n".join(
            f"- {s['symbol']} ({s['kind']}, lines {s['start_line']}-{s['end_line']})"
            for s in symbols)
        prompt = self._SYMBOL_PROMPT.format(issue=issue, file=file, symbols=listing)
        picked = self._parse_json_array(self._call_gemma(prompt))

        out: list[dict] = []
        for item in picked:
            meta = by_name.get(item.get("symbol"))
            if meta is None:
                continue
            out.append({**meta, "reason": str(item.get("reason", ""))})
        if out:
            return out
        log.warning("LLM symbol pick empty; returning all symbols")
        return [{**s, "reason": "candidate (no LLM filtering)"} for s in symbols]
```

### Task 2.9 — `suggest_edit_sites` (Stage 3 public method)

- [ ] Add `suggest_edit_sites`. For the given symbols, read their source bodies (using the AST ranges), show the numbered lines to Gemma, and ask for precise edit hunks. Clamp returned line ranges to each symbol's actual bounds.

```python
    _EDIT_PROMPT = """You are a fault-localization expert. Given a bug report and the \
source of suspect functions/classes (with line numbers), identify the specific line \
ranges that must be edited to fix the bug.

Return ONLY a JSON array, each element:
{{"file": "<path>", "start_line": <int>, "end_line": <int>, "reason": "<one sentence>"}}
Use the line numbers shown. Keep ranges tight.

BUG REPORT:
{issue}

SOURCE:
{source}
"""

    def suggest_edit_sites(self, issue: str, file: str, symbols: list[str]) -> list[dict]:
        all_syms = {s["symbol"]: s for s in self._extract_symbols(file)}
        wanted = [all_syms[name] for name in symbols if name in all_syms]
        if not wanted:
            return []
        try:
            with open(self._abs(file), "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            log.warning("cannot read %s: %s", file, exc)
            return []

        blocks = []
        bounds: dict[str, tuple[int, int]] = {}
        for s in wanted:
            lo, hi = s["start_line"], s["end_line"]
            bounds[s["symbol"]] = (lo, hi)
            numbered = "\n".join(f"{i}: {lines[i - 1]}"
                                 for i in range(lo, min(hi, len(lines)) + 1))
            blocks.append(f"### {s['symbol']} ({file})\n{numbered}")
        prompt = self._EDIT_PROMPT.format(issue=issue, source="\n\n".join(blocks))
        hunks = self._parse_json_array(self._call_gemma(prompt))

        lo_all = min(b[0] for b in bounds.values())
        hi_all = max(b[1] for b in bounds.values())
        out: list[dict] = []
        for h in hunks:
            try:
                start = int(h.get("start_line"))
                end = int(h.get("end_line"))
            except (TypeError, ValueError):
                continue
            start = max(lo_all, min(start, hi_all))
            end = max(start, min(end, hi_all))
            out.append({"file": file, "start_line": start, "end_line": end,
                        "reason": str(h.get("reason", ""))})
        return out
```

---

## Phase 3 — MCP server entry point

### Task 3.1 — Server scaffold, logger, helpers

- [ ] Create `services/skills/repo-fault-localize/server.py`. Wire stdio transport; logging to stderr only; JSONL helper; per-repo `FaultLocalizer` cache so the BM25 index is reused across calls in one session.

```python
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from localizer import FaultLocalizer

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("fault-localize")

app = Server("repo-fault-localize")

_LOCALIZERS: dict[str, FaultLocalizer] = {}


def _get_localizer(repo_path: str) -> FaultLocalizer:
    loc = _LOCALIZERS.get(repo_path)
    if loc is None:
        loc = FaultLocalizer(repo_path)
        _LOCALIZERS[repo_path] = loc
    return loc


def _jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in rows)
```

### Task 3.2 — Declare the three tools (`list_tools`)

- [ ] Register the three tools with JSON Schema. Names must match the SKILL.md frontmatter exactly.

```python
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="fault_localize.locate_files",
             description="Top-k files most likely to need edits for a bug. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"issue": {"type": "string"},
                                         "repo_path": {"type": "string"},
                                         "top_k": {"type": "integer", "default": 5}},
                          "required": ["issue", "repo_path"]}),
        Tool(name="fault_localize.locate_symbols",
             description="Functions/classes in a file most likely to contain the bug. JSONL.",
             inputSchema={"type": "object",
                          "properties": {"issue": {"type": "string"},
                                         "file": {"type": "string"},
                                         "repo_path": {"type": "string"}},
                          "required": ["issue", "file", "repo_path"]}),
        Tool(name="fault_localize.suggest_edit_sites",
             description="Precise edit line-ranges within given symbols. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"issue": {"type": "string"},
                                         "file": {"type": "string"},
                                         "repo_path": {"type": "string"},
                                         "symbols": {"type": "array",
                                                     "items": {"type": "string"}}},
                          "required": ["issue", "file", "symbols", "repo_path"]}),
    ]
```

> Note: `suggest_edit_sites` takes `repo_path` in the schema even though the public method signature is `(issue, file, symbols)` — the server uses it to resolve the cached localizer and absolute paths. Keep the method signature as specified; pass `repo_path` only to `_get_localizer`.

### Task 3.3 — Implement `call_tool` dispatch

- [ ] One dispatcher routes the three tools to the cached localizer and returns JSONL `TextContent`. Unknown tools and errors return a JSON error object (still valid on stdout).

```python
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        repo_path = arguments["repo_path"]
        loc = _get_localizer(repo_path)
        if name == "fault_localize.locate_files":
            rows = loc.locate_files(arguments["issue"],
                                    int(arguments.get("top_k", 5)))
        elif name == "fault_localize.locate_symbols":
            rows = loc.locate_symbols(arguments["issue"], arguments["file"])
        elif name == "fault_localize.suggest_edit_sites":
            rows = loc.suggest_edit_sites(arguments["issue"], arguments["file"],
                                          list(arguments["symbols"]))
        else:
            return [TextContent(type="text",
                                text=json.dumps({"error": f"unknown tool {name}"}))]
    except Exception as exc:  # noqa: BLE001 - surface as JSON, never crash the stream
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
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

### Task 4.1 — `conftest.py`: importability + sample buggy repo + Gemma mock

- [ ] Create `tests/services/skills/repo-fault-localize/conftest.py`. It makes the skill modules importable, writes a small Python repo with a known bug, and provides a `patch_gemma` fixture that monkeypatches `FaultLocalizer._call_gemma`.

```python
import sys
from pathlib import Path

import pytest

# make the skill modules importable
sys.path.insert(0, str(Path(__file__).resolve().parents[4]
                       / "services" / "skills" / "repo-fault-localize"))


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "config.py").write_text(
        "def parse_config(data):\n"
        "    # BUG: no None check before .get()\n"
        "    return data.get('key')\n"
        "\n"
        "def unrelated_helper():\n"
        "    return 42\n"
    )
    (tmp_path / "utils.py").write_text(
        "def format_path(p):\n"
        "    return str(p)\n"
    )
    return tmp_path


@pytest.fixture
def patch_gemma(monkeypatch):
    """Patch the single LLM entry point. Pass a dict mapping a prompt-substring
    to the canned JSON-array response; first match wins."""
    import localizer

    def install(responses: dict[str, str]):
        def fake(self, prompt: str) -> str:
            for needle, resp in responses.items():
                if needle in prompt:
                    return resp
            return "[]"
        monkeypatch.setattr(localizer.FaultLocalizer, "_call_gemma", fake)

    return install
```

### Task 4.2 — `test_bm25_index.py`: search ranks the keyword file first

- [ ] Verify `BM25Index.search` returns the file containing the query keyword with the highest score.

```python
import pytest
from bm25_index import BM25Index


@pytest.mark.mocked
def test_bm25_ranks_keyword_file_first(sample_repo):
    idx = BM25Index(str(sample_repo))
    idx.build()
    results = idx.search("parse_config key", top_k=5)
    assert results, "expected at least one hit"
    assert results[0][0] == "config.py"
    assert results[0][1] > 0.0
```

### Task 4.3 — `test_bm25_index.py`: empty index returns empty

- [ ] Verify searching an unbuilt / empty index returns `[]` rather than raising.

```python
@pytest.mark.mocked
def test_bm25_empty_index(tmp_path):
    idx = BM25Index(str(tmp_path))
    idx.build()  # no code files
    assert idx.search("anything") == []
```

### Task 4.4 — `test_localizer.py`: `locate_files` returns JSONL shape

- [ ] Verify `locate_files` returns dicts with `file`, `score`, `reason`, ranked by the mocked LLM.

```python
import pytest
from localizer import FaultLocalizer


@pytest.mark.mocked
def test_locate_files_shape(sample_repo, patch_gemma):
    patch_gemma({"rank the files": (
        '[{"file": "config.py", "score": 0.95, '
        '"reason": "defines parse_config named in the bug"}]'
    )})
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.locate_files("parse_config crashes on None data", top_k=3)
    assert rows
    assert rows[0]["file"] == "config.py"
    assert {"file", "score", "reason"} <= set(rows[0].keys())
    assert 0.0 <= rows[0]["score"] <= 1.0
```

### Task 4.5 — `test_localizer.py`: `locate_files` falls back to BM25 on empty LLM

- [ ] Verify that when the LLM returns `[]`, `locate_files` still returns BM25-ranked candidates with normalized scores.

```python
@pytest.mark.mocked
def test_locate_files_bm25_fallback(sample_repo, patch_gemma):
    patch_gemma({})  # all prompts -> "[]"
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.locate_files("parse_config key", top_k=3)
    assert rows
    assert any(r["file"] == "config.py" for r in rows)
    assert all(0.0 <= r["score"] <= 1.0 for r in rows)
```

### Task 4.6 — `test_localizer.py`: `locate_symbols` returns file symbols

- [ ] Verify `locate_symbols` returns functions/classes from the specified file with AST line ranges, filtered by the mocked LLM pick.

```python
@pytest.mark.mocked
def test_locate_symbols(sample_repo, patch_gemma):
    patch_gemma({"select the ones most likely":
                 '[{"symbol": "parse_config", "reason": "uses .get on possibly-None"}]'})
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.locate_symbols("parse_config crashes", "config.py")
    assert rows
    r = rows[0]
    assert r["symbol"] == "parse_config"
    assert r["file"] == "config.py"
    assert r["kind"] == "function"
    assert r["start_line"] >= 1 and r["end_line"] >= r["start_line"]
    assert {"file", "symbol", "kind", "start_line", "end_line", "reason"} <= set(r.keys())
```

### Task 4.7 — `test_localizer.py`: `locate_symbols` falls back to all symbols

- [ ] Verify an empty LLM pick yields all extracted symbols (so the pipeline never dead-ends).

```python
@pytest.mark.mocked
def test_locate_symbols_fallback(sample_repo, patch_gemma):
    patch_gemma({})  # -> "[]"
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.locate_symbols("anything", "config.py")
    names = {r["symbol"] for r in rows}
    assert {"parse_config", "unrelated_helper"} <= names
```

### Task 4.8 — `test_localizer.py`: `suggest_edit_sites` returns clamped line ranges

- [ ] Verify `suggest_edit_sites` returns `file`/`start_line`/`end_line`/`reason`, and that out-of-bounds line numbers from the LLM are clamped into the symbol's actual range.

```python
@pytest.mark.mocked
def test_suggest_edit_sites_clamped(sample_repo, patch_gemma):
    # LLM returns a wildly out-of-range end_line; must be clamped to file bounds.
    patch_gemma({"identify the specific line ranges":
                 '[{"file": "config.py", "start_line": 3, "end_line": 9999, '
                 '"reason": "add a None check before .get"}]'})
    loc = FaultLocalizer(str(sample_repo))
    rows = loc.suggest_edit_sites("None crash", "config.py", ["parse_config"])
    assert rows
    r = rows[0]
    assert {"file", "start_line", "end_line", "reason"} <= set(r.keys())
    assert r["start_line"] >= 1
    assert r["end_line"] <= 3  # parse_config spans lines 1-3 in the fixture
    assert r["end_line"] >= r["start_line"]
```

### Task 4.9 — `test_localizer.py`: malformed file does not raise

- [ ] Verify `_extract_symbols` tolerates a syntactically broken file (returns `[]` or partial, never raises), keeping the pipeline robust.

```python
@pytest.mark.mocked
def test_broken_file_tolerated(sample_repo, patch_gemma):
    (sample_repo / "broken.py").write_text("def oops(:\n  pass\n")
    patch_gemma({})
    loc = FaultLocalizer(str(sample_repo))
    # Should not raise; symbols may be empty or partial.
    loc.locate_symbols("x", "broken.py")
```

### Task 4.10 — `test_localizer.py`: no stdout pollution

- [ ] Running the full pipeline must emit nothing to stdout (all logs go to stderr).

```python
@pytest.mark.mocked
def test_no_stdout_pollution(sample_repo, patch_gemma, capsys):
    patch_gemma({"rank the files":
                 '[{"file": "config.py", "score": 0.9, "reason": "x"}]',
                 "select the ones most likely":
                 '[{"symbol": "parse_config", "reason": "x"}]',
                 "identify the specific line ranges":
                 '[{"file": "config.py", "start_line": 1, "end_line": 3, "reason": "x"}]'})
    loc = FaultLocalizer(str(sample_repo))
    files = loc.locate_files("parse_config None crash", top_k=2)
    syms = loc.locate_symbols("parse_config None crash", files[0]["file"])
    loc.suggest_edit_sites("parse_config None crash", files[0]["file"],
                           [s["symbol"] for s in syms])
    captured = capsys.readouterr()
    assert captured.out == ""
```

### Task 4.11 — Run the suite

- [ ] Install deps and run the mocked tests; all must pass.

```bash
pip install -r /Users/zachstallbohm/Work/gemma/services/skills/repo-fault-localize/requirements.txt
pip install pytest
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skills/repo-fault-localize -m mocked -v
```

---

## Phase 5 — Verification

### Task 5.1 — stdout-purity smoke test of the server

- [ ] Confirm the server starts and its stdout carries only JSON-RPC (no banner). Send an `initialize` request and assert the first stdout byte is `{`.

```bash
cd /Users/zachstallbohm/Work/gemma/services/skills/repo-fault-localize
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  | python server.py 2>/dev/null | head -c1
# expect: {
```

### Task 5.2 — Self-review checklist

- [ ] All three tools (`locate_files`, `locate_symbols`, `suggest_edit_sites`) are declared in `list_tools`, dispatched in `call_tool`, listed in SKILL.md frontmatter, and exercised by tests.
- [ ] `BM25Index`, `FaultLocalizer` names are used consistently across `bm25_index.py`, `localizer.py`, `server.py`, and tests.
- [ ] All three stages compose: stage 2 consumes a `file` from stage 1, stage 3 consumes `symbols` from stage 2.
- [ ] Every tool returns JSONL (newline-joined JSON objects), empty string on no results.
- [ ] LLM access is centralized in `FaultLocalizer._call_gemma`; tests monkeypatch only that method; no real network calls in mocked tests.
- [ ] `GEMMA_BASE` / `GEMMA_MODEL` read from env; no hardcoded inference URL.
- [ ] `repo-graph` use is best-effort and degrades to BM25-only when unavailable; `requires: [repo-graph]` is declared in SKILL.md.
- [ ] No `print()` anywhere; all logging via `logging` to `sys.stderr`.
- [ ] No `tiktoken`, no `chromadb.PersistentClient`, no MongoDB.
- [ ] LLM line numbers are never trusted: symbol ranges come from the AST; edit-site ranges are clamped to symbol bounds.
- [ ] No placeholder/TODO bodies — every method has a working implementation.
