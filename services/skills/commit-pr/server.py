"""MCP server for the commit-pr skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import asyncio
import json
import logging
import sys

import commit_pr

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("commit-pr.server")
app: Server = Server("commit-pr")


@app.list_tools()
async def list_tools() -> list[Tool]:
    groups = {"groups": {"type": "array", "items": {"type": "object"}}}
    return [
        Tool(name="summarize_diff",
             description="Group diff hunks by intent (reads diff only).",
             inputSchema={"type": "object", "properties": {
                 "diff_text": {"type": "string"},
                 "repo_path": {"type": "string"}}}),
        Tool(name="write_commit",
             description="Emit a Conventional Commits message from grouped changes.",
             inputSchema={"type": "object", "properties": {
                 **groups, "scope": {"type": "string"}}, "required": ["groups"]}),
        Tool(name="write_pr",
             description="Emit a PR body (Summary/Rationale/Test Plan/Risk Notes).",
             inputSchema={"type": "object", "properties": {
                 **groups, "title": {"type": "string"}}, "required": ["groups"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "summarize_diff":
            result = await commit_pr.summarize_diff(
                arguments.get("diff_text"), arguments.get("repo_path"))
        elif name == "write_commit":
            result = await commit_pr.write_commit(
                arguments["groups"], arguments.get("scope"))
        elif name == "write_pr":
            result = await commit_pr.write_pr(
                arguments["groups"], arguments.get("title"))
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
