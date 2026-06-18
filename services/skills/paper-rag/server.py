"""paper-rag MCP server — stdio transport.

Exposes paper_rag.add_papers, paper_rag.query, paper_rag.search, paper_rag.list_papers.
stdout carries JSON-RPC only; all logging is to stderr.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from paper_store import PaperStore

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-rag.server")

app = Server("paper-rag")
_store: PaperStore | None = None


def _get_store() -> PaperStore:
    global _store
    if _store is None:
        _store = PaperStore()
    return _store


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="paper_rag.add_papers",
            description="Ingest PDF files into the index (parse, embed, store).",
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["paths"],
            },
        ),
        Tool(
            name="paper_rag.query",
            description="Answer a question with inline citations. Returns JSON with answer + evidence + citations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["question"],
            },
        ),
        Tool(
            name="paper_rag.search",
            description="Similarity search over ingested papers. Returns JSONL of matches.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="paper_rag.list_papers",
            description="List all ingested papers (title, path, date).",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    store = _get_store()
    if name == "paper_rag.add_papers":
        result = await store.add_papers(arguments["paths"])
        text = json.dumps(result)
    elif name == "paper_rag.query":
        result = await store.query(arguments["question"], arguments.get("top_k", 5))
        text = json.dumps(result)
    elif name == "paper_rag.search":
        matches = await store.search(arguments["query"], arguments.get("top_k", 10))
        text = "\n".join(json.dumps(m) for m in matches)  # JSONL
    elif name == "paper_rag.list_papers":
        papers = await store.list_papers()
        text = json.dumps(papers)
    else:
        raise ValueError(f"unknown tool: {name}")
    return [TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
