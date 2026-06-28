"""MCP server for the academic-writing skill (stdio JSON-RPC).

Wraps the AcademicWritingSkill IMRaD pipeline (STORM pre-writing -> outline ->
per-section draft -> citation validation -> Chain-of-Density abstract -> style
transfer) as MCP tools so the skill-worker can discover and dispatch it like any
other skill. The pipeline logic lives in academic_writing_skill.py (independently
unit-tested); this module is the thin MCP transport + DSPy-LM wiring only.

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr (CLAUDE.md §1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict, is_dataclass
from typing import Any

import dspy
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from academic_writing_skill import AcademicWritingSkill, Ref

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("academic-writing.server")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
# CLAUDE.md §6: every llama.cpp request MUST set thinking_budget_tokens. These are
# writing/planning calls, so use a real (but bounded) budget. Override via env.
ACADEMIC_WRITING_THINKING_BUDGET = int(os.getenv("ACADEMIC_WRITING_THINKING_BUDGET", "2048"))


def _build_lm() -> dspy.LM:
    """A dspy.LM bound to the local llama.cpp OpenAI-compatible endpoint."""
    return dspy.LM(
        model="openai/gemma-4-31b",
        api_base=GEMMA_BASE,
        api_key="not-needed",
        extra_body={"thinking_budget_tokens": ACADEMIC_WRITING_THINKING_BUDGET},
    )


# Lazy single skill instance: constructing it calls dspy.configure() and builds
# the DSPy modules. Done on first use (not import) so a config hiccup surfaces as
# a per-call JSON error instead of an import failure that would stop the skill
# from registering at all (the very bug this server fixes).
_skill: AcademicWritingSkill | None = None


def _get_skill() -> AcademicWritingSkill:
    global _skill
    if _skill is None:
        _skill = AcademicWritingSkill(_build_lm())
    return _skill


def _to_refs(raw: Any) -> list[Ref]:
    """Convert a list of ref dicts (tool JSON) into Ref dataclasses."""
    refs: list[Ref] = []
    for r in raw or []:
        refs.append(Ref(
            id=str(r["id"]),
            title=str(r.get("title", "")),
            abstract=str(r.get("abstract", "")),
            bibtex=str(r.get("bibtex", "")),
            doi=r.get("doi"),
            arxiv_id=r.get("arxiv_id"),
        ))
    return refs


def _serialize(obj: Any) -> Any:
    """JSON-ready view of a pipeline return value (dataclass / pydantic / str / list)."""
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, list):
        return [_serialize(x) for x in obj]
    return obj


app: Server = Server("academic-writing")

_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "abstract": {"type": "string"},
        "bibtex": {"type": "string"},
        "doi": {"type": "string"},
        "arxiv_id": {"type": "string"},
    },
    "required": ["id", "title", "abstract", "bibtex"],
}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="research_topic",
            description=(
                "STORM pre-writing: generate diverse expert perspectives, interview each "
                "against the supplied references, and synthesize structured ResearchNotes "
                "(key_findings feed the outline)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "refs": {"type": "array", "items": _REF_SCHEMA},
                    "n_perspectives": {"type": "integer", "default": 3},
                },
                "required": ["topic", "refs"],
            },
        ),
        Tool(
            name="outline_skill",
            description=(
                "STORM two-stage outline: cluster references by IMRaD section and emit a "
                "canonical IMRaD scaffold. Returns an Outline of sections."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "refs": {"type": "array", "items": _REF_SCHEMA},
                },
                "required": ["topic", "refs"],
            },
        ),
        Tool(
            name="draft_section",
            description=(
                "Draft ONE IMRaD section (never the whole paper) grounded in the supplied "
                "refs and notes. Returns markdown/LaTeX with inline \\cite{key} citations."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "section_name": {"type": "string"},
                    "refs": {"type": "array", "items": _REF_SCHEMA},
                    "notes": {"type": "string"},
                },
                "required": ["section_name", "refs", "notes"],
            },
        ),
        Tool(
            name="validate_citations",
            description=(
                "Deterministic-first citation validation cascade (DOI->Crossref, arXiv, "
                "Semantic Scholar, LLM fallback). Returns one CitationResult per entry; "
                "callers MUST filter to valid=True before including a citation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "bibtex_entries": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["bibtex_entries"],
            },
        ),
        Tool(
            name="chain_of_density",
            description=(
                "Chain-of-Density abstract: start sparse, add salient entities each "
                "iteration while holding word count == target_words. Returns the abstract."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "target_words": {"type": "integer"},
                    "iterations": {"type": "integer", "default": 3},
                },
                "required": ["text", "target_words"],
            },
        ),
        Tool(
            name="style_transfer",
            description=(
                "Single-pass text style transfer (e.g. casual->formal) that preserves all "
                "\\cite{}/\\ref{}/numeric tokens verbatim. Returns the restyled text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source_style": {"type": "string", "default": "casual"},
                    "target_style": {"type": "string", "default": "formal"},
                },
                "required": ["text"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        def _run() -> Any:
            skill = _get_skill()
            if name == "research_topic":
                return skill.research_topic(
                    arguments["topic"], _to_refs(arguments["refs"]),
                    int(arguments.get("n_perspectives", 3)),
                )
            if name == "outline_skill":
                return skill.outline_skill(arguments["topic"], _to_refs(arguments["refs"]))
            if name == "draft_section":
                return skill.draft_section(
                    arguments["section_name"], _to_refs(arguments["refs"]), arguments["notes"],
                )
            if name == "validate_citations":
                return skill.validate_citations(list(arguments["bibtex_entries"]))
            if name == "chain_of_density":
                return skill.chain_of_density(
                    arguments["text"], int(arguments["target_words"]),
                    int(arguments.get("iterations", 3)),
                )
            if name == "style_transfer":
                return skill.style_transfer(
                    arguments["text"],
                    arguments.get("source_style", "casual"),
                    arguments.get("target_style", "formal"),
                )
            raise ValueError(f"unknown tool: {name}")

        result = await asyncio.to_thread(_run)
        return [TextContent(type="text", text=json.dumps({"result": _serialize(result)}, default=str))]
    except Exception as exc:
        log.exception("academic-writing tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def main() -> None:
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
