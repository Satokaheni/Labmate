"""Live execution smoke for skills that depend on EXTERNAL services / creds.

Each test gates (skips, not fails) when its prerequisite is missing:
- citation-graph  -> Semantic Scholar API (SS_API_KEY, network)
- dataset-search  -> Hugging Face Hub (network)
- paper-rag       -> Chroma (CHROMA_URL) + embedding model

Run with the repo .env loaded so the creds are present, e.g.:
    set -a; . ./.env; set +a
    LIVE_TESTS=1 PYTHONPATH=. python -m pytest tests/live/test_skill_exec_external_live.py -rs -v
"""
import os
import urllib.request

import pytest

from tests.live.conftest import require_service
from tests.live.skill_harness import (
    manifest_by_name, call_skill_tool, result_text, result_is_error,
    SkillRegisterError,
)

pytestmark = pytest.mark.live


def _reachable(url: str, timeout: float = 4.0) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception as exc:  # noqa: BLE001 — any failure means "treat as down"
        # An HTTP error code still proves the host answered.
        return isinstance(exc, urllib.error.HTTPError)


async def _run(skill_name, tool, args, timeout=60.0):
    m = manifest_by_name(skill_name)
    if m is None:
        require_service(lambda: False, f"{skill_name} not runnable")
    try:
        return await call_skill_tool(m, tool, args, timeout=timeout)
    except SkillRegisterError as exc:
        require_service(lambda: False, f"{skill_name} register ({exc})")


@pytest.mark.asyncio
async def test_citation_graph_search_papers():
    require_service(lambda: bool(os.getenv("SS_API_KEY")), "SS_API_KEY (Semantic Scholar)")
    require_service(lambda: _reachable("https://api.semanticscholar.org"), "Semantic Scholar API")
    r = await _run("citation-graph", "search_papers",
                   {"query": "attention is all you need", "limit": 3}, timeout=60)
    assert not result_is_error(r)
    assert result_text(r).strip(), "citation-graph returned empty"


@pytest.mark.asyncio
async def test_dataset_search_hf_hub():
    require_service(lambda: _reachable("https://huggingface.co"), "Hugging Face Hub")
    r = await _run("dataset-search", "search_hf_hub",
                   {"query": "sentiment analysis", "max_results": 3}, timeout=60)
    assert not result_is_error(r)
    assert result_text(r).strip(), "dataset-search returned empty"


@pytest.mark.asyncio
async def test_paper_rag_list_papers():
    chroma = os.getenv("CHROMA_URL", "http://localhost:8765")
    require_service(lambda: _reachable(chroma + "/api/v2/heartbeat"), f"Chroma at {chroma}")
    # list_papers is zero-arg and needs no embedding/LLM call — proves the skill
    # registers and reaches its Chroma collection.
    r = await _run("paper-rag", "list_papers", {}, timeout=60)
    assert not result_is_error(r)
    assert result_text(r).strip(), "paper-rag returned empty"
