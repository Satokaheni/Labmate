"""PaperStore — cited agentic RAG over scientific PDFs using PaperQA2.

The vector store points at the shared Chroma container (client-server mode,
CLAUDE.md rule #4), not PaperQA's default local index. All logging is to
stderr — stdout is reserved for MCP JSON-RPC.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import paperqa
from paperqa import Settings
from paperqa.settings import AgentSettings

# stderr-only logger — stdout is sacred; use log.xxx, never the print builtin.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-rag")

CHROMA_URL = os.getenv("CHROMA_URL", "http://chroma:8000")
CHROMA_COLLECTION = os.getenv("PAPER_RAG_COLLECTION", "paper_rag")
# Local embedding model — local-first constraint, no OpenAI embeddings.
EMBED_MODEL = os.getenv("PAPER_RAG_EMBED_MODEL", "st-all-MiniLM-L6-v2")


def _chroma_host_port(url: str) -> tuple[str, int]:
    """Parse 'http://chroma:8000' -> ('chroma', 8000)."""
    host = url.split("//")[1].split(":")[0]
    port = int(url.split(":")[-1])
    return host, port


class PaperStore:
    """Wraps a PaperQA2 ``Docs`` index backed by the Chroma container."""

    def __init__(self) -> None:
        host, port = _chroma_host_port(CHROMA_URL)
        # CORRECT: client-server mode only. Do not use the local client variants.
        self._chroma = chromadb.AsyncHttpClient(host=host, port=port)
        self._collection_name = CHROMA_COLLECTION
        # PaperQA2 settings: local embedding model, no remote LLM embeddings.
        self._settings = Settings(
            embedding=EMBED_MODEL,
            agent=AgentSettings(agent_llm=os.getenv("PAPER_RAG_LLM", "ollama/llama3")),
        )
        self._docs = paperqa.Docs()
        log.info("PaperStore initialized (chroma=%s:%s, embed=%s)", host, port, EMBED_MODEL)

    async def add_papers(self, paths: list[str]) -> dict:
        """Ingest PDF files: parse, embed, store. Returns a summary dict."""
        added: list[dict] = []
        errors: list[dict] = []
        for raw in paths:
            p = Path(raw)
            if not p.exists():
                errors.append({"path": raw, "error": "file not found"})
                log.warning("add_papers: missing file %s", raw)
                continue
            try:
                # PaperQA parses, chunks, and embeds the PDF with the local model.
                docname = await self._docs.aadd(str(p), settings=self._settings)
                added.append(
                    {
                        "path": str(p),
                        "docname": docname,
                        "added_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                log.info("add_papers: ingested %s as %s", p, docname)
            except Exception as exc:  # noqa: BLE001 — report, don't crash the server
                errors.append({"path": raw, "error": str(exc)})
                log.exception("add_papers: failed on %s", p)
        return {"added": added, "errors": errors, "count": len(added)}

    async def query(self, question: str, top_k: int = 5) -> dict:
        """Answer a question with inline citations. Returns answer/evidence/citations."""
        self._settings.answer.evidence_k = top_k
        session = await self._docs.aquery(question, settings=self._settings)
        contexts = getattr(session, "contexts", []) or []
        evidence = [
            {
                "text": getattr(c, "context", ""),
                "citation": getattr(getattr(c, "text", None), "name", ""),
                "score": getattr(c, "score", None),
            }
            for c in contexts
        ]
        # Distinct sources cited in the answer.
        citations = sorted({e["citation"] for e in evidence if e["citation"]})
        return {
            "question": question,
            "answer": getattr(session, "answer", ""),
            "evidence": evidence,
            "citations": citations,
        }

    async def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Similarity search over ingested passages. Returns a list of match dicts."""
        # get_evidence retrieves passages without synthesizing an answer.
        self._settings.answer.evidence_k = top_k
        session = await self._docs.aget_evidence(query, settings=self._settings)
        contexts = getattr(session, "contexts", []) or []
        matches: list[dict] = []
        for c in contexts[:top_k]:
            text_obj = getattr(c, "text", None)
            matches.append(
                {
                    "text": getattr(c, "context", ""),
                    "citation": getattr(text_obj, "name", ""),
                    "docname": getattr(getattr(text_obj, "doc", None), "docname", ""),
                    "score": getattr(c, "score", None),
                }
            )
        return matches

    async def list_papers(self) -> list[dict]:
        """List ingested papers: title, path, date."""
        papers: list[dict] = []
        for doc in self._docs.docs.values():
            papers.append(
                {
                    "title": getattr(doc, "title", None) or getattr(doc, "docname", ""),
                    "path": getattr(doc, "filepath", "") or "",
                    "docname": getattr(doc, "docname", ""),
                    "citation": getattr(doc, "citation", ""),
                }
            )
        return papers
