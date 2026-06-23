"""MCP server for the arxiv-prep skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import asyncio
import json
import logging
import sys

import arxiv_prep

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("arxiv-prep.server")
app: Server = Server("arxiv-prep")


@app.list_tools()
async def list_tools() -> list[Tool]:
    proj = {"project_dir": {"type": "string"}}
    return [
        Tool(name="clean_source", description="Run arxiv-latex-cleaner on a project dir.",
             inputSchema={"type": "object", "properties": proj, "required": ["project_dir"]}),
        Tool(name="verify_compile", description="Compile the main .tex with tectonic.",
             inputSchema={"type": "object", "properties": proj, "required": ["project_dir"]}),
        Tool(name="anonymize", description="Return a diff anonymizing the source (no in-place edit).",
             inputSchema={"type": "object", "properties": proj, "required": ["project_dir"]}),
        Tool(name="package_tarball", description="Create submission.tar.gz of the project dir.",
             inputSchema={"type": "object", "properties": {
                 **proj, "output_path": {"type": "string"}}, "required": ["project_dir"]}),
        Tool(name="extract_metadata", description="Extract title/authors/abstract/category.",
             inputSchema={"type": "object", "properties": proj, "required": ["project_dir"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "clean_source":
            result = arxiv_prep.clean_source(arguments["project_dir"])
        elif name == "verify_compile":
            result = arxiv_prep.verify_compile(arguments["project_dir"])
        elif name == "anonymize":
            result = arxiv_prep.anonymize(arguments["project_dir"])
        elif name == "package_tarball":
            result = arxiv_prep.package_tarball(
                arguments["project_dir"], arguments.get("output_path"))
        elif name == "extract_metadata":
            result = arxiv_prep.extract_metadata(arguments["project_dir"])
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
