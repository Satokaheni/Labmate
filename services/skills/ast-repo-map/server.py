"""MCP stdio server for the ast.repo-map skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
All logging is configured to sys.stderr below.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# CRITICAL: stderr only. Configure before importing/using anything that logs.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("ast.repo-map.server")

from repo_mapper import RepoMapper  # noqa: E402 (after logging is configured)

app: Server = Server("ast.repo-map")
mapper: RepoMapper | None = None


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_repo_map",
            description=(
                "Return a JSONL list of the most important symbols in the "
                "repository, ranked by personalized PageRank and bounded by a "
                "token budget."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files the agent is actively editing.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Hard cap on output tokens.",
                    },
                },
                "required": ["chat_files", "max_tokens"],
            },
        ),
        Tool(
            name="get_symbols",
            description="Return all symbols defined in a specific file as JSONL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Repo-relative path to a source file.",
                    }
                },
                "required": ["file"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global mapper
    if mapper is None:
        repo_root = os.getenv("REPO_ROOT", os.getcwd())
        mapper = RepoMapper(repo_root)
        log.info("RepoMapper initialized at %s", repo_root)

    try:
        if name == "get_repo_map":
            result = mapper.get_repo_map(
                chat_files=arguments["chat_files"],
                max_tokens=arguments["max_tokens"],
            )
        elif name == "get_symbols":
            result = mapper.get_symbols(file=arguments["file"])
        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=f"unknown tool: {name}")]
    except Exception as exc:  # never let one bad call crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=f"error: {exc!r}")]

    return [TextContent(type="text", text=result)]


async def main() -> None:
    log.info("starting ast.repo-map MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
