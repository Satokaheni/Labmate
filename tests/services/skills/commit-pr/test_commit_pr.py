from __future__ import annotations
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[4]
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


def test_skill_md_frontmatter_parses():
    import yaml, re
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    assert m
    meta = yaml.safe_load(m.group(1))
    assert meta["name"] == "commit-pr"
    desc = meta["description"].lower()
    assert "never stages" in desc or "reads the diff" in desc


def test_server_lists_three_tools():
    import importlib.util, asyncio, sys
    server_path = _MODULE_PATH.parent / "server.py"
    # Add the skill directory to sys.path so server.py can import commit_pr
    skill_dir = str(_MODULE_PATH.parent)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    spec = importlib.util.spec_from_file_location("commit_pr_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = asyncio.run(mod.list_tools())
    assert {t.name for t in tools} == {"summarize_diff", "write_commit", "write_pr"}


def test_skill_runner_catalogs_commit_pr():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "commit-pr" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "commit-pr" in prompt
    assert "pull-request" in prompt.lower() or "pull request" in prompt.lower()
