"""dataset-generation skill logic: generate synthetic training data via Gemma 4 31B.

CRITICAL: never write to stdout. All logging goes to stderr.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("dataset-generation")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")


async def _gemma(prompt: str, budget: int = 512) -> str:
    r = await litellm.acompletion(
        model="openai/gemma-4-31b",
        api_base=GEMMA_BASE,
        api_key="not-needed",
        messages=[{"role": "user", "content": prompt}],
        extra_body={"thinking_budget_tokens": budget},
    )
    return r.choices[0].message.content or ""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


async def generate_from_seeds(
    seeds: list[str],
    template: str,
    n_per_seed: int = 3,
    output_format: str = "instruction-response",
) -> dict:
    """Generate synthetic training examples from seed prompts.

    Args:
        seeds: seed prompts/instructions to expand
        template: describes the desired format (e.g. 'empathetic dialogue response')
        n_per_seed: number of synthetic examples per seed
        output_format: 'instruction-response' | 'preference' | 'chat'

    Returns:
        {examples: [{seed, generated: [{input, output}]}, ...]}
    """
    all_examples: list[dict] = []
    for seed in seeds:
        prompt = (
            f"You are generating synthetic training data. Template: {template}\n"
            f"Format: {output_format}\n\n"
            f"Seed: {seed}\n\n"
            f"Generate exactly {n_per_seed} diverse training examples based on this seed. "
            f"Each example should have 'input' and 'output' fields. "
            f"Respond ONLY with a JSON array: "
            f'[{{"input": "...", "output": "..."}}]'
        )
        try:
            raw = await _gemma(prompt)
            parsed = json.loads(_strip_fences(raw))
            generated = parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, Exception) as exc:
            log.warning("generate_from_seeds failed for seed %r: %s", seed[:50], exc)
            generated = []
        all_examples.append({"seed": seed, "generated": generated})
    total = sum(len(e["generated"]) for e in all_examples)
    return {"examples": all_examples, "total_generated": total}


def format_as_jsonl(examples: list[dict], output_path: str) -> dict:
    """Write examples from generate_from_seeds output to a JSONL file.

    Flattens {seed, generated} records into one JSON object per line.
    Returns {ok, path, count}.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        with out.open("w", encoding="utf-8") as f:
            for record in examples:
                seed = record.get("seed", "")
                for item in record.get("generated", []):
                    line = {"seed": seed, **item}
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
                    count += 1
        return {"ok": True, "path": str(out), "count": count}
    except OSError as exc:
        return {"ok": False, "path": str(out), "count": 0, "error": str(exc)}


def validate_coverage(examples: list[dict], requirements: list[str]) -> dict:
    """Check that generated examples cover all stated requirements.

    Lexical check: each requirement must appear in at least one generated
    input or output. Returns {covered, gaps, coverage_pct}.
    """
    all_text = " ".join(
        f"{item.get('input', '')} {item.get('output', '')}"
        for record in examples
        for item in record.get("generated", [])
    ).lower()
    covered = [r for r in requirements if r.lower() in all_text]
    gaps = [r for r in requirements if r.lower() not in all_text]
    total = len(requirements)
    pct = (len(covered) / total) if total else 1.0
    return {"covered": covered, "gaps": gaps, "coverage_pct": pct}
