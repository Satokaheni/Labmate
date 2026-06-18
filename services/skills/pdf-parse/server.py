"""MCP stdio server for the pdf_parse skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
All logging is configured to sys.stderr below, including third-party loggers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# CRITICAL: stderr only. Configure before importing anything that may log.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
# Docling and its backends log on the root logger; basicConfig above routes
# the root logger to stderr, so their output cannot leak onto stdout.
log = logging.getLogger("pdf-parse.server")

from parser import DocumentParser  # noqa: E402 (after logging is configured)

app: Server = Server("pdf-parse")
parser: DocumentParser | None = None


@app.list_tools()
async def list_tools() -> list[Tool]:
    mode_prop = {
        "type": "string",
        "enum": ["docling", "mineru"],
        "default": "docling",
        "description": "Parser backend. 'docling' is CPU-friendly (default); "
                       "'mineru' is higher-fidelity but requires a GPU.",
    }
    return [
        Tool(
            name="parse",
            description=(
                "Parse a single PDF to Markdown. Returns JSON with markdown, "
                "figures, tables, and metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the PDF."},
                    "mode": mode_prop,
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="parse_batch",
            description=(
                "Parse multiple PDFs. Returns JSONL, one result object per line, "
                "in input order."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Absolute paths to the PDFs.",
                    },
                    "mode": mode_prop,
                },
                "required": ["paths"],
            },
        ),
        Tool(
            name="extract_figures",
            description=(
                "Extract only the figures (image path + caption + page) from a "
                "PDF as a JSON list."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the PDF."},
                },
                "required": ["path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global parser
    if parser is None:
        out_dir = os.getenv("PDF_PARSE_OUTPUT_DIR", "/tmp/pdf-parse-assets")
        parser = DocumentParser(output_dir=out_dir)
        log.info("DocumentParser initialized, output_dir=%s", out_dir)

    try:
        if name == "parse":
            result = parser.parse(arguments["path"], mode=arguments.get("mode", "docling"))
            text = json.dumps(result.to_dict(), ensure_ascii=False)
        elif name == "parse_batch":
            results = parser.parse_batch(
                arguments["paths"], mode=arguments.get("mode", "docling")
            )
            text = "\n".join(
                json.dumps(r.to_dict(), ensure_ascii=False) for r in results
            )
        elif name == "extract_figures":
            figures = parser.extract_figures(arguments["path"])
            text = json.dumps(figures, ensure_ascii=False)
        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=f"unknown tool: {name}")]
    except Exception as exc:  # never let one bad call crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=f"error: {exc!r}")]

    return [TextContent(type="text", text=text)]


async def main() -> None:
    log.info("starting pdf-parse MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
