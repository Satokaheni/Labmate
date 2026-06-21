#!/usr/bin/env python3
"""
extend_eval.py — keep the routing eval set in sync with the skill catalog.

For every skill in the catalog that has no cases in the eval file yet, this
generates:
  - positive cases: realistic task phrasings that should route to the skill
  - disambiguation cases: tasks that resemble the skill's nearest neighbors but
    should still route to the new skill (and a few guard cases that resemble the
    new skill but should route to a neighbor)

Generation uses your local Gemma endpoint (OpenAI-compatible, e.g. llama-server).
Pass --no-llm for a deterministic, lower-quality templated fallback.

Run it after adding any skill; it only touches skills not already covered, so it
is safe to run repeatedly.

  python eval/extend_eval.py \
    --skills-dir services/skills \
    --eval eval/routing_eval.jsonl \
    --per-skill 6 \
    --base-url http://localhost:8000/v1 \
    --model gemma-4-31b
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


# --------------------------------------------------------------------------- #
# Catalog loading
# --------------------------------------------------------------------------- #
def load_catalog(skills_dir: str | None, catalog_json: str | None) -> dict[str, str]:
    """Return {skill_name: description}. Prefer an exported catalog JSON if given,
    otherwise scan <skills_dir>/*/SKILL.md frontmatter. Adapt this if your
    catalog lives somewhere else (e.g. runner.catalog)."""
    if catalog_json:
        data = json.loads(Path(catalog_json).read_text())
        return {k: collapse(v) for k, v in data.items()}
    if not skills_dir:
        sys.exit("Provide --skills-dir or --catalog-json")
    if yaml is None:
        sys.exit("pyyaml is required to parse SKILL.md frontmatter (pip install pyyaml)")
    catalog: dict[str, str] = {}
    for skill_md in sorted(Path(skills_dir).glob("*/SKILL.md")):
        name, desc = parse_frontmatter(skill_md)
        if name and desc:
            catalog[name] = collapse(desc)
    if not catalog:
        sys.exit(f"No SKILL.md files found under {skills_dir}")
    return catalog


def parse_frontmatter(path: Path) -> tuple[str | None, str | None]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return None, None
    meta = yaml.safe_load(m.group(1)) or {}
    name = meta.get("name") or path.parent.name
    return name, meta.get("description")


def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# Eval file I/O
# --------------------------------------------------------------------------- #
def load_eval(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def covered_skills(cases: list[dict]) -> set[str]:
    return {c["expected"] for c in cases if c.get("expected") and c["expected"] != "none"}


def existing_tasks(cases: list[dict]) -> set[str]:
    return {norm(c["task"]) for c in cases}


def cluster_for(skill: str, neighbors: list[str], cases: list[dict]) -> str | None:
    """Reuse a neighbor's existing cluster tag if one exists."""
    by_skill = {c["expected"]: c.get("cluster") for c in cases if c.get("cluster")}
    for n in neighbors:
        if by_skill.get(n):
            return by_skill[n]
    return None


def norm(s: str) -> str:
    return collapse(s).lower()


# --------------------------------------------------------------------------- #
# LLM helpers (OpenAI-compatible local endpoint)
# --------------------------------------------------------------------------- #
def make_client(base_url: str):
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("openai package required (pip install openai), or use --no-llm")
    return OpenAI(base_url=base_url, api_key=os.environ.get("OPENAI_API_KEY", "sk-noop"))


def chat_json(client, model: str, prompt: str, temperature: float = 0.8):
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        extra_body={"thinking_budget_tokens": 512},
    )
    return extract_json(resp.choices[0].message.content or "")


def extract_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
def gen_positive(client, model, skill, desc, n) -> list[str]:
    prompt = (
        f"A routing system picks one skill for a user task. The skill is:\n"
        f"  {skill}: {desc}\n\n"
        f"Write {n} diverse, realistic user task phrasings that should route to "
        f"this skill. Vary length and wording; sound like a real user, not a "
        f"description echo. Return ONLY a JSON array of strings."
    )
    out = chat_json(client, model, prompt)
    return [s for s in (out or []) if isinstance(s, str)][:n]


def gen_neighbors(client, model, skill, desc, catalog) -> list[str]:
    others = {k: v for k, v in catalog.items() if k != skill}
    listing = "\n".join(f"- {k}: {v}" for k, v in others.items())
    prompt = (
        f"New skill:\n  {skill}: {desc}\n\n"
        f"Existing skills:\n{listing}\n\n"
        f"Which existing skills are most likely to be confused with the new skill "
        f"during routing? Return ONLY a JSON array of up to 3 existing skill names."
    )
    out = chat_json(client, model, prompt, temperature=0.3)
    return [s for s in (out or []) if s in others][:3]


def gen_disambiguation(client, model, skill, desc, neighbors, catalog) -> list[dict]:
    if not neighbors:
        return []
    nb = "\n".join(f"- {n}: {catalog[n]}" for n in neighbors)
    prompt = (
        f"Target skill:\n  {skill}: {desc}\n\n"
        f"Nearby skills it could be confused with:\n{nb}\n\n"
        f"Write boundary task phrasings that sit near these skills. Return ONLY a "
        f'JSON array of objects: {{"task": "...", "expected": "<skill name that '
        f'should win>"}}. Include 3 where the target should win despite resembling '
        f"a neighbor, and 2 where a neighbor should win despite resembling the "
        f"target. Use only the skill names given above."
    )
    out = chat_json(client, model, prompt, temperature=0.6)
    valid = set(neighbors) | {skill}
    return [o for o in (out or [])
            if isinstance(o, dict) and o.get("task") and o.get("expected") in valid]


def templated_positive(skill, desc, n) -> list[str]:
    """Deterministic fallback: derive a couple of tasks from the 'Use when' clause."""
    m = re.search(r"Use when (.+?)(?:\.|Distinct from|$)", desc, re.IGNORECASE)
    base = m.group(1).strip() if m else f"work that needs the {skill} skill"
    base = base.rstrip(".")
    return [f"I need to {base}.", f"Help me {base}.", f"Can you {base}?"][:n]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skills-dir")
    ap.add_argument("--catalog-json")
    ap.add_argument("--eval", required=True)
    ap.add_argument("--per-skill", type=int, default=6)
    ap.add_argument("--base-url", default="http://localhost:8000/v1")
    ap.add_argument("--model", default="gemma-4-31b")
    ap.add_argument("--no-llm", action="store_true", help="deterministic templated cases")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    args = ap.parse_args()

    catalog = load_catalog(args.skills_dir, args.catalog_json)
    eval_path = Path(args.eval)
    cases = load_eval(eval_path)
    covered = covered_skills(cases)
    seen_tasks = existing_tasks(cases)

    new_skills = [s for s in catalog if s not in covered]
    if not new_skills:
        print(f"All {len(catalog)} skills already covered. Nothing to add.")
        return

    client = None if args.no_llm else make_client(args.base_url)
    additions: list[dict] = []

    for skill in new_skills:
        desc = catalog[skill]
        print(f"[+] generating cases for: {skill}")
        if args.no_llm:
            positives = templated_positive(skill, desc, args.per_skill)
            neighbors, disambig = [], []
        else:
            positives = gen_positive(client, args.model, skill, desc, args.per_skill)
            neighbors = gen_neighbors(client, args.model, skill, desc, catalog)
            disambig = gen_disambiguation(client, args.model, skill, desc, neighbors, catalog)
            print(f"    neighbors: {', '.join(neighbors) or '(none)'}")

        cluster = cluster_for(skill, neighbors, cases) or slug(skill)
        idx = 0
        for task in positives:
            if norm(task) in seen_tasks:
                continue
            seen_tasks.add(norm(task))
            idx += 1
            additions.append({"id": f"{cluster}_{slug(skill)}_gen_{idx}", "task": task,
                              "expected": skill, "cluster": cluster, "source": "generated"})
        for o in disambig:
            if norm(o["task"]) in seen_tasks:
                continue
            seen_tasks.add(norm(o["task"]))
            idx += 1
            row = {"id": f"{cluster}_{slug(skill)}_disambig_{idx}", "task": o["task"],
                   "expected": o["expected"], "cluster": cluster, "source": "generated"}
            if o["expected"] != skill:
                row["acceptable"] = [skill]   # boundary case won by a neighbor
            additions.append(row)

    print(f"\nGenerated {len(additions)} cases for {len(new_skills)} skill(s): "
          f"{', '.join(new_skills)}")

    if args.dry_run:
        for a in additions:
            print(json.dumps(a, ensure_ascii=False))
        print("\n(dry run — nothing written)")
        return

    with eval_path.open("a", encoding="utf-8") as f:
        for a in additions:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    print(f"Appended to {eval_path} (now {len(cases) + len(additions)} cases).")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


if __name__ == "__main__":
    main()
