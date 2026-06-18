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

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".labmate", "dist", "build", ".venv"}

_DEF_KINDS = {
    "function_definition",
    "class_definition",
    "function_declaration",
    "method_definition",
    "function_item",
    "struct_item",
    "class_declaration",
}


@dataclass(frozen=True)
class Edge:
    src_file: str       # repo-relative path of the referencing site
    src_line: int       # 1-based line of the reference
    src_symbol: str     # enclosing definition name at the reference site ("" if module-level)
    dst_file: str       # repo-relative path where the symbol is defined
    dst_line: int       # 1-based line of the definition
    dst_symbol: str     # the referenced symbol's name
    kind: str           # 'call' | 'import' | 'type_ref' | 'inherit'


def _node_name(node, src_bytes: bytes) -> str:
    """Extract text of a node from the source bytes using its byte range."""
    return src_bytes[node.start_byte():node.end_byte()].decode("utf-8", "replace")


def _node_line(node) -> int:
    """1-based line number of a node."""
    return node.start_position().row + 1


def _iter_children(node):
    """Yield all children of a node (tree-sitter 0.25 API)."""
    count = node.child_count()
    for i in range(count):
        yield node.child(i)


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

    def _parse_file(self, path: str):
        """Return (src_bytes, tree) or (None, None) on error."""
        lang = _LANG_BY_EXT[os.path.splitext(path)[1]]
        try:
            with open(path, "rb") as fh:
                src_bytes = fh.read()
            src_str = src_bytes.decode("utf-8", "replace")
            tree = get_parser(lang).parse(src_str)
            return src_bytes, tree
        except Exception as exc:  # noqa: BLE001 - never crash the builder
            log.warning("parse failed for %s: %s", path, exc)
            return None, None

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

    def _extract_definitions(self, path: str) -> list[tuple[str, int]]:
        src_bytes, tree = self._parse_file(path)
        if tree is None:
            return []
        out: list[tuple[str, int]] = []

        def walk(node):
            if node.kind() in _DEF_KINDS:
                name = node.child_by_field_name("name")
                if name is not None:
                    out.append((_node_name(name, src_bytes), _node_line(name)))
            for child in _iter_children(node):
                walk(child)

        walk(tree.root_node())
        return out

    def _extract_edges(self, path: str) -> list[Edge]:
        src_bytes, tree = self._parse_file(path)
        if tree is None:
            return []

        rel = self._rel(path)
        edges: list[Edge] = []

        def enclosing_symbol(node) -> str:
            cur = node.parent()
            while cur is not None:
                if cur.kind() in _DEF_KINDS:
                    name = cur.child_by_field_name("name")
                    if name is not None:
                        return _node_name(name, src_bytes)
                cur = cur.parent()
            return ""

        def emit(name_node, kind):
            name = _node_name(name_node, src_bytes)
            targets = self._definitions.get(name)
            if not targets:
                return  # unresolved reference -> dropped
            src_line = _node_line(name_node)
            for dst_file, dst_line in targets:
                if dst_file == rel and dst_line == src_line:
                    continue  # skip self-definition edge
                edges.append(Edge(
                    src_file=rel, src_line=src_line,
                    src_symbol=enclosing_symbol(name_node),
                    dst_file=dst_file, dst_line=dst_line, dst_symbol=name, kind=kind,
                ))

        def walk(node):
            kind = node.kind()
            if kind in ("call", "call_expression"):
                fn = node.child_by_field_name("function")
                if fn is None and node.child_count() > 0:
                    fn = node.child(0)
                if fn is not None:
                    callee = fn.child_by_field_name("attribute")
                    if callee is None:
                        callee = fn
                    if callee.kind() in ("identifier", "property_identifier"):
                        emit(callee, "call")
            elif kind in ("import_from_statement", "import_statement", "import_declaration"):
                for ident in _iter_children(node):
                    if ident.kind() in ("dotted_name", "identifier"):
                        emit(ident, "import")
            elif kind in ("type", "type_identifier"):
                emit(node, "type_ref")
            elif kind in ("argument_list", "superclasses", "class_heritage"):
                for ch in _iter_children(node):
                    if ch.kind() in ("identifier", "type_identifier"):
                        emit(ch, "inherit")
            for child in _iter_children(node):
                walk(child)

        walk(tree.root_node())
        return edges
