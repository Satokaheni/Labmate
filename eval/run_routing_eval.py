#!/usr/bin/env python3
"""
run_routing_eval.py — score skill routing and hosted-tool auto-selection.

Mirrors skill_router.select() as described in the harness audit: same
catalog_prompt, a single load_skill tool whose `name` is an enum over the
catalog, tool_choice="auto", thinking_budget_tokens=0, and SELECT_ATTEMPTS
resampling. Reports overall / per-cluster / per-skill accuracy, a confusion list,
the false-positive rate on negative cases, and per-case stability across repeats.

Also scores hosted MCP-tool auto-selection: given FLAT tool schemas
(e.g. mcp__ast-ts-refactor__find_references), the model calls tool_choice="auto"
without skill names, and we check whether the called tool's function.name matches
the expected one.

  python eval/run_routing_eval.py \\
    --eval eval/routing_eval.jsonl \\
    --skills-dir services/skills \\
    --base-url http://localhost:8000/v1 \\
    --model gemma-4-12b \\
    --repeats 3 \\
    --report eval/reports/

  # Hosted-tool scoring (requires a hosted_tools.json file):
  python eval/run_routing_eval.py \\
    --eval eval/fixtures/hosted_routing.example.jsonl \\
    --hosted-tools eval/fixtures/hosted_tools.example.json \\
    --base-url http://localhost:8000/v1 \\
    --model gemma-4-12b \\
    --repeats 3 \\
    --report eval/reports/

PRODUCTION FIDELITY: route_one() below is a faithful copy of select(). For a
true production test, replace its body with a call to your real
services.orchestrator.skill_router.select (see the SEAM marker).
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from eval.metric_meaning import routing_header_lines

try:
    import yaml
except ImportError:
    yaml = None


def get_token_count(text: str) -> int:
    """Get token count for text using Gemma tokenizer. Falls back to char count on error."""
    try:
        from services.memory.tokenizer import token_count

        return token_count(text)
    except Exception:  # noqa: BLE001
        # Fallback: rough estimate using char count
        return len(text) // 4


SELECT_SYSTEM = (
    "You are a skill router. Call load_skill with the ONE skill whose PURPOSE "
    "matches the user's underlying GOAL. Judge by the goal, not by surface "
    "keywords: e.g. 'make slides from this PDF' is a slide-making goal, not a "
    "PDF-parsing goal. Route — do NOT decline — whenever the task asks you to "
    "critique, review, analyze, localize, generate, refactor, test, document, or "
    "fetch external information, because a dedicated skill exists for those. If "
    "the task needs external or live information — anything mentioning the web, "
    "'online', 'look up', 'search for', or the 'latest'/'current' state of "
    "something — route to the web-search skill. DECLINE (do not call load_skill) "
    "ONLY when the task is trivially self-contained and needs no skill: basic "
    "arithmetic, a simple rephrase/reword of provided text, or a "
    "general-knowledge factual question. Examples: 'What is 17 times 23?' -> "
    "decline; 'Rephrase this to sound more formal.' -> decline; 'Give me a UX "
    "critique of this component.' -> load_skill(design-critique); 'Look up the "
    "latest changelog online.' -> load_skill(web-search); 'Make conference "
    "slides from this paper PDF.' -> load_skill(paper-to-slides)."
)

HOSTED_SYSTEM = (
    "You are a tool selector. Given a user task and a set of available tools, "
    "call the single best tool for the task. Choose based on the tool's description "
    "and your understanding of what the user is trying to accomplish. Only call a tool "
    "if one is genuinely relevant.\n\n"
    "The user's workspace root is: /workspace/project\n"
    "When a tool requires an ABSOLUTE path (e.g. a tsconfig or a file path), construct it by joining this "
    "root with the relative path (e.g. /workspace/project/tsconfig.json). Always provide such required "
    "absolute-path arguments rather than declining to call a tool."
)


# --------------------------------------------------------------------------- #
# Catalog (same loader contract as extend_eval.py)
# --------------------------------------------------------------------------- #
def load_catalog(skills_dir, catalog_json) -> dict[str, str]:
    if catalog_json:
        data = json.loads(Path(catalog_json).read_text())
        return {k: collapse(v) for k, v in data.items()}
    if yaml is None:
        sys.exit("pyyaml required to parse SKILL.md (pip install pyyaml)")
    cat = {}
    for md in sorted(Path(skills_dir).glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if not m:
            continue
        meta = yaml.safe_load(m.group(1)) or {}
        name = meta.get("name") or md.parent.name
        if meta.get("description"):
            cat[name] = collapse(meta["description"])
    if not cat:
        sys.exit(f"No skills found under {skills_dir}")
    return cat


def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def catalog_prompt(catalog: dict[str, str], mode: str = "full") -> str:
    """Render a skill catalog as a newline-separated list.

    Delegates per-line rendering to ``render_catalog_line`` from
    ``services.skill_runner.skill_runner`` — that function is the single source
    of truth so the eval and production renderers can never drift.

    Modes: 'full' | 'terse' | 'names'  (see render_catalog_line for semantics).
    """
    from services.skill_runner.skill_runner import render_catalog_line

    return "\n".join(render_catalog_line(n, d, mode) for n, d in catalog.items())


def load_skill_tool(catalog: dict[str, str]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "Load the single most relevant skill for the task.",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": list(catalog)}},
                    "required": ["name"],
                },
            },
        }
    ]


# --------------------------------------------------------------------------- #
# Routing — SEAM: swap this for the real select() for production fidelity
# --------------------------------------------------------------------------- #
async def route_one(
    client, model, task, catalog, tools, system_prompt, attempts, temperature
) -> str | None:
    """Return the selected skill name, or None if the router declined. Mirrors
    select(): up to `attempts` independent samples at thinking_budget=0, accept
    the first valid enum name."""
    for _ in range(attempts):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ],
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
                extra_body={"thinking_budget_tokens": 0},
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ! request error: {e}", file=sys.stderr)
            continue
        calls = resp.choices[0].message.tool_calls
        if not calls:
            continue  # declined this sample → resample
        try:
            name = json.loads(calls[0].function.arguments).get("name")
        except (json.JSONDecodeError, AttributeError):
            continue
        if name in catalog:
            return name
    return None


async def route_hosted_one(
    client,
    model,
    task,
    hosted_tools,
    system_prompt,
    attempts,
    temperature,
    call_model: Callable | None = None,
) -> str | None:
    """Return the selected hosted tool name (e.g. 'mcp__ast-ts-refactor__find_references'),
    or None if declined. Scores flat tool auto-selection.

    If call_model is provided, it will be called instead of the real client
    (for testing with stubs)."""
    if call_model is None:
        call_model = _default_call_hosted_model

    for _ in range(attempts):
        try:
            resp = await call_model(client, model, task, hosted_tools, system_prompt, temperature)
        except Exception as e:  # noqa: BLE001
            print(f"  ! request error: {e}", file=sys.stderr)
            continue

        if not resp:
            continue

        calls = (
            resp.choices[0].message.tool_calls
            if hasattr(resp.choices[0].message, "tool_calls")
            else None
        )
        if not calls:
            continue  # declined this sample → resample

        try:
            tool_name = calls[0].function.name
            return tool_name
        except (AttributeError, IndexError):
            continue

    return None


async def _default_call_hosted_model(client, model, task, hosted_tools, system_prompt, temperature):
    """Default implementation for calling the model with hosted tools."""
    return await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ],
        tools=hosted_tools,
        tool_choice="auto",
        temperature=temperature,
        extra_body={"thinking_budget_tokens": 0},
    )


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def is_correct(case: dict, pred: str | None) -> bool:
    expected = case["expected"]
    if expected == "none":
        return pred is None
    return pred == expected or pred in case.get("acceptable", [])


def is_correct_hosted(case: dict, pred: str | None) -> bool:
    """Score a hosted-tool case. Expected is already the mcp__...__... name."""
    expected = case["expected"]
    if expected == "none":
        return pred is None
    return pred == expected or pred in case.get("acceptable", [])


async def evaluate(
    cases,
    client,
    model,
    catalog,
    repeats,
    attempts,
    temperature,
    concurrency,
    hosted_tools=None,
    call_model=None,
    catalog_mode="full",
):
    """Evaluate cases, routing them to the right scorer based on kind.

    If call_model is provided, it will be used instead of the real client
    (for testing with stubs). Should be an async callable matching
    _default_call_hosted_model signature."""
    skill_tools = load_skill_tool(catalog)
    skill_system = (
        f"{SELECT_SYSTEM}\n\nAvailable skills:\n{catalog_prompt(catalog, mode=catalog_mode)}"
    )
    hosted_system = HOSTED_SYSTEM
    sem = asyncio.Semaphore(concurrency)

    async def run_case(case):
        async with sem:
            kind = case.get("kind", "skill")  # default to skill for backwards compat
            preds = []

            if kind == "hosted":
                if not hosted_tools:
                    print(
                        f"  ! skipping hosted case {case['id']}: no hosted_tools loaded",
                        file=sys.stderr,
                    )
                    return None
                for _ in range(repeats):
                    preds.append(
                        await route_hosted_one(
                            client,
                            model,
                            case["task"],
                            hosted_tools,
                            hosted_system,
                            attempts,
                            temperature,
                            call_model=call_model,
                        )
                    )
            else:  # skill
                for _ in range(repeats):
                    preds.append(
                        await route_one(
                            client,
                            model,
                            case["task"],
                            catalog,
                            skill_tools,
                            skill_system,
                            attempts,
                            temperature,
                        )
                    )

            if not preds:
                return None

            majority, count = Counter(preds).most_common(1)[0]
            score_fn = is_correct_hosted if kind == "hosted" else is_correct
            return {
                "id": case["id"],
                "task": case["task"],
                "expected": case["expected"],
                "kind": kind,
                "cluster": case.get("cluster", "uncategorized"),
                "preds": preds,
                "prediction": majority,
                "correct": score_fn(case, majority),
                "stability": count / repeats,
            }

    results = await asyncio.gather(*(run_case(c) for c in cases))
    return [r for r in results if r is not None]


def summarize(results: list[dict]) -> dict:
    """Summarize results, including per-kind breakdown if mixed."""
    total = len(results)
    correct = sum(r["correct"] for r in results)
    by_cluster = defaultdict(lambda: [0, 0])
    by_skill = defaultdict(lambda: [0, 0])
    by_kind = defaultdict(lambda: [0, 0])
    confusion = []
    false_pos = 0
    neg_total = 0
    for r in results:
        kind = r.get("kind", "skill")
        by_kind[kind][0] += r["correct"]
        by_kind[kind][1] += 1

        c = by_cluster[r["cluster"]]
        c[0] += r["correct"]
        c[1] += 1
        if r["expected"] != "none":
            s = by_skill[r["expected"]]
            s[0] += r["correct"]
            s[1] += 1
            if not r["correct"]:
                confusion.append((r["expected"], r["prediction"], r["task"]))
        else:
            neg_total += 1
            if r["prediction"] is not None:
                false_pos += 1
    return {
        "overall": correct / total if total else 0.0,
        "n": total,
        "mean_stability": sum(r["stability"] for r in results) / total if total else 0.0,
        "by_cluster": {k: v[0] / v[1] for k, v in sorted(by_cluster.items())},
        "by_skill": {k: v[0] / v[1] for k, v in sorted(by_skill.items())},
        "by_kind": {k: v[0] / v[1] for k, v in sorted(by_kind.items())},
        "false_positive_rate": (false_pos / neg_total) if neg_total else None,
        "confusion": confusion,
    }


def write_reports(results, summary, report_dir, repeats):
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (Path(report_dir) / f"routing-eval-{stamp}.json").write_text(
        json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False)
    )

    lines = [
        f"# Routing eval — {stamp}",
        "",
        *routing_header_lines(),
        f"- cases: {summary['n']}  |  repeats: {repeats}",
        f"- overall accuracy: {summary['overall']:.3f}",
        f"- mean stability: {summary['mean_stability']:.3f}",
        f"- false-positive rate (negatives): " f"{summary['false_positive_rate']:.3f}"
        if summary["false_positive_rate"] is not None
        else "- false-positive rate: n/a",
        "",
    ]

    if len(summary.get("by_kind", {})) > 1:
        lines += ["", "## Per-kind", ""]
        for k, v in sorted(summary["by_kind"].items()):
            lines.append(f"- {k}: {v:.3f}")

    lines += ["", "## Per-cluster", ""]
    for k, v in summary["by_cluster"].items():
        lines.append(f"- {k}: {v:.3f}")
    lines += ["", "## Per-skill recall", ""]
    for k, v in summary["by_skill"].items():
        flag = "  <-- LOW" if v < 0.8 else ""
        lines.append(f"- {k}: {v:.3f}{flag}")
    lines += ["", "## Misroutes (expected -> predicted)", ""]
    for exp, pred, task in summary["confusion"]:
        lines.append(f"- {exp} -> {pred or 'NONE'}  | {task}")
    md_path = Path(report_dir) / f"routing-eval-{stamp}.md"
    md_path.write_text("\n".join(lines))
    return md_path


def make_client(base_url):
    try:
        from openai import AsyncOpenAI
    except ImportError:
        sys.exit("openai package required (pip install openai)")
    return AsyncOpenAI(base_url=base_url, api_key=os.environ.get("OPENAI_API_KEY", "sk-noop"))


def load_hosted_tools(path: str | None) -> list[dict] | None:
    """Load hosted tool schemas from JSON file."""
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text())
    except Exception as e:  # noqa: BLE001
        print(f"Failed to load hosted tools from {path}: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", required=True)
    ap.add_argument("--skills-dir")
    ap.add_argument("--catalog-json")
    ap.add_argument("--hosted-tools", help="JSON file with hosted tool schemas")
    ap.add_argument("--base-url", default="http://localhost:8000/v1")
    ap.add_argument("--model", default="gemma-4-12b")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--select-attempts", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--concurrency", type=int, default=2, help="match llama-server --parallel")
    ap.add_argument(
        "--catalog-mode",
        choices=["full", "terse", "names"],
        default="full",
        help="skill catalog rendering mode",
    )
    ap.add_argument("--report", default="eval/reports/")
    args = ap.parse_args()

    catalog = load_catalog(args.skills_dir, args.catalog_json)
    hosted_tools = load_hosted_tools(args.hosted_tools)
    cases = [json.loads(line) for line in Path(args.eval).read_text().splitlines() if line.strip()]

    # Cases expecting a skill that is not in the live catalog will always fail —
    # warn so unimplemented skills (e.g. ones you are mid-adding) are visible.
    missing = {
        c["expected"]
        for c in cases
        if c.get("kind", "skill") == "skill"
        and c["expected"] not in catalog
        and c["expected"] != "none"
    }
    if missing:
        print(
            f"NOTE: eval references skills not in the catalog (will score 0 until "
            f"registered): {', '.join(sorted(missing))}\n",
            file=sys.stderr,
        )

    client = make_client(args.base_url)

    # Print catalog mode and token count
    catalog_text = catalog_prompt(catalog, mode=args.catalog_mode)
    catalog_tokens = get_token_count(catalog_text)
    print(f"catalog mode={args.catalog_mode} tokens={catalog_tokens}", file=sys.stderr)

    results = asyncio.run(
        evaluate(
            cases,
            client,
            args.model,
            catalog,
            args.repeats,
            args.select_attempts,
            args.temperature,
            args.concurrency,
            hosted_tools=hosted_tools,
            catalog_mode=args.catalog_mode,
        )
    )
    summary = summarize(results)
    md = write_reports(results, summary, args.report, args.repeats)

    print(
        f"\noverall: {summary['overall']:.3f}  "
        f"stability: {summary['mean_stability']:.3f}  "
        f"FP-rate: {summary['false_positive_rate'] if summary['false_positive_rate'] is not None else 'n/a'}"
    )
    if len(summary.get("by_kind", {})) > 1:
        print("per-kind:")
        for k, v in sorted(summary["by_kind"].items()):
            print(f"  {k:<20} {v:.3f}")
    print("per-cluster:")
    for k, v in summary["by_cluster"].items():
        print(f"  {k:<20} {v:.3f}")
    print(f"\nreport: {md}")


if __name__ == "__main__":
    main()
