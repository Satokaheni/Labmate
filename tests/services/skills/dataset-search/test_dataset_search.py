from __future__ import annotations
import asyncio
import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

_MODULE_PATH = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "dataset-search" / "dataset_search.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("dataset_search", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_response(json_data):
    mock = MagicMock()
    mock.json.return_value = json_data
    mock.raise_for_status.return_value = None
    return mock


def test_search_hf_hub_returns_results():
    ds = _load()
    raw = [
        {"id": "emotion", "downloads": 1000, "likes": 50,
         "task_categories": ["text-classification"], "description": "Emotion dataset"},
    ]
    with patch.object(ds.requests, "get", return_value=_make_response(raw)):
        out = ds.search_hf_hub("emotion recognition", max_results=5)
    assert "results" in out
    assert out["results"][0]["id"] == "emotion"
    assert "task_categories" in out["results"][0]


def test_search_hf_hub_handles_network_error():
    ds = _load()
    with patch.object(ds.requests, "get", side_effect=ds.requests.RequestException("timeout")):
        out = ds.search_hf_hub("anything")
    assert out["results"] == []
    assert "error" in out


def test_search_papers_with_code_returns_results():
    ds = _load()
    raw = {"results": [
        {"name": "EmpathyDataset", "full_name": "Empathy Dialogue Dataset",
         "description": "Empathetic conversation dataset",
         "evaluations": [{"task": "dialogue"}], "url": "https://pwc.com/x"}
    ]}
    with patch.object(ds.requests, "get", return_value=_make_response(raw)):
        out = ds.search_papers_with_code("empathetic dialogue", max_results=3)
    assert out["results"][0]["name"] == "EmpathyDataset"
    assert "tasks" in out["results"][0]


def test_rank_candidates_scores_by_overlap():
    ds = _load()
    candidates = [
        {"id": "emotion", "description": "emotion classification labels"},
        {"id": "weather", "description": "weather forecast data"},
        {"id": "sentiment", "description": "sentiment polarity dataset emotion"},
    ]
    out = ds.rank_candidates(candidates, "emotion labels", criteria=["classification"])
    ranked = out["ranked"]
    assert ranked[0]["score"] >= ranked[-1]["score"]
    # 'emotion' or 'sentiment' should outscore 'weather'
    top_ids = [r["id"] for r in ranked[:2]]
    assert "weather" not in top_ids


def test_skill_md_frontmatter_parses():
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    assert m, "No YAML frontmatter found"
    meta = yaml.safe_load(m.group(1))
    assert meta["name"] == "dataset-search"
    desc = meta["description"].lower()
    assert "web-search" in desc or "web search" in desc
    assert "dataset-generation" in desc or "dataset generation" in desc


def test_server_lists_three_tools():
    server_path = _MODULE_PATH.parent / "server.py"
    spec = importlib.util.spec_from_file_location("dataset_search_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = asyncio.run(mod.list_tools())
    assert {t.name for t in tools} == {"search_hf_hub", "search_papers_with_code", "rank_candidates"}


def test_skill_runner_catalogs_dataset_search():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "dataset-search" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "dataset-search" in prompt
    assert "dataset" in prompt.lower()
