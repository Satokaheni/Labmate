import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from localizer import FaultLocalizer

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("fault-localize")

app = Server("repo-fault-localize")

_LOCALIZERS: dict[str, FaultLocalizer] = {}


def _get_localizer(repo_path: str) -> FaultLocalizer:
    loc = _LOCALIZERS.get(repo_path)
    if loc is None:
        loc = FaultLocalizer(repo_path)
        _LOCALIZERS[repo_path] = loc
    return loc


def _jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in rows)


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="locate_files",
             description="Top-k files most likely to need edits for a bug. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"issue": {"type": "string"},
                                         "repo_path": {"type": "string"},
                                         "top_k": {"type": "integer", "default": 5}},
                          "required": ["issue", "repo_path"]}),
        Tool(name="locate_symbols",
             description="Functions/classes in a file most likely to contain the bug. JSONL.",
             inputSchema={"type": "object",
                          "properties": {"issue": {"type": "string"},
                                         "file": {"type": "string"},
                                         "repo_path": {"type": "string"}},
                          "required": ["issue", "file", "repo_path"]}),
        Tool(name="suggest_edit_sites",
             description="Precise edit line-ranges within given symbols. Returns JSONL.",
             inputSchema={"type": "object",
                          "properties": {"issue": {"type": "string"},
                                         "file": {"type": "string"},
                                         "repo_path": {"type": "string"},
                                         "symbols": {"type": "array",
                                                     "items": {"type": "string"}}},
                          "required": ["issue", "file", "symbols", "repo_path"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        repo_path = arguments["repo_path"]
        loc = _get_localizer(repo_path)
        if name == "locate_files":
            rows = loc.locate_files(arguments["issue"],
                                    int(arguments.get("top_k", 5)))
        elif name == "locate_symbols":
            rows = loc.locate_symbols(arguments["issue"], arguments["file"])
        elif name == "suggest_edit_sites":
            rows = loc.suggest_edit_sites(arguments["issue"], arguments["file"],
                                          list(arguments["symbols"]))
        else:
            return [TextContent(type="text",
                                text=json.dumps({"error": f"unknown tool {name}"}))]
    except Exception as exc:  # noqa: BLE001 - surface as JSON, never crash the stream
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    return [TextContent(type="text", text=_jsonl(rows))]


async def _run() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main() -> None:
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
