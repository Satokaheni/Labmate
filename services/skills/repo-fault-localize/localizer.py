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

    def _snippet(self, rel_path: str, max_lines: int = 40) -> str:
        try:
            with open(self._abs(rel_path), "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return ""
        head = lines[:max_lines]
        return "\n".join(head)

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

    def locate_files(self, issue: str, top_k: int = 5) -> list[dict]:
        self._ensure_index()
        n_candidates = max(12, top_k * 4)
        candidates = self._bm25.search(issue, top_k=n_candidates)
        candidates = self._expand_with_graph(candidates)
        ranked = self._rank_files(issue, candidates)
        return ranked[:top_k]

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
                src_bytes = fh.read()
            src_str = src_bytes.decode("utf-8", "replace")
            tree = get_parser(lang).parse(src_str)
        except Exception as exc:  # noqa: BLE001 - never crash the localizer
            log.warning("parse failed for %s: %s", rel_path, exc)
            return []

        out: list[dict] = []

        def walk(node):
            kind = self._DEF_KINDS.get(node.kind())
            if kind is not None:
                name = node.child_by_field_name("name")
                if name is not None:
                    out.append({
                        "file": rel_path,
                        "symbol": src_bytes[name.start_byte():name.end_byte()].decode("utf-8", "replace"),
                        "kind": kind,
                        "start_line": node.start_position().row + 1,
                        "end_line": node.end_position().row + 1,
                    })
            for i in range(node.child_count()):
                walk(node.child(i))

        walk(tree.root_node())
        return out

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
