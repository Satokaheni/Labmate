"""MCP stdio server — exposes code_semantic_search tool."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from urllib.parse import urlparse

import chromadb
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .indexer import CodeGraphIndexer
from .search  import hybrid_code_search

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("codegraph_mcp")

CHROMA_URL = os.getenv("CHROMA_URL", "http://localhost:8000")
COLLECTION = "code_symbols"

server = Server("codegraph-semantic")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(
        name="code_semantic_search",
        description=(
            "Search the codebase by meaning. Returns the top-k symbols "
            "(functions, classes, methods) most semantically relevant to the query. "
            "Use when you need to find code by what it does rather than what it's named."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of what to find",
                },
                "k": {
                    "type": "integer",
                    "default": 8,
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Number of results",
                },
            },
            "required": ["query"],
        },
    )]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "code_semantic_search":
        raise ValueError(f"unknown tool: {name}")

    results = await hybrid_code_search(
        query      = arguments["query"],
        chroma_col = server.state["col"],
        k          = min(int(arguments.get("k", 8)), 20),
    )
    return [TextContent(type="text", text=json.dumps(results, indent=2))]


async def main() -> None:
    parsed = urlparse(CHROMA_URL)
    chroma = await chromadb.AsyncHttpClient(
        host=parsed.hostname or "localhost",
        port=parsed.port or 8000,
    )
    col     = await chroma.get_or_create_collection(
        COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
    indexer = CodeGraphIndexer(col)

    server.state = {"col": col}

    # Background both tasks so stdio handshake starts immediately.
    # full_index is a no-op if collection is already populated.
    server.state["index_task"] = asyncio.create_task(indexer.full_index())
    server.state["watch_task"] = asyncio.create_task(indexer.watch())

    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
