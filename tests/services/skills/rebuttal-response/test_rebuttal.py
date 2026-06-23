from __future__ import annotations
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "rebuttal-response" / "rebuttal.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("rebuttal", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_reviews_extracts_concerns():
    rb = _load()
    review = (
        "1. The evaluation is weak; no baseline comparison. (major)\n"
        "2. Typo in Section 3.\n"
    )
    out = rb.parse_reviews(review)
    assert "concerns" in out
    assert len(out["concerns"]) >= 2
    first = out["concerns"][0]
    assert set(first) >= {"id", "severity", "type", "target_section", "text"}


@pytest.mark.asyncio
async def test_draft_response_calls_llm_per_concern():
    rb = _load()
    concerns = [
        {"id": "c1", "severity": "major", "type": "evaluation",
         "target_section": "5", "text": "no baseline"},
        {"id": "c2", "severity": "minor", "type": "typo",
         "target_section": "3", "text": "typo"},
    ]
    fake = MagicMock()
    fake.choices = [MagicMock()]
    fake.choices[0].message.content = "We have addressed this by adding a baseline."
    with patch.object(rb.litellm, "acompletion", new=AsyncMock(return_value=fake)) as mac:
        out = await rb.draft_response(concerns, paper_context="Our method ...")
    assert mac.await_count == 2
    for call in mac.await_args_list:
        assert call.kwargs.get("api_key") == "not-needed"
    assert len(out["responses"]) == 2
    assert out["responses"][0]["concern_id"] == "c1"


def test_coverage_audit_reports_gaps():
    rb = _load()
    concerns = [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]
    responses = [
        {"concern_id": "c1", "response": "..."},
        {"concern_id": "c2", "response": "..."},
    ]
    out = rb.coverage_audit(concerns, responses)
    assert out["covered"] == ["c1", "c2"]
    assert out["gaps"] == ["c3"]
    assert abs(out["coverage_pct"] - (2 / 3)) < 1e-6


def test_skill_md_frontmatter_parses():
    import yaml, re
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    assert m
    meta = yaml.safe_load(m.group(1))
    assert meta["name"] == "rebuttal-response"
    assert meta["requires"] == ["pdf-parse", "paper-rag", "citation-check"]


@pytest.mark.asyncio
async def test_server_lists_three_tools():
    import importlib.util
    server_path = _MODULE_PATH.parent / "server.py"
    spec = importlib.util.spec_from_file_location("rebuttal_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = await mod.list_tools()
    assert {t.name for t in tools} == {"parse_reviews", "draft_response", "coverage_audit"}


def test_skill_runner_catalogs_rebuttal_response():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "rebuttal-response" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "rebuttal-response" in prompt
    assert "rebuttal" in prompt.lower()
