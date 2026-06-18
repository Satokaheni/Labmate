"""MCP stdio server for the paper_to_slides skill.

CRITICAL: stdout is the JSON-RPC 2.0 channel. NEVER print() anywhere.
All logging is configured to sys.stderr before pipeline imports.
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

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("paper-to-slides.server")

from compile_loop import CompileLoop          # noqa: E402
from outline_planner import OutlinePlanner     # noqa: E402
from slide_generator import SlideGenerator     # noqa: E402
from speaker_notes import SpeakerNotes         # noqa: E402

app: Server = Server("paper-to-slides")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate",
            description="Full pipeline: parsed paper -> Beamer/Marp deck + PDF. "
                        "Returns JSON with tex_path, pdf_path, notes_path, "
                        "slide_count, compile_success.",
            inputSchema={
                "type": "object",
                "properties": {
                    "parsed_paper_path": {"type": "string",
                        "description": "Absolute path to pdf-parse JSON output."},
                    "talk_duration_min": {"type": "integer", "default": 20},
                    "output_format": {"type": "string",
                        "enum": ["beamer", "marp"], "default": "beamer"},
                    "include_notes": {"type": "boolean", "default": False},
                },
                "required": ["parsed_paper_path"],
            },
        ),
        Tool(
            name="generate_outline",
            description="Outline only. Returns the JSON PresentationBlueprint.",
            inputSchema={
                "type": "object",
                "properties": {
                    "parsed_paper_path": {"type": "string"},
                    "talk_duration_min": {"type": "integer", "default": 20},
                },
                "required": ["parsed_paper_path"],
            },
        ),
        Tool(
            name="compile_tex",
            description="Compile + self-correct an existing .tex. Returns JSON with "
                        "pdf_path, success, attempts, final_error.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tex_path": {"type": "string"},
                    "max_retries": {"type": "integer", "default": 5},
                },
                "required": ["tex_path"],
            },
        ),
    ]


def _output_dir_for(parsed_paper_path: str) -> str:
    base = os.getenv("PAPER_TO_SLIDES_OUTPUT_DIR")
    if base:
        return base
    return os.path.join(os.path.dirname(os.path.abspath(parsed_paper_path)), "slides")


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "generate_outline":
            paper = json.loads(open(arguments["parsed_paper_path"]).read())
            bp = OutlinePlanner().plan(
                paper, arguments.get("talk_duration_min", 20))
            text = json.dumps(bp.to_dict(), ensure_ascii=False)

        elif name == "compile_tex":
            res = CompileLoop().compile(
                arguments["tex_path"], arguments.get("max_retries", 5))
            text = json.dumps(res.to_dict(), ensure_ascii=False)

        elif name == "generate":
            text = _run_generate(arguments)

        else:
            log.error("unknown tool: %s", name)
            return [TextContent(type="text", text=f"unknown tool: {name}")]
    except Exception as exc:  # never crash the stdio server
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=f"error: {exc!r}")]

    return [TextContent(type="text", text=text)]


def _run_generate(args: dict) -> str:
    parsed_path = args["parsed_paper_path"]
    duration = args.get("talk_duration_min", 20)
    output_format = args.get("output_format", "beamer")
    include_notes = args.get("include_notes", False)

    paper = json.loads(open(parsed_path).read())
    out_dir = _output_dir_for(parsed_path)

    # [1] outline
    bp = OutlinePlanner().plan(paper, duration)
    # [3] slide source
    src_path = SlideGenerator().generate(bp, out_dir, output_format)

    # [4] compile (Beamer only; Marp is not compiled to PDF here)
    if output_format == "beamer":
        result = CompileLoop().compile(src_path)
        pdf_path = result.pdf_path
        compile_success = result.success
    else:
        pdf_path = None
        compile_success = True  # Marp .md needs no LaTeX compile

    # [5] optional speaker notes
    notes_path = None
    if include_notes:
        notes_path = SpeakerNotes().generate(bp, out_dir)

    return json.dumps({
        "tex_path": src_path,
        "pdf_path": pdf_path,
        "notes_path": notes_path,
        "slide_count": len(bp.slides),
        "compile_success": compile_success,
    }, ensure_ascii=False)


async def main() -> None:
    log.info("starting paper-to-slides MCP server")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
