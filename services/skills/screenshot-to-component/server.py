"""MCP stdio server for the screenshot_to_component skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
All logging is configured to sys.stderr below, including third-party loggers.
SINGLE-GPU: every LLM call targets GEMMA_BASE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# CRITICAL: stderr only. Configure before importing anything that may log (litellm).
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("screenshot-to-component.server")

from grounder import UIGrounder  # noqa: E402 (after logging is configured)
from pipeline import Pipeline  # noqa: E402
from planner import LayoutPlanner  # noqa: E402

app: Server = Server("screenshot-to-component")
pipeline: Pipeline | None = None


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate",
            description=(
                "Full 3-stage pipeline (grounding -> planning -> generation). "
                "Returns JSON with component_code, layout_plan, and output_path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Absolute path to the screenshot/mockup image.",
                    },
                    "framework": {
                        "type": "string",
                        "enum": ["react-tailwind", "html-css", "vue-tailwind"],
                        "default": "react-tailwind",
                        "description": "Target framework for the generated component.",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Optional absolute path to write the component file to.",
                    },
                },
                "required": ["image_path"],
            },
        ),
        Tool(
            name="ground",
            description=(
                "Grounding stage only. Returns JSON with detected UI elements "
                "(bounding boxes + semantic labels). Useful for inspection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Absolute path to the screenshot/mockup image.",
                    },
                },
                "required": ["image_path"],
            },
        ),
        Tool(
            name="plan",
            description=(
                "Planning stage only. Takes grounding JSON, returns a "
                "hierarchical layout plan as JSON."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "grounding_result": {
                        "type": "string",
                        "description": "GroundingResult serialized as a JSON string.",
                    },
                },
                "required": ["grounding_result"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global pipeline
    if pipeline is None:
        pipeline = Pipeline()
        log.info("Pipeline initialized")

    try:
        if name == "generate":
            result = pipeline.generate(
                arguments["image_path"],
                framework=arguments.get("framework", "react-tailwind"),
                output_path=arguments.get("output_path"),
            )
            text = json.dumps(result, ensure_ascii=False)
        elif name == "ground":
            grounding = pipeline.grounder.ground(arguments["image_path"])
            text = grounding.model_dump_json()
        elif name == "plan":
            plan = pipeline.planner.plan(arguments["grounding_result"])
            text = plan.model_dump_json()
        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=f"unknown tool: {name}")]
    except Exception as exc:  # never let one bad call crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=f"error: {exc!r}")]

    return [TextContent(type="text", text=text)]


async def main() -> None:
    log.info("starting screenshot-to-component MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
