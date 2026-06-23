#!/usr/bin/env python3
"""
run_routing_eval.py — score skill routing against your select() mechanism.

Mirrors skill_router.select() as described in the harness audit: same
catalog_prompt, a single load_skill tool whose `name` is an enum over the
catalog, tool_choice="auto", thinking_budget_tokens=0, and SELECT_ATTEMPTS
resampling. Reports overall / per-cluster / per-skill accuracy, a confusion list,
the false-positive rate on negative cases, and per-case stability across repeats.

  python eval/run_routing_eval.py \
    --eval eval/routing_eval.jsonl \
    --skills-dir services/skills \
    --base-url http://localhost:8000/v1 \
    --model gemma-4-31b \
    --repeats 3 \
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
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

SELECT_SYSTEM = (
    "You are a skill router. If ANY available skill is relevant to the user's "
    "task, you MUST call load_skill with that skill's name. Only decline to call "
    "a tool if truly no skill fits."
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


def catalog_prompt(catalog: dict[str, str]) -> str:
    return "\n".join(f"- {n}: {d}" for n, d in catalog.items())


def load_skill_tool(catalog: dict[str, str]) -> list[dict]:
    return [{
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
    }]


# --------------------------------------------------------------------------- #
# Routing — SEAM: swap this for the real select() for production fidelity
# --------------------------------------------------------------------------- #
async def route_one(client, model, task, catalog, tools, system_prompt,
                    attempts, temperature) -> str | None:
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


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def is_correct(case: dict, pred: str | None) -> bool:
    expected = case["expected"]
    if expected == "none":
        return pred is None
    return pred == expected or pred in case.get("acceptable", [])


async def evaluate(cases, client, model, catalog, repeats, attempts,
                   temperature, concurrency):
    tools = load_skill_tool(catalog)
    system_prompt = f"{SELECT_SYSTEM}\n\nAvailable skills:\n{catalog_prompt(catalog)}"
    sem = asyncio.Semaphore(concurrency)

    async def run_case(case):
        async with sem:
            preds = []
            for _ in range(repeats):
                preds.append(await route_one(client, model, case["task"], catalog,
                                             tools, system_prompt, attempts, temperature))
            majority, count = Counter(preds).most_common(1)[0]
            return {
                "id": case["id"], "task": case["task"], "expected": case["expected"],
                "cluster": case.get("cluster", "uncategorized"),
                "preds": preds, "prediction": majority,
                "correct": is_correct(case, majority),
                "stability": count / repeats,
            }

    results = await asyncio.gather(*(run_case(c) for c in cases))
    return list(results)


def summarize(results: list[dict]) -> dict:
    total = len(results)
    correct = sum(r["correct"] for r in results)
    by_cluster = defaultdict(lambda: [0, 0])
    by_skill = defaultdict(lambda: [0, 0])
    confusion = []
    false_pos = 0
    neg_total = 0
    for r in results:
        c = by_cluster[r["cluster"]]
        c[0] += r["correct"]; c[1] += 1
        if r["expected"] != "none":
            s = by_skill[r["expected"]]
            s[0] += r["correct"]; s[1] += 1
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
        "false_positive_rate": (false_pos / neg_total) if neg_total else None,
        "confusion": confusion,
    }


def write_reports(results, summary, report_dir, repeats):
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (Path(report_dir) / f"routing-eval-{stamp}.json").write_text(
        json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False))

    lines = [f"# Routing eval — {stamp}", "",
             f"- cases: {summary['n']}  |  repeats: {repeats}",
             f"- overall accuracy: {summary['overall']:.3f}",
             f"- mean stability: {summary['mean_stability']:.3f}",
             f"- false-positive rate (negatives): "
             f"{summary['false_positive_rate']:.3f}" if summary['false_positive_rate']
             is not None else "- false-positive rate: n/a", "", "## Per-cluster", ""]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", required=True)
    ap.add_argument("--skills-dir")
    ap.add_argument("--catalog-json")
    ap.add_argument("--base-url", default="http://localhost:8000/v1")
    ap.add_argument("--model", default="gemma-4-31b")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--select-attempts", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--concurrency", type=int, default=2, help="match llama-server --parallel")
    ap.add_argument("--report", default="eval/reports/")
    args = ap.parse_args()

    if not (args.skills_dir or args.catalog_json):
        sys.exit("Provide --skills-dir or --catalog-json")
    catalog = load_catalog(args.skills_dir, args.catalog_json)
    cases = [json.loads(l) for l in Path(args.eval).read_text().splitlines() if l.strip()]

    # Cases expecting a skill that is not in the live catalog will always fail —
    # warn so unimplemented skills (e.g. ones you are mid-adding) are visible.
    missing = {c["expected"] for c in cases
               if c["expected"] not in catalog and c["expected"] != "none"}
    if missing:
        print(f"NOTE: eval references skills not in the catalog (will score 0 until "
              f"registered): {', '.join(sorted(missing))}\n", file=sys.stderr)

    client = make_client(args.base_url)
    results = asyncio.run(evaluate(cases, client, args.model, catalog, args.repeats,
                                   args.select_attempts, args.temperature, args.concurrency))
    summary = summarize(results)
    md = write_reports(results, summary, args.report, args.repeats)

    print(f"\noverall: {summary['overall']:.3f}  "
          f"stability: {summary['mean_stability']:.3f}  "
          f"FP-rate: {summary['false_positive_rate'] if summary['false_positive_rate'] is not None else 'n/a'}")
    print("per-cluster:")
    for k, v in summary["by_cluster"].items():
        print(f"  {k:<20} {v:.3f}")
    print(f"\nreport: {md}")


if __name__ == "__main__":
    main()
