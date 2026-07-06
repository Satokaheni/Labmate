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
from critique_skill import CritiqueSkill
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Shared resilient model-call path (failover + transient retry); falls back to raw
# litellm.completion when the skill runs standalone (no repo on PYTHONPATH).
try:
    from services.model_client import resilient_completion as _completion
except ImportError:  # pragma: no cover — standalone skill run
    _completion = litellm.completion

logging.basicConfig(
    stream=sys.stderr, level=logging.INFO, format="%(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("critique.server")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")

# CLAUDE.md §6: every llama.cpp request MUST set thinking_budget_tokens explicitly.
# Post-April-2026 builds default it to INT_MAX when unset, which makes the model
# reason unbounded and the critique reflexion loop (up to MAX_ITERS × several
# calls) blow past the SkillRouter dispatch timeout (~60s) — the observed hang.
# These calls are mechanical (generate/verify/score), so a small budget keeps
# each call fast and deterministic. Override via CRITIQUE_THINKING_BUDGET.
CRITIQUE_THINKING_BUDGET = int(os.getenv("CRITIQUE_THINKING_BUDGET", "512"))
# instructor re-asks the model whenever the structured Critique fails schema
# validation. The Q4 model fails this complex schema often, so an unbounded
# (default) retry count fanned a single evaluator call out to ~6 model calls
# (~55s). Cap it: a couple of attempts, then let the gate fall open gracefully
# (server returns its error → verify treats the artifact as passed). Override
# via CRITIQUE_MAX_RETRIES.
CRITIQUE_MAX_RETRIES = int(os.getenv("CRITIQUE_MAX_RETRIES", "2"))


class _GemmaClient:
    """Sync litellm shim that fulfils the CritiqueSkill lm_client interface."""

    def complete(self, prompt: str) -> str:
        r = _completion(
            model="openai/gemma-4-31b",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking_budget_tokens": CRITIQUE_THINKING_BUDGET},
        )
        return r.choices[0].message.content or ""

    def chat(self, *, response_model, messages: list[dict], temperature: float = 0.1):
        import instructor

        client = instructor.from_litellm(_completion)
        return client.chat.completions.create(
            model="openai/gemma-4-31b",
            messages=messages,
            response_model=response_model,
            temperature=temperature,
            api_base=GEMMA_BASE,
            api_key="not-needed",
            max_retries=CRITIQUE_MAX_RETRIES,
            extra_body={"thinking_budget_tokens": CRITIQUE_THINKING_BUDGET},
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
            # Single-pass scoring: the verify gate needs only score/verdict/notes,
            # not the full (slow) reflexion+CoVe loop. See CritiqueSkill.score_once.
            crit = _skill.score_once(output, task, critique_type)
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
