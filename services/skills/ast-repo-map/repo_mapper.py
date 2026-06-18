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
import tree_sitter
from tree_sitter_language_pack import get_language

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


class RepoMapper:
    """Parses a repository, caches by mtime, ranks symbols by PageRank."""

    def __init__(self, repo_root: str) -> None:
        self.repo_root = Path(repo_root).resolve()
        # path (repo-relative str) -> {"mtime": float, "tags": list[Tag]}
        self._cache: dict[str, dict] = {}
        # parse-call counter, used by tests to assert cache hits
        self._parse_count = 0

    def _lang_for(self, path: str) -> str | None:
        return EXT_TO_LANG.get(Path(path).suffix)

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

        language = get_language(lang_name)
        # Use Python tree_sitter.Parser (not the Rust-native get_parser)
        # so we get a Python Tree/Node with the Query/QueryCursor API.
        parser = tree_sitter.Parser(language)
        # tree-sitter is error-tolerant: broken code yields a partial tree
        # with ERROR nodes and does NOT raise.
        tree = parser.parse(source)
        self._parse_count += 1

        tags = self._extract_tags(language, tree, source, path)
        self._cache[path] = {"mtime": mtime, "tags": tags}
        return tags

    def _extract_tags(
        self, language, tree, source: bytes, path: str
    ) -> list[Tag]:
        lang_name = self._lang_for(path)
        # tree_sitter 0.25+: Query constructor takes (Language, str)
        # QueryCursor.captures(node) returns dict[str, list[Node]]
        query = tree_sitter.Query(language, TAGS_QUERIES[lang_name])
        cursor = tree_sitter.QueryCursor(query)
        tags: list[Tag] = []
        captures = cursor.captures(tree.root_node)
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

    @staticmethod
    def _signature_line(source: bytes, node) -> str:
        text = source.decode("utf-8", errors="replace")
        lines = text.splitlines()
        row = node.start_point[0]
        if 0 <= row < len(lines):
            return lines[row].strip()
        return ""

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
            # Use the pure-Python implementation to avoid scipy/numpy version
            # conflicts in environments where scipy may be built against an
            # incompatible NumPy version.
            from networkx.algorithms.link_analysis.pagerank_alg import (
                _pagerank_python,
            )
            return _pagerank_python(
                graph, personalization=personalization, weight="weight"
            )
        except nx.PowerIterationFailedConvergence:
            log.warning("pagerank failed to converge; using uniform ranks")
            n = graph.number_of_nodes()
            return {node: 1.0 / n for node in graph.nodes()}

    def _normalize(self, path: str) -> str:
        p = Path(path)
        if p.is_absolute():
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                return str(p)
        return str(Path(path))  # collapses "./x" -> "x"

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

        omitted = len(def_tags) - emitted
        if omitted > 0:
            lines.append(f"// ... {omitted} symbols omitted")
        return "\n".join(lines)

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
