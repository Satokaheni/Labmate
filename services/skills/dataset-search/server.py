"""MCP server for the dataset-search skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import asyncio
import json
import logging
import sys

import dataset_search

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-search.server")
app: Server = Server("dataset-search")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="search_hf_hub",
             description="Search Hugging Face Hub for datasets.",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string"},
                 "task": {"type": "string"},
                 "modality": {"type": "string"},
                 "max_results": {"type": "integer", "default": 10}},
                 "required": ["query"]}),
        Tool(name="search_papers_with_code",
             description="Search PapersWithCode for datasets.",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string"},
                 "max_results": {"type": "integer", "default": 5}},
                 "required": ["query"]}),
        Tool(name="rank_candidates",
             description="Score and rank dataset candidates by query overlap.",
             inputSchema={"type": "object", "properties": {
                 "candidates": {"type": "array", "items": {"type": "object"}},
                 "query": {"type": "string"},
                 "criteria": {"type": "array", "items": {"type": "string"}}},
                 "required": ["candidates", "query"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "search_hf_hub":
            result = dataset_search.search_hf_hub(
                arguments["query"],
                task=arguments.get("task"),
                modality=arguments.get("modality"),
                max_results=int(arguments.get("max_results", 10)),
            )
        elif name == "search_papers_with_code":
            result = dataset_search.search_papers_with_code(
                arguments["query"],
                max_results=int(arguments.get("max_results", 5)),
            )
        elif name == "rank_candidates":
            result = dataset_search.rank_candidates(
                arguments["candidates"],
                arguments["query"],
                arguments.get("criteria"),
            )
        else:
            raise ValueError(f"unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(result, default=str))]
    except Exception as exc:
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def main() -> None:
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
