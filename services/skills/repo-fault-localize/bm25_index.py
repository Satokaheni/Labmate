import logging
import os
import re
import sys

from rank_bm25 import BM25Plus

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


class BM25Index:
    def __init__(self, repo_path: str):
        self._repo_path = os.path.abspath(repo_path)
        self._files: list[str] = []          # repo-relative paths, index-aligned with corpus
        self._corpus: list[list[str]] = []   # tokenized doc per file
        self._bm25: BM25Plus | None = None

    def _iter_code_files(self):
        for dirpath, dirnames, filenames in os.walk(self._repo_path):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1] in _CODE_EXTS:
                    yield os.path.join(dirpath, fn)

    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self._repo_path)

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
            self._bm25 = BM25Plus(self._corpus)
        log.info("BM25 index built over %d files", len(self._files))

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        if self._bm25 is None or not self._files:
            return []
        q = _tokenize(query)
        scores = self._bm25.get_scores(q)
        ranked = sorted(zip(self._files, scores), key=lambda p: p[1], reverse=True)
        return [(f, float(s)) for f, s in ranked[:top_k] if s > 0.0]
