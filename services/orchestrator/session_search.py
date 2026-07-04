"""Thin, unit-testable wrapper turning Labmate's raw chat_turns transcript into
a flat, ReAct-loop-callable tool.

The orchestrator holds an optional ``SessionSearch`` instance and the loop
dispatches to it when the model calls the ``session_search`` tool.  The
wrapper takes an INJECTABLE store (anything exposing ``async
search_turns(query, top_k, mode, session_id) -> list[dict]``, which
``StorageManager`` already does) so it is testable with a fake store and
never touches live Mongo itself.

Output is RAW: the retrieved turn text is returned verbatim, ranked, and
truncated for budget — never summarized by an LLM.  Results go in the message
TAIL (cache-safe, never the prefix).
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("orchestrator")

# Budget-aware caps: per-snippet and total limits (per-snippet 600 / total 4000)
# because output shape is a ranked list.
_MAX_SNIPPET_CHARS = 600
_MAX_TOTAL_CHARS = 4000
_MAX_K = 20  # matches the code_semantic_search "max 20 results" contract


class SessionSearch:
    """Retrieve past conversation turns from Mongo and format them as raw text.

    store: any object with
        ``async search_turns(query: str, top_k: int, mode: str,
                             session_id: str | None) -> list[dict]``.
        Each dict must carry ``sessionId``, ``seq``, and ``text`` keys.
        ``None`` is allowed and yields a clear "not available" sentinel
        (regression-safe).
    """

    def __init__(self, store: Any, *, max_results: int = 8) -> None:
        self.store = store
        self.max_results = max_results

    async def search(
        self,
        query: str,
        k: int | None = None,
        mode: str = "text",
        session_id: str | None = None,
    ) -> str:
        if self.store is None:
            return "(session search not available)"

        top_k = self.max_results if k is None else int(k)
        top_k = max(1, min(_MAX_K, top_k))

        try:
            rows = await self.store.search_turns(
                query or "", top_k, mode=mode, session_id=session_id
            )
        except Exception as exc:  # never raise into the loop
            _logger.warning("session_search failed: %s", exc)
            return f"session search failed: {exc}"

        snippets: list[str] = []
        for i, row in enumerate(rows or [], start=1):
            if not isinstance(row, dict):
                continue
            text = (row.get("text") or "").strip()
            if not text:
                continue
            sid = row.get("sessionId", "?")
            seq = row.get("seq", "?")
            snippets.append(f"[{i}] (session {sid}, turn {seq}) {text[:_MAX_SNIPPET_CHARS]}")

        if not snippets:
            return "(no matching past turns)"

        return "\n".join(snippets)[:_MAX_TOTAL_CHARS]


__all__ = ["SessionSearch"]
