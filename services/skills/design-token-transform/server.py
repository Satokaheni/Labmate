import asyncio
import json
import logging
import sys

from dotenv import load_dotenv
from mcp.server import Server

load_dotenv()
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# CRITICAL: stderr only. Configure before importing local modules that log.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("design-token-transform.server")

from figma_client import FigmaClient, TokenSet  # noqa: E402
from transformer import TokenTransformer        # noqa: E402

app: Server = Server("design-token-transform")
transformer = TokenTransformer()


@app.list_tools()
async def list_tools() -> list[Tool]:
    format_prop = {
        "type": "string",
        "enum": ["tailwind", "css-vars", "shadcn"],
        "default": "tailwind",
        "description": "Target format for the transformed tokens.",
    }
    return [
        Tool(
            name="extract",
            description=(
                "Fetch design tokens from a Figma file via the REST API. "
                "Returns a TokenSet as JSON."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "figma_file_key": {
                        "type": "string",
                        "description": "Figma file key (from the file URL).",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Optional node id to scope extraction.",
                    },
                },
                "required": ["figma_file_key"],
            },
        ),
        Tool(
            name="transform",
            description=(
                "Transform a raw TokenSet JSON string into a target format "
                "(tailwind | css-vars | shadcn). Returns the format string."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tokens_json": {
                        "type": "string",
                        "description": "A TokenSet serialized as JSON.",
                    },
                    "format": format_prop,
                },
                "required": ["tokens_json"],
            },
        ),
        Tool(
            name="extract_and_transform",
            description=(
                "Extract tokens from a Figma file and transform them in one call. "
                "Optionally writes the result to output_path. Returns the format string."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "figma_file_key": {"type": "string"},
                    "format": format_prop,
                    "output_path": {
                        "type": "string",
                        "description": "Optional path to write the transformed output.",
                    },
                },
                "required": ["figma_file_key"],
            },
        ),
    ]


_client: FigmaClient | None = None


def _get_client() -> FigmaClient:
    global _client
    if _client is None:
        _client = FigmaClient()  # raises if FIGMA_ACCESS_TOKEN missing
    return _client


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "extract":
            token_set = await _get_client().get_file_tokens(
                arguments["figma_file_key"], arguments.get("node_id")
            )
            text = token_set.model_dump_json()

        elif name == "transform":
            token_set = TokenSet.model_validate_json(arguments["tokens_json"])
            text = transformer.transform(
                token_set, arguments.get("format", "tailwind")
            )

        elif name == "extract_and_transform":
            token_set = await _get_client().get_file_tokens(
                arguments["figma_file_key"]
            )
            text = transformer.transform(
                token_set, arguments.get("format", "tailwind")
            )
            out_path = arguments.get("output_path")
            if out_path:
                with open(out_path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                log.info("wrote transformed tokens to %s", out_path)

        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=f"unknown tool: {name}")]

    except Exception as exc:  # never let one bad call crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=f"error: {exc!r}")]

    return [TextContent(type="text", text=text)]


async def main() -> None:
    log.info("starting design-token-transform MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
