import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from graph_builder import RepoGraphBuilder
from graph_store import GraphStore

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("repo-graph")

app = Server("repo-graph")


def _db_path(repo_path: str) -> str:
    return os.path.join(os.path.abspath(repo_path), ".labmate", "repo_graph.sqlite")


def _jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in rows)


_LAST_REPO: dict[str, str] = {}  # holds {"path": <repo_path>}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="build",
             description="Build/update the line-level code graph for a repo. Returns summary stats.",
             inputSchema={"type": "object",
                          "properties": {"repo_path": {"type": "string"}},
                          "required": ["repo_path"]}),
        Tool(name="search",
             description="Search symbols by name. Returns JSONL of file:line matches.",
             inputSchema={"type": "object",
                          "properties": {"query": {"type": "string"},
                                         "top_k": {"type": "integer", "default": 10}},
                          "required": ["query"]}),
        Tool(name="get_references",
             description="All sites that reference a symbol. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"file": {"type": "string"},
                                         "symbol": {"type": "string"}},
                          "required": ["file", "symbol"]}),
        Tool(name="get_callers",
             description="Functions/methods that call a symbol. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"file": {"type": "string"},
                                         "symbol": {"type": "string"}},
                          "required": ["file", "symbol"]}),
        Tool(name="get_callees",
             description="Functions/methods this symbol calls. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"file": {"type": "string"},
                                         "symbol": {"type": "string"}},
                          "required": ["file", "symbol"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "build":
        repo_path = arguments["repo_path"]
        builder = RepoGraphBuilder(repo_path)
        edges = builder.build()
        store = GraphStore(_db_path(repo_path))
        store.upsert_definitions(builder.definitions())
        store.upsert_edges(edges)
        store.close()
        _LAST_REPO["path"] = repo_path
        by_kind: dict[str, int] = {}
        for e in edges:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        stats = {"repo_path": repo_path,
                 "definitions": len(builder.definitions()),
                 "edges": len(edges), "by_kind": by_kind}
        return [TextContent(type="text", text=json.dumps(stats))]

    repo_path = _LAST_REPO.get("path")
    if repo_path is None:
        return [TextContent(type="text",
                            text=json.dumps({"error": "call build first"}))]
    store = GraphStore(_db_path(repo_path))
    try:
        if name == "search":
            rows = store.search(arguments["query"], int(arguments.get("top_k", 10)))
        elif name == "get_references":
            rows = store.get_references(arguments["file"], arguments["symbol"])
        elif name == "get_callers":
            rows = store.get_callers(arguments["file"], arguments["symbol"])
        elif name == "get_callees":
            rows = store.get_callees(arguments["file"], arguments["symbol"])
        else:
            return [TextContent(type="text",
                                text=json.dumps({"error": f"unknown tool {name}"}))]
    finally:
        store.close()
    return [TextContent(type="text", text=_jsonl(rows))]


async def _run() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main() -> None:
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
