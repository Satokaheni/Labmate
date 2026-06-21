"""MCP server for the rebuttal-response skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rebuttal

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("rebuttal-response.server")
app: Server = Server("rebuttal-response")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="parse_reviews",
             description="Decompose reviewer text into an itemized concern matrix.",
             inputSchema={"type": "object",
                          "properties": {"review_text": {"type": "string"}},
                          "required": ["review_text"]}),
        Tool(name="draft_response",
             description="Generate point-by-point replies grounded in the paper.",
             inputSchema={"type": "object", "properties": {
                 "concerns": {"type": "array", "items": {"type": "object"}},
                 "paper_context": {"type": "string"}},
                 "required": ["concerns", "paper_context"]}),
        Tool(name="coverage_audit",
             description="Confirm every concern is addressed; flag the gaps.",
             inputSchema={"type": "object", "properties": {
                 "concerns": {"type": "array", "items": {"type": "object"}},
                 "responses": {"type": "array", "items": {"type": "object"}}},
                 "required": ["concerns", "responses"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "parse_reviews":
            result = rebuttal.parse_reviews(arguments["review_text"])
        elif name == "draft_response":
            result = await rebuttal.draft_response(
                arguments["concerns"], arguments["paper_context"])
        elif name == "coverage_audit":
            result = rebuttal.coverage_audit(
                arguments["concerns"], arguments["responses"])
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
