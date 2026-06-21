"""MCP server for the dataset-generation skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import asyncio
import json
import logging
import sys

import dataset_generation

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-generation.server")
app: Server = Server("dataset-generation")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="generate_from_seeds",
             description="Generate synthetic training examples from seed prompts.",
             inputSchema={"type": "object", "properties": {
                 "seeds": {"type": "array", "items": {"type": "string"}},
                 "template": {"type": "string"},
                 "n_per_seed": {"type": "integer", "default": 3},
                 "output_format": {"type": "string",
                                   "enum": ["instruction-response", "preference", "chat"],
                                   "default": "instruction-response"}},
                 "required": ["seeds", "template"]}),
        Tool(name="format_as_jsonl",
             description="Write generated examples to a JSONL file.",
             inputSchema={"type": "object", "properties": {
                 "examples": {"type": "array", "items": {"type": "object"}},
                 "output_path": {"type": "string"}},
                 "required": ["examples", "output_path"]}),
        Tool(name="validate_coverage",
             description="Check that generated examples cover all requirements.",
             inputSchema={"type": "object", "properties": {
                 "examples": {"type": "array", "items": {"type": "object"}},
                 "requirements": {"type": "array", "items": {"type": "string"}}},
                 "required": ["examples", "requirements"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "generate_from_seeds":
            result = await dataset_generation.generate_from_seeds(
                arguments["seeds"],
                arguments["template"],
                n_per_seed=int(arguments.get("n_per_seed", 3)),
                output_format=arguments.get("output_format", "instruction-response"),
            )
        elif name == "format_as_jsonl":
            result = dataset_generation.format_as_jsonl(
                arguments["examples"], arguments["output_path"])
        elif name == "validate_coverage":
            result = dataset_generation.validate_coverage(
                arguments["examples"], arguments["requirements"])
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
