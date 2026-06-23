"""commit-pr skill logic: author commit messages and PR descriptions from a diff.

CRITICAL: never write to stdout. All logging goes to stderr.
NEVER runs git add / commit / push — reads the diff only.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("commit-pr")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")


async def _gemma(prompt: str, budget: int = 1024) -> str:
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


def _read_diff(diff_text: str | None, repo_path: str | None) -> str:
    if diff_text is not None:
        return diff_text
    # Read-only: `git diff HEAD`. NEVER add/commit/push.
    proc = subprocess.run(
        ["git", "-C", repo_path or ".", "diff", "HEAD"],
        capture_output=True, text=True, timeout=60,
    )
    return proc.stdout


async def summarize_diff(diff_text: str | None = None,
                         repo_path: str | None = None) -> dict:
    """Group diff hunks by intent. Returns {groups: [{intent, files, summary}]}."""
    diff = _read_diff(diff_text, repo_path)
    if not diff.strip():
        return {"groups": []}
    prompt = (
        "Group the changes in this git diff by intent (feat, fix, refactor, docs, "
        "test, chore). Respond ONLY with JSON: "
        '{"groups": [{"intent": "...", "files": ["..."], "summary": "..."}]}\n\n'
        f"DIFF:\n{diff}"
    )
    raw = await _gemma(prompt)
    try:
        parsed = json.loads(_strip_fences(raw))
        groups = parsed.get("groups", []) if isinstance(parsed, dict) else []
    except json.JSONDecodeError:
        log.warning("summarize_diff: could not parse model JSON")
        groups = []
    return {"groups": groups}


async def write_commit(groups: list[dict], scope: str | None = None) -> dict:
    """Emit a Conventional Commits message. Returns {message}."""
    scope_hint = f" Use scope '{scope}'." if scope else ""
    prompt = (
        "Write a single Conventional Commits message for these grouped changes."
        f"{scope_hint} Format: type(scope): subject, then an optional body. "
        "Respond with ONLY the commit message.\n\n"
        f"GROUPS:\n{json.dumps(groups, indent=2)}"
    )
    message = (await _gemma(prompt)).strip()
    return {"message": message}


async def write_pr(groups: list[dict], title: str | None = None) -> dict:
    """Emit a PR body with Summary/Rationale/Test Plan/Risk Notes. Returns {title, body}."""
    title_hint = f" Use the title '{title}'." if title else ""
    prompt = (
        "Write a pull-request description in markdown for these grouped changes."
        f"{title_hint} Include exactly these sections as h2 headers: "
        "Summary, Rationale, Test Plan, Risk Notes. Respond with ONLY the markdown body.\n\n"
        f"GROUPS:\n{json.dumps(groups, indent=2)}"
    )
    body = (await _gemma(prompt, budget=1536)).strip()
    if not title:
        first = groups[0]["summary"] if groups else "Update"
        title = first[:72]
    return {"title": title, "body": body}
