from __future__ import annotations
import asyncio
import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

_MODULE_PATH = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "dataset-generation" / "dataset_generation.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("dataset_generation", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _llm_returning(content: str):
    fake = MagicMock()
    fake.choices = [MagicMock()]
    fake.choices[0].message.content = content
    return AsyncMock(return_value=fake)


@pytest.mark.asyncio
async def test_generate_from_seeds_calls_llm_per_seed():
    dg = _load()
    seeds = ["Write an empathetic reply to grief.", "Respond to frustration."]
    generated = json.dumps([
        {"input": "I am sad", "output": "I understand your pain."},
        {"input": "I lost my job", "output": "That must be so hard."},
        {"input": "I feel hopeless", "output": "You are not alone."},
    ])
    with patch.object(dg.litellm, "acompletion", new=_llm_returning(generated)) as mac:
        out = await dg.generate_from_seeds(seeds, template="empathetic dialogue", n_per_seed=3)
    assert mac.await_count == 2  # one call per seed
    # Check api_key on each call
    for call in mac.await_args_list:
        assert call.kwargs.get("api_key") == "not-needed"
    assert len(out["examples"]) == 2
    assert out["total_generated"] == 6


@pytest.mark.asyncio
async def test_generate_from_seeds_handles_llm_failure():
    dg = _load()
    with patch.object(dg.litellm, "acompletion", side_effect=Exception("timeout")):
        out = await dg.generate_from_seeds(["seed"], template="test")
    assert out["examples"][0]["generated"] == []
    assert out["total_generated"] == 0


def test_format_as_jsonl_writes_file(tmp_path):
    dg = _load()
    examples = [
        {"seed": "s1", "generated": [{"input": "a", "output": "b"}, {"input": "c", "output": "d"}]},
    ]
    out_path = tmp_path / "out.jsonl"
    out = dg.format_as_jsonl(examples, str(out_path))
    assert out["ok"] is True
    assert out["count"] == 2
    lines = out_path.read_text().strip().split("\n")
    assert len(lines) == 2
    record = json.loads(lines[0])
    assert record["seed"] == "s1"
    assert record["input"] == "a"


def test_validate_coverage_finds_gaps():
    dg = _load()
    examples = [
        {"seed": "s1", "generated": [{"input": "empathy example", "output": "caring reply"}]},
    ]
    out = dg.validate_coverage(examples, requirements=["empathy", "humor", "caring"])
    assert "empathy" in out["covered"]
    assert "caring" in out["covered"]
    assert "humor" in out["gaps"]
    assert abs(out["coverage_pct"] - 2/3) < 1e-6


def test_skill_md_frontmatter_parses():
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    assert m, "No YAML frontmatter found"
    meta = yaml.safe_load(m.group(1))
    assert meta["name"] == "dataset-generation"
    desc = meta["description"].lower()
    assert "dataset-search" in desc or "dataset search" in desc


def test_server_lists_three_tools():
    import sys
    server_path = _MODULE_PATH.parent / "server.py"
    # Add the skill directory to sys.path so server.py can import dataset_generation
    skill_dir = str(_MODULE_PATH.parent)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    spec = importlib.util.spec_from_file_location("dataset_generation_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = asyncio.run(mod.list_tools())
    assert {t.name for t in tools} == {
        "generate_from_seeds", "format_as_jsonl", "validate_coverage"}


def test_skill_runner_catalogs_dataset_generation():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "dataset-generation" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "dataset-generation" in prompt
    assert "synthetic" in prompt.lower() or "dataset" in prompt.lower()
