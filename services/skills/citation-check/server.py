"""citation-check MCP server — exposes verify_claims and verify_citations."""
from __future__ import annotations

import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

import claim_verifier
import citation_verifier
from models import ClaimVerificationResult  # noqa: F401 — imported for type context

# All diagnostics to stderr — stdout is reserved for JSON-RPC.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("citation-check.server")

app = Server("citation-check")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="verify_claims",
            description="Decompose text into claim-triplets and verify each against references.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "references": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "references"],
            },
        ),
        Tool(
            name="verify_citations",
            description="Verify BibTeX entries against Crossref/Semantic Scholar.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bibliography": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["bibliography"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "verify_claims":
        result = claim_verifier.verify_claims(
            arguments["text"], arguments.get("references", [])
        )
        return [TextContent(type="text", text=result.model_dump_json())]
    if name == "verify_citations":
        results = citation_verifier.verify_citations(arguments["bibliography"])
        payload = json.dumps([r.model_dump() for r in results])
        return [TextContent(type="text", text=payload)]
    raise ValueError(f"unknown tool: {name}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
