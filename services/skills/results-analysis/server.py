"""MCP server for the results-analysis skill (stdio JSON-RPC).

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

# Load results_analysis module from the same directory
import importlib.util
_module_path = Path(__file__).parent / "results_analysis.py"
spec = importlib.util.spec_from_file_location("results_analysis", _module_path)
results_analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(results_analysis)

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("results-analysis.server")
app: Server = Server("results-analysis")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="profile_results",
             description="Descriptive stats + auto-detected metric columns.",
             inputSchema={"type": "object",
                          "properties": {"file_path": {"type": "string"}},
                          "required": ["file_path"]}),
        Tool(name="compare_runs",
             description="Significance test + bootstrap CI across configurations.",
             inputSchema={"type": "object", "properties": {
                 "file_path": {"type": "string"},
                 "group_col": {"type": "string"},
                 "metric_col": {"type": "string"}},
                 "required": ["file_path", "group_col", "metric_col"]}),
        Tool(name="make_figures",
             description="Publication-ready figures + markdown/LaTeX tables.",
             inputSchema={"type": "object", "properties": {
                 "file_path": {"type": "string"},
                 "metric_cols": {"type": "array", "items": {"type": "string"}},
                 "output_dir": {"type": "string"}},
                 "required": ["file_path", "metric_cols", "output_dir"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "profile_results":
            result = results_analysis.profile_results(arguments["file_path"])
        elif name == "compare_runs":
            result = results_analysis.compare_runs(
                arguments["file_path"], arguments["group_col"], arguments["metric_col"])
        elif name == "make_figures":
            result = results_analysis.make_figures(
                arguments["file_path"], arguments["metric_cols"], arguments["output_dir"])
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
