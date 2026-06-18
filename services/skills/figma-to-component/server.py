"""MCP stdio server for the figma_to_component skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("figma-to-component.server")

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import TextContent, Tool  # noqa: E402

from figma_fetcher import FigmaFetcher  # noqa: E402
from component_synth import ComponentSynthesizer  # noqa: E402

app: Server = Server("figma-to-component")
_fetcher: FigmaFetcher | None = None
_synth: ComponentSynthesizer | None = None


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="figma_to_component.convert",
            description=(
                "Fetch a Figma node's structured data and synthesize a React + Tailwind "
                "component. Returns JSON: component_code, component_name, props_interface, framework."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "figma_file_key": {"type": "string"},
                    "node_id": {"type": "string"},
                    "framework": {"type": "string", "default": "react-tailwind"},
                },
                "required": ["figma_file_key", "node_id"],
            },
        ),
        Tool(
            name="figma_to_component.inspect",
            description="Fetch and return the structured Figma node data (ComponentSpec) as JSON.",
            inputSchema={
                "type": "object",
                "properties": {
                    "figma_file_key": {"type": "string"},
                    "node_id": {"type": "string"},
                },
                "required": ["figma_file_key", "node_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _fetcher, _synth
    try:
        if _fetcher is None:
            _fetcher = FigmaFetcher()
        if _synth is None:
            _synth = ComponentSynthesizer()

        if name == "figma_to_component.inspect":
            spec = await _fetcher.get_node(arguments["figma_file_key"], arguments["node_id"])
            text = spec.model_dump_json()
        elif name == "figma_to_component.convert":
            spec = await _fetcher.get_node(arguments["figma_file_key"], arguments["node_id"])
            framework = arguments.get("framework", "react-tailwind")
            result = _synth.synthesize(spec, framework)
            text = result.model_dump_json()
        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    except Exception as exc:
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": repr(exc)}))]
    return [TextContent(type="text", text=text)]


async def main() -> None:
    log.info("starting figma-to-component MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
