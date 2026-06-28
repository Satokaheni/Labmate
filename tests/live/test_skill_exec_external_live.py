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


def _vision_available() -> bool:
    """True iff the GEMMA_BASE endpoint accepts image input (mmproj loaded).

    The default RunPod llama.cpp build serves Gemma TEXT-ONLY and returns
    "image input is not supported" (HTTP 500) for an image_url message — the
    vision skills (design-critique, screenshot-to-component) then cannot run.
    """
    import base64 as _b64
    import json as _json
    base = os.getenv("GEMMA_BASE", "http://localhost:8000/v1").rstrip("/")
    png = _b64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    body = _json.dumps({
        "model": "gemma-4",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "color?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64.b64encode(png).decode()}},
        ]}],
        "max_tokens": 4,
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer not-needed"})
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception:  # noqa: BLE001 — any error (incl. 500 "not supported") => no vision
        return False


# A tiny valid PNG (64x64 solid) written to disk for the vision-skill fixtures.
_PNG_64 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00@\x00\x00\x00@\x08\x02\x00\x00\x00%\x0b\xe6\x89"
    b"\x00\x00\x00\x19IDATx\x9c\xed\xc1\x01\r\x00\x00\x00\xc2\xa0\xf7Om\x0e7\xa0\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\xbe\r!\x00\x00\x01\x9a`\xe1\xd5\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _write_png(path) -> str:
    path.write_bytes(_PNG_64)
    return str(path)


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


@pytest.mark.asyncio
async def test_design_critique_critique(tmp_path):
    require_service(_vision_available, "vision-capable GEMMA endpoint (mmproj)")
    img = _write_png(tmp_path / "ui.png")
    r = await _run("design-critique", "critique", {"image_path": img}, timeout=90)
    assert not result_is_error(r)
    assert result_text(r).strip(), "design-critique returned empty"


@pytest.mark.asyncio
async def test_screenshot_to_component_ground(tmp_path):
    require_service(_vision_available, "vision-capable GEMMA endpoint (mmproj)")
    img = _write_png(tmp_path / "shot.png")
    r = await _run("screenshot-to-component", "ground", {"image_path": img}, timeout=90)
    assert not result_is_error(r)
    assert result_text(r).strip(), "screenshot-to-component returned empty"


@pytest.mark.asyncio
async def test_figma_to_component_inspect():
    # Needs a REAL Figma file key + node id (the FIGMA_ACCESS_TOKEN alone is not
    # enough). Provide FIGMA_FILE_KEY + FIGMA_NODE_ID to exercise it.
    require_service(lambda: bool(os.getenv("FIGMA_ACCESS_TOKEN")), "FIGMA_ACCESS_TOKEN")
    key = os.getenv("FIGMA_FILE_KEY")
    node = os.getenv("FIGMA_NODE_ID")
    require_service(lambda: bool(key and node), "FIGMA_FILE_KEY + FIGMA_NODE_ID")
    r = await _run("figma-to-component", "inspect",
                   {"figma_file_key": key, "node_id": node}, timeout=90)
    assert not result_is_error(r)
    assert result_text(r).strip(), "figma-to-component returned empty"


@pytest.mark.asyncio
async def test_pdf_parse_parse(tmp_path):
    # docling downloads/loads models on first run (heavy + slow), so this is
    # opt-in via RUN_HEAVY_PDF=1 to keep the default suite fast.
    require_service(lambda: os.getenv("RUN_HEAVY_PDF") == "1", "RUN_HEAVY_PDF=1 (docling is heavy)")
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 44>>stream\nBT /F1 24 Tf 20 100 Td (Hello PDF) Tj ET\nendstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = await _run("pdf-parse", "parse", {"path": str(pdf)}, timeout=300)
    assert not result_is_error(r)
    assert result_text(r).strip(), "pdf-parse returned empty"
