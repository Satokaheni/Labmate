# B3 commit-pr Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Add a `commit-pr` skill that authors commit messages and PR descriptions from a working-tree diff — reading the diff only, never staging/committing/pushing.

**Architecture:** A standard Labmate skill under `services/skills/commit-pr/`: `SKILL.md`, a logic module `commit_pr.py` with three functions, and an MCP `server.py`. `summarize_diff` reads the diff (or runs `git diff HEAD` read-only when none is passed) and uses Gemma to group hunks by intent; `write_commit` emits a Conventional Commits message; `write_pr` emits a markdown PR body. `SkillRunner.discover()` catalogs it from frontmatter.

**Tech Stack:** Python, `mcp` SDK, `litellm` (Gemma 4 31B), `git` (read-only), pytest.

> **Skill rules:** Never `print()` — log to `sys.stderr`. `server.py` uses `mcp.server.Server` + `stdio_server`. Every `litellm.acompletion` MUST pass `api_key="not-needed"`, `model="openai/gemma-4-31b"`, `api_base` from env `GEMMA_BASE`, and explicit `extra_body={"thinking_budget_tokens": ...}`. This skill NEVER runs `git add`, `git commit`, or `git push` — it only reads.

---

### Task 1: Create the skill logic module

**Files:**
- Create: `services/skills/commit-pr/commit_pr.py`
- Create: `services/skills/commit-pr/__init__.py`
- Create: `tests/services/skills/commit-pr/__init__.py`
- Create: `tests/services/skills/commit-pr/test_commit_pr.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/skills/commit-pr/__init__.py` (empty), then `tests/services/skills/commit-pr/test_commit_pr.py`:

```python
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "services" / "skills" / "commit-pr" / "commit_pr.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("commit_pr", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _llm_returning(content: str):
    fake = MagicMock()
    fake.choices = [MagicMock()]
    fake.choices[0].message.content = content
    return AsyncMock(return_value=fake)


@pytest.mark.asyncio
async def test_summarize_diff_groups_with_llm():
    cp = _load()
    diff = "diff --git a/auth.py b/auth.py\n+def login(): ...\n"
    llm_json = json.dumps({"groups": [
        {"intent": "feat", "files": ["auth.py"], "summary": "add login"}]})
    with patch.object(cp.litellm, "acompletion", new=_llm_returning(llm_json)) as mac:
        out = await cp.summarize_diff(diff_text=diff)
    assert out["groups"][0]["intent"] == "feat"
    assert mac.await_args.kwargs.get("api_key") == "not-needed"


@pytest.mark.asyncio
async def test_summarize_diff_runs_git_when_no_text(tmp_path):
    cp = _load()
    llm_json = json.dumps({"groups": [{"intent": "fix", "files": ["x.py"], "summary": "fix x"}]})
    with patch.object(cp.subprocess, "run") as mrun, \
         patch.object(cp.litellm, "acompletion", new=_llm_returning(llm_json)):
        mrun.return_value = MagicMock(returncode=0, stdout="diff --git a/x.py b/x.py\n", stderr="")
        out = await cp.summarize_diff(repo_path=str(tmp_path))
    # Must call read-only `git diff HEAD`, never add/commit/push.
    called = mrun.call_args[0][0]
    assert "diff" in called
    assert "commit" not in called and "add" not in called and "push" not in called
    assert out["groups"][0]["intent"] == "fix"


@pytest.mark.asyncio
async def test_write_commit_emits_conventional_message():
    cp = _load()
    groups = [{"intent": "feat", "files": ["auth.py"], "summary": "add login flow"}]
    with patch.object(cp.litellm, "acompletion",
                      new=_llm_returning("feat(auth): add login flow")):
        out = await cp.write_commit(groups, scope="auth")
    assert out["message"].startswith("feat")


@pytest.mark.asyncio
async def test_write_pr_has_required_sections():
    cp = _load()
    groups = [{"intent": "feat", "files": ["auth.py"], "summary": "add login flow"}]
    body = ("## Summary\n...\n## Rationale\n...\n## Test Plan\n...\n## Risk Notes\n...")
    with patch.object(cp.litellm, "acompletion", new=_llm_returning(body)):
        out = await cp.write_pr(groups, title="Add login")
    assert out["title"]
    for section in ("Summary", "Rationale", "Test Plan", "Risk Notes"):
        assert section in out["body"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/skills/commit-pr/test_commit_pr.py -v`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Write minimal implementation**

Create `services/skills/commit-pr/__init__.py` (empty).

Create `services/skills/commit-pr/commit_pr.py`:

```python
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
        # Derive a short title from the first group summary.
        first = groups[0]["summary"] if groups else "Update"
        title = first[:72]
    return {"title": title, "body": body}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/skills/commit-pr/test_commit_pr.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/skills/commit-pr/__init__.py services/skills/commit-pr/commit_pr.py tests/services/skills/commit-pr/
git commit -m "feat(skills): add commit-pr logic module (B3)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Add SKILL.md, server.py, and requirements.txt

**Files:**
- Create: `services/skills/commit-pr/SKILL.md`
- Create: `services/skills/commit-pr/server.py`
- Create: `services/skills/commit-pr/requirements.txt`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/skills/commit-pr/test_commit_pr.py`:

```python
def test_skill_md_frontmatter_parses():
    import frontmatter
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    meta, _ = frontmatter.parse(skill_md.read_text(encoding="utf-8"))
    assert meta["name"] == "commit-pr"
    assert "never stages" in meta["description"].lower() or "reads the diff" in meta["description"].lower()


def test_server_lists_three_tools():
    import importlib.util, asyncio
    server_path = _MODULE_PATH.parent / "server.py"
    spec = importlib.util.spec_from_file_location("commit_pr_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = asyncio.get_event_loop().run_until_complete(mod.list_tools())
    assert {t.name for t in tools} == {"summarize_diff", "write_commit", "write_pr"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/skills/commit-pr/test_commit_pr.py::test_skill_md_frontmatter_parses tests/services/skills/commit-pr/test_commit_pr.py::test_server_lists_three_tools -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Create `services/skills/commit-pr/SKILL.md` (frontmatter pasted EXACTLY):

```markdown
---
name: commit-pr
description: >-
  Authors commit messages and pull-request descriptions from a working-tree diff.
  summarize_diff groups changes by intent; write_commit emits a Conventional
  Commits message; write_pr produces a PR body with summary, rationale, test
  plan, and risk notes. Use after editing code, when preparing a commit, or when
  opening a pull request. Reads the diff only — it never stages, commits, or
  pushes. Distinct from the git_status/git_log/git_diff bridge tools, which only
  read repository state; this skill generates the prose that describes a change.
version: "0.1.0"
license: MIT
requires: []
---

# commit-pr Skill

Generates the prose describing a change — never mutates the repository.

## When to use

- After editing code, to prepare a commit message.
- When opening a pull request.

## Tools

- `summarize_diff(diff_text=None, repo_path=None)` — groups changes by intent;
  runs read-only `git diff HEAD` when no diff is passed.
- `write_commit(groups, scope=None)` — `{message}` in Conventional Commits format.
- `write_pr(groups, title=None)` — `{title, body}` with Summary, Rationale,
  Test Plan, and Risk Notes sections.

## Constraints

- NEVER runs `git add`, `git commit`, or `git push`. Reads the diff only.
```

Create `services/skills/commit-pr/server.py`:

```python
"""MCP server for the commit-pr skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import asyncio
import json
import logging
import sys

import commit_pr

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("commit-pr.server")
app: Server = Server("commit-pr")


@app.list_tools()
async def list_tools() -> list[Tool]:
    groups = {"groups": {"type": "array", "items": {"type": "object"}}}
    return [
        Tool(name="summarize_diff",
             description="Group diff hunks by intent (reads diff only).",
             inputSchema={"type": "object", "properties": {
                 "diff_text": {"type": "string"},
                 "repo_path": {"type": "string"}}}),
        Tool(name="write_commit",
             description="Emit a Conventional Commits message from grouped changes.",
             inputSchema={"type": "object", "properties": {
                 **groups, "scope": {"type": "string"}}, "required": ["groups"]}),
        Tool(name="write_pr",
             description="Emit a PR body (Summary/Rationale/Test Plan/Risk Notes).",
             inputSchema={"type": "object", "properties": {
                 **groups, "title": {"type": "string"}}, "required": ["groups"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "summarize_diff":
            result = await commit_pr.summarize_diff(
                arguments.get("diff_text"), arguments.get("repo_path"))
        elif name == "write_commit":
            result = await commit_pr.write_commit(
                arguments["groups"], arguments.get("scope"))
        elif name == "write_pr":
            result = await commit_pr.write_pr(
                arguments["groups"], arguments.get("title"))
        else:
            raise ValueError(f"unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(result, default=str))]
    except Exception as exc:
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def main() -> None:
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

Create `services/skills/commit-pr/requirements.txt`:

```
mcp>=1.0.0
litellm>=1.0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/skills/commit-pr/test_commit_pr.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/skills/commit-pr/SKILL.md services/skills/commit-pr/server.py services/skills/commit-pr/requirements.txt
git commit -m "feat(skills): add commit-pr SKILL.md + MCP server (B3)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Verify catalog registration

**Files:**
- Modify: `tests/services/skills/commit-pr/test_commit_pr.py`

Discovery is automatic from SKILL.md frontmatter. This guard test proves `SkillRunner` catalogs the skill and `catalog_prompt()` advertises it.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/skills/commit-pr/test_commit_pr.py`:

```python
def test_skill_runner_catalogs_commit_pr():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent  # .../services/skills
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "commit-pr" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "commit-pr" in prompt
    assert "pull-request" in prompt.lower() or "pull request" in prompt.lower()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/services/skills/commit-pr/test_commit_pr.py::test_skill_runner_catalogs_commit_pr -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/services/skills/commit-pr/test_commit_pr.py
git commit -m "test(skills): assert commit-pr is cataloged (B3)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
