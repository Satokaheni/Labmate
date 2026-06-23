"""MCP server for the critique skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Literal

import litellm
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from critique_skill import CritiqueSkill

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("critique.server")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")


class _GemmaClient:
    """Sync litellm shim that fulfils the CritiqueSkill lm_client interface."""

    def complete(self, prompt: str) -> str:
        r = litellm.completion(
            model="openai/gemma-4-31b",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
        )
        return r.choices[0].message.content or ""

    def chat(self, *, response_model, messages: list[dict], temperature: float = 0.1):
        import instructor
        client = instructor.from_litellm(litellm.completion)
        return client.chat.completions.create(
            model="openai/gemma-4-31b",
            messages=messages,
            response_model=response_model,
            temperature=temperature,
            api_base=GEMMA_BASE,
            api_key="not-needed",
        )


_skill = CritiqueSkill(_GemmaClient())
app: Server = Server("critique")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="critique",
            description=(
                "Run grounded critique-reflexion on code or writing output. "
                "Returns score (0–1), verdict (pass/revise/fail), and issue notes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "output": {"type": "string"},
                    "task": {"type": "string"},
                    "critique_type": {
                        "type": "string",
                        "enum": ["code", "writing"],
                        "default": "code",
                    },
                },
                "required": ["output", "task"],
            },
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name != "critique":
            raise ValueError(f"unknown tool: {name}")
        output: str = arguments["output"]
        task: str = arguments["task"]
        critique_type: Literal["code", "writing"] = arguments.get("critique_type", "code")

        def _run() -> dict:
            crit, _ = _skill.critique(output, task, critique_type)
            notes = "; ".join(i.explanation for i in crit.issues_found)
            return {"score": crit.score, "verdict": crit.verdict, "notes": notes}

        result = await asyncio.to_thread(_run)
        return [TextContent(type="text", text=json.dumps(result))]
    except Exception as exc:
        log.exception("critique tool failed")
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def main() -> None:
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
