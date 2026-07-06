#!/usr/bin/env python3
"""Routing-latency fix probe — does the max_tokens cap actually clamp the pre-flight ramble?

The routing-latency fix (commit aee3383) added `max_tokens` caps to the two pre-flight
model calls that run BEFORE any answer:
  - assess_ambiguity (graph.py, via CodingOrchestrator.architect)  -> ASSESS_MAX_TOKENS=512
  - skill routing    (skill_router.py, _sample_select)            -> ROUTE_MAX_TOKENS=256
Both emit small structured output (JSON / a load_skill tool call) but had NO cap, so the
model could ramble to thousands of content tokens (~3.8k observed live) — the dominant
pre-answer latency.

A trivial prompt ("What is 2+2?") can't validate this: it scores ~0.0 ambiguity and emits
~50-90 tokens naturally, far under the caps, so there is nothing to clamp. The ramble is a
TAIL event that only fires on AMBIGUOUS prompts (the assess prompt itself scores
"make it better" -> 0.85), where the model enumerates assumptions + a blocking question.

This probe hits the EXACT two call seams with ambiguous prompts across N trials and reads
`response.usage.completion_tokens` per call directly — the precise quantity max_tokens caps
— so we get a DISTRIBUTION (min/median/max/p95) instead of a single dice roll, and we
sidestep the two confounders a full-stack A/B hits on this host:
  (1) logs have no per-call token labels -> can't attribute which call rambled;
  (2) on a windowed (non --swa-full) server, ~3k prompt-reprocessing tokens/call swamp the
      wall-clock, hiding a real generation clamp inside noise.

It also VERIFIES the capped output still PARSES (valid ambiguity JSON / a valid load_skill
tool call) — so "the cap silently breaks routing" can't hide behind a nice token number.

Run on the host (no config changes, commits nothing):
    PYTHONPATH=. python eval/routing_latency_probe.py --trials 8
    PYTHONPATH=. python eval/routing_latency_probe.py --trials 8 --base-url http://localhost:8000/v1

Interpretation:
  - uncapped p95/max clears the cap AND capped never exceeds it  -> the guard BITES (fix works)
  - even uncapped never approaches the cap on this model         -> ramble doesn't reproduce here;
                                                                    cap is confirmed-harmless insurance
  - any capped run fails to parse                                -> REGRESSION: cap is truncating
                                                                    the structured output
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from pathlib import Path

from services.model_client import acompletion_with_failover, resolve_bases

MODEL = "openai/gemma-4-31b"

# Ambiguous prompts — high-ambiguity by the assess rubric (undefined referent / no
# deliverable / no success criteria), so the assess call should enumerate assumptions +
# a blocking_question. These are exactly the shape that made it ramble live.
AMBIGUOUS_PROMPTS = [
    "Improve it.",
    "Make this better.",
    "Fix the thing.",
    "Improve performance.",
    "Refactor this to be cleaner.",
    "Help me with the thing we discussed.",
]

# A control the fix should NOT affect (scores ~0.0; emits little regardless of the cap).
CONTROL_PROMPT = "What is 2+2?"


# ---- assess prompt: verbatim SNAPSHOT of graph.py::assess_ambiguity (no client workspace,
# no prior-conversation block — the empty-context case, which is what a fresh goal sees).
# If graph.py's template changes, refresh this string. -------------------------------------
def build_assess_prompt(goal: str) -> str:
    return (
        "You are triaging a task before an autonomous agent executes it.\n"
        f"TASK: {goal}\n\n"
        "List the assumptions an agent must make to act on this as written. "
        "Then rate overall ambiguity from 0.0 (fully specified) to 1.0 (critically "
        "underspecified).\n\n"
        "The score measures ONE thing only: is the CORE objective underspecified? "
        "Score it on whether the agent can tell WHAT to do, not on whether every "
        "execution detail is pinned down.\n\n"
        "Score HIGH (>= 0.6, typically 0.7-0.9) ONLY when the CORE task is "
        "underspecified, i.e. it has any of:\n"
        '  - an undefined referent (e.g. "it", "the thing", "this") with no '
        "antecedent naming the actual object,\n"
        "  - no concrete deliverable (you cannot tell what artifact to produce),\n"
        "  - undefined success criteria (you'd have to guess what 'done' means, or "
        "no target/metric is given for a 'make it better'-style request),\n"
        "  - essential information missing without which you cannot meaningfully "
        "begin.\n\n"
        "Score LOW (~0.0-0.3) when the core objective is clear and actionable, EVEN "
        "IF minor execution parameters are unstated. Unstated MINOR parameters are "
        "NOT ambiguity — the agent should assume reasonable defaults and proceed. "
        "These do NOT raise the score:\n"
        "  - output format (list vs table vs JSON),\n"
        "  - count or quantity (how many results/items),\n"
        "  - which specific library, method, or algorithm to use,\n"
        "  - styling, naming, or other cosmetic choices.\n\n"
        "Examples:\n"
        '  "make it better" -> 0.85  (undefined referent, no deliverable)\n'
        '  "fix the thing" -> 0.9  (undefined referent, no deliverable)\n'
        '  "improve performance" (no system/target/metric) -> 0.75  (undefined '
        "success criteria)\n"
        '  "search the Hugging Face Hub for emotion classification datasets" -> 0.1 '
        " (clear objective; format and count are defaults, not ambiguity)\n"
        '  "write a python function that reverses a string" -> 0.1  (the '
        "implementation method is just a default to pick)\n"
        '  "what is 2+2?" -> 0.0\n'
        '  "add a docstring to the reverse_string function in utils.py" -> 0.1\n\n'
        'When ambiguity is high, set "blocking_question" to the single most useful '
        "question to ask the user; otherwise leave it empty.\n"
        "Respond as JSON: "
        '{"assumptions": ["..."], "ambiguity": 0.0, "blocking_question": "" }'
    )


def _completion_tokens(resp) -> int | None:
    usage = getattr(resp, "usage", None)
    ct = getattr(usage, "completion_tokens", None) if usage else None
    try:
        return int(ct) if ct is not None else None
    except (TypeError, ValueError):
        return None


def _assess_parses(resp) -> bool:
    """The capped assess output must still be valid ambiguity JSON with a float score."""
    try:
        content = resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return False
    t = content.strip()
    if t.startswith("```json"):
        t = t[7:]
    elif t.startswith("```"):
        t = t[3:]
    if t.endswith("```"):
        t = t[:-3]
    try:
        out = json.loads(t.strip())
        return isinstance(out, dict) and isinstance(float(out.get("ambiguity", "x")), float)
    except (json.JSONDecodeError, ValueError, TypeError):
        return False


def _route_parses(resp) -> bool:
    """The capped routing output must still yield a well-formed load_skill tool call
    (or a clean no-call, which is valid — the model may decline). A truncated tool call
    with unparseable arguments is the failure we're guarding against."""
    try:
        msg = resp.choices[0].message
    except (AttributeError, IndexError):
        return False
    tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:
        return True  # a clean decline is a valid, non-truncated outcome
    for tc in tool_calls:
        func = getattr(tc, "function", None)
        if func is None or getattr(func, "name", None) != "load_skill":
            continue
        args = getattr(func, "arguments", "{}")
        if isinstance(args, str):
            try:
                json.loads(args)
            except json.JSONDecodeError:
                return False  # truncated / malformed args -> the regression
    return True


async def _run_assess(bases, goal: str, cap: int | None):
    kwargs = {"max_tokens": cap} if cap is not None else {}
    return await acompletion_with_failover(
        model=MODEL,
        bases=bases,
        api_key="not-needed",
        messages=[{"role": "user", "content": build_assess_prompt(goal)}],
        extra_body={"thinking_budget_tokens": int(os.getenv("ASSESS_THINKING_BUDGET", "384"))},
        **kwargs,
    )


async def _run_route(bases, catalog: str, schema: dict, task: str, cap: int | None):
    kwargs = {"max_tokens": cap} if cap is not None else {}
    directive = (
        "You are a skill router. If ANY available skill is relevant to the "
        "user's task, you MUST call load_skill with that skill's name. Only "
        "decline to call a tool if truly no skill fits."
    )
    return await acompletion_with_failover(
        model=MODEL,
        bases=bases,
        api_key="not-needed",
        messages=[
            {"role": "system", "content": f"{directive}\n\n{catalog}"},
            {"role": "user", "content": task},
        ],
        tools=[schema],
        tool_choice="auto",
        extra_body={"thinking_budget_tokens": 0},
        **kwargs,
    )


def _summarize(label: str, cap: int | None, tokens: list[int], parse_fails: int):
    cap_str = "uncapped" if cap is None else f"cap={cap}"
    if not tokens:
        print(f"  {label:<10} {cap_str:<11} — no data (all calls failed)")
        return
    tokens_sorted = sorted(tokens)
    p95 = tokens_sorted[min(len(tokens_sorted) - 1, int(round(0.95 * (len(tokens_sorted) - 1))))]
    over = sum(1 for t in tokens if cap is not None and t >= cap)
    flags = []
    if cap is None and max(tokens) >= 256:
        flags.append("RAMBLES")  # uncapped tail is well past the routing cap
    if cap is not None and over:
        flags.append(f"{over} at/over cap")  # cap is doing work (or clamping)
    if parse_fails:
        flags.append(f"⚠ {parse_fails} PARSE-FAIL")
    print(
        f"  {label:<10} {cap_str:<11} "
        f"n={len(tokens):<3} min={min(tokens):<5} med={int(statistics.median(tokens)):<5} "
        f"p95={p95:<5} max={max(tokens):<5}  {'  '.join(flags)}"
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=8, help="trials per (prompt, cap)")
    ap.add_argument("--base-url", default=os.getenv("GEMMA_BASE", "http://localhost:8000/v1"))
    ap.add_argument("--assess-cap", type=int, default=int(os.getenv("ASSESS_MAX_TOKENS", "512")))
    ap.add_argument("--route-cap", type=int, default=int(os.getenv("ROUTE_MAX_TOKENS", "256")))
    ap.add_argument("--skip-routing", action="store_true", help="assess section only")
    args = ap.parse_args()

    bases = resolve_bases(args.base_url, os.getenv("LABMATE_FALLBACK_BASES"))
    print(
        f"probe: bases={bases} trials={args.trials} "
        f"assess_cap={args.assess_cap} route_cap={args.route_cap}\n"
    )

    # ---------------- ASSESS (the documented ~3.8k ramble source) ----------------
    print("=== assess_ambiguity call (ambiguous prompts should ramble uncapped) ===")
    for label, prompts, cap in (
        ("ambiguous", AMBIGUOUS_PROMPTS, None),
        ("ambiguous", AMBIGUOUS_PROMPTS, args.assess_cap),
        ("control", [CONTROL_PROMPT], None),
    ):
        tokens: list[int] = []
        parse_fails = 0
        for goal in prompts:
            for _ in range(args.trials):
                try:
                    resp = await _run_assess(bases, goal, cap)
                except Exception as exc:  # noqa: BLE001
                    print(f"    call failed ({goal!r}, cap={cap}): {exc}")
                    continue
                ct = _completion_tokens(resp)
                if ct is not None:
                    tokens.append(ct)
                if cap is not None and not _assess_parses(resp):
                    parse_fails += 1
        _summarize(label, cap, tokens, parse_fails)
    print()

    # ---------------- ROUTING (best-effort — needs the real skill catalog) ----------------
    if args.skip_routing:
        print("=== skill routing call: SKIPPED (--skip-routing) ===")
        return
    print("=== skill routing call (_sample_select seam) ===")
    try:
        from services.skill_runner.skill_runner import SkillRunner

        skills_root = Path(__file__).resolve().parent.parent / "services" / "skills"
        runner = SkillRunner(roots=[skills_root])
        runner.discover()
        catalog = runner.catalog_prompt()
        schema = runner.tool_schema()
        print(f"  (catalog: {len(runner.catalog)} skills)")
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIPPED — could not build SkillRunner: {exc}")
        return

    for label, cap in (("ambiguous", None), ("ambiguous", args.route_cap)):
        tokens = []
        parse_fails = 0
        for task in AMBIGUOUS_PROMPTS:
            for _ in range(args.trials):
                try:
                    resp = await _run_route(bases, catalog, schema, task, cap)
                except Exception as exc:  # noqa: BLE001
                    print(f"    call failed ({task!r}, cap={cap}): {exc}")
                    continue
                ct = _completion_tokens(resp)
                if ct is not None:
                    tokens.append(ct)
                if cap is not None and not _route_parses(resp):
                    parse_fails += 1
        _summarize(label, cap, tokens, parse_fails)


if __name__ == "__main__":
    asyncio.run(main())
