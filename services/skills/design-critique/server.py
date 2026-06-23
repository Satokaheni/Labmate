"""design-critique MCP server — exposes critique and compare over stdio."""
from __future__ import annotations

import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from critic import FOCUS_AREAS, UICritic

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("design-critique.server")

app = Server("design-critique")
critic = UICritic()


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="critique",
            description=(
                "Critique a UI screenshot. Returns a per-area checklist with "
                "pass/fail/warning status per item and an overall verdict."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string"},
                    "focus_areas": {
                        "type": "array",
                        "items": {"type": "string", "enum": FOCUS_AREAS},
                    },
                },
                "required": ["image_path"],
            },
        ),
        Tool(
            name="compare",
            description=(
                "Compare two UI screenshots (before/after) and return a "
                "diff-focused critique as JSON."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "before_path": {"type": "string"},
                    "after_path": {"type": "string"},
                },
                "required": ["before_path", "after_path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "critique":
        result = critic.critique(
            arguments["image_path"], arguments.get("focus_areas")
        )
        return [TextContent(type="text", text=result.model_dump_json())]
    if name == "compare":
        result = critic.compare(arguments["before_path"], arguments["after_path"])
        return [TextContent(type="text", text=json.dumps(result))]
    raise ValueError(f"unknown tool: {name}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
