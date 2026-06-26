"""Thin, unit-testable wrapper turning Labmate's vector memory into a flat,
ReAct-loop-callable tool.

Mirrors how ``code_semantic_search`` wraps the codegraph MCP: the orchestrator
holds an optional ``MemorySearch`` instance and the loop dispatches to it when
the model calls the ``memory_search`` tool. The wrapper takes an INJECTABLE
store (anything exposing ``async search_memories(query, top_k) -> list[dict]``,
which ``StorageManager`` already does) so it is testable with a fake store and
never touches live Chroma/Mongo itself.

Output is RAW: the retrieved snippet text is returned verbatim, ranked, and
truncated for budget — never summarized by an LLM.
"""
from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("orchestrator")

# Generous caps (no shared tool_grounding helper exists in this repo).
_MAX_SNIPPET_CHARS = 600
_MAX_TOTAL_CHARS = 4000
_MAX_K = 20  # matches the code_semantic_search "max 20 results" contract


class MemorySearch:
    """Retrieve prior context from vector memory and format it as raw text.

    store: any object with ``async search_memories(query: str, top_k: int)
           -> list[dict]``. Each dict is expected to carry a human-readable
           ``fact`` (preferred) or ``raw_fact`` field. ``None`` is allowed and
           yields a clear "not available" sentinel (regression-safe).
    """

    def __init__(self, store: Any, *, max_results: int = 8) -> None:
        self.store = store
        self.max_results = max_results

    async def search(self, query: str, k: int | None = None) -> str:
        if self.store is None:
            return "(memory store not available)"

        top_k = self.max_results if k is None else int(k)
        top_k = max(1, min(_MAX_K, top_k))

        try:
            rows = await self.store.search_memories(query or "", top_k)
        except Exception as exc:  # never raise into the loop
            _logger.warning("memory_search failed: %s", exc)
            return f"memory search failed: {exc}"

        snippets: list[str] = []
        for i, row in enumerate(rows or [], start=1):
            text = ""
            if isinstance(row, dict):
                text = (row.get("fact") or row.get("raw_fact") or "").strip()
            elif isinstance(row, str):
                text = row.strip()
            if not text:
                continue
            snippets.append(f"[{i}] {text[:_MAX_SNIPPET_CHARS]}")

        if not snippets:
            return "(no relevant memory found)"

        return "\n".join(snippets)[:_MAX_TOTAL_CHARS]


__all__ = ["MemorySearch"]
