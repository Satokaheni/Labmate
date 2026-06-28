import pytest

from tests.live.conftest import require_service
from tests.live.skill_harness import (
    manifest_by_name, call_skill_tool, result_text, result_is_error,
    inference_available, SkillRegisterError,
)

pytestmark = pytest.mark.live

_BUG = (
    'def last_index(seq, target):\n'
    '    """Return the index of the LAST occurrence of target, or -1."""\n'
    '    for i in range(len(seq)):\n'
    '        if seq[i] == target:\n'
    '            return i  # bug: returns FIRST match, not last\n'
    '    return -1\n'
)


async def _run(skill_name, tool, args, timeout=120.0):
    require_service(inference_available, "GEMMA_BASE inference server")
    m = manifest_by_name(skill_name)
    if m is None:
        require_service(lambda: False, f"{skill_name} not runnable")
    try:
        return await call_skill_tool(m, tool, args, timeout=timeout)
    except SkillRegisterError as exc:
        require_service(lambda: False, f"{skill_name} register ({exc})")


@pytest.mark.asyncio
async def test_repo_fault_localize_does_not_punt_on_tiny_file(tmp_path):
    (tmp_path / "off.py").write_text(_BUG)
    r = await _run("repo-fault-localize", "locate_files",
                   {"issue": "last_index returns the first match instead of the last",
                    "repo_path": str(tmp_path)})
    assert not result_is_error(r)
    text = result_text(r).lower()
    # the "file too large" punt on a 7-line file is the bug we are guarding against
    assert "too large" not in text and "send a snippet" not in text, \
        "repo-fault-localize punted 'file too large' on a tiny file"
    assert "off.py" in result_text(r), "did not localize the fixture file"


@pytest.mark.asyncio
async def test_critique_runs(tmp_path):
    r = await _run("critique", "critique",
                   {"output": "2 + 2 = 5", "task": "Compute 2 + 2 and report the result."})
    assert not result_is_error(r)
    assert result_text(r).strip(), "critique returned empty"


@pytest.mark.asyncio
async def test_code_review_runs():
    diff = (
        "--- a/m.py\n+++ b/m.py\n@@\n-def avg(x):\n-    return sum(x)/len(x)\n"
        "+def avg(x):\n+    return sum(x)/len(x) + 1\n"
    )
    r = await _run("code-review", "code_review", {"diff": diff})
    assert not result_is_error(r)
    assert result_text(r).strip(), "code-review returned empty"


@pytest.mark.asyncio
async def test_test_gen_generate(tmp_path):
    src = tmp_path / "calc.py"
    src.write_text("def add(a, b):\n    return a + b\n")
    r = await _run("test-gen", "generate", {"source_file": str(src)})
    assert not result_is_error(r)
    assert result_text(r).strip(), "test-gen returned empty"


@pytest.mark.asyncio
async def test_commit_pr_summarize_diff():
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@\n"
        "-def avg(x):\n-    return sum(x)/len(x)\n"
        "+def avg(x):\n+    if not x:\n+        return 0\n+    return sum(x)/len(x)\n"
    )
    r = await _run("commit-pr", "summarize_diff", {"diff_text": diff})
    assert not result_is_error(r)
    assert result_text(r).strip(), "commit-pr returned empty"


@pytest.mark.asyncio
async def test_citation_check_verify_claims():
    r = await _run("citation-check", "verify_claims", {
        "text": "Transformers use self-attention to model sequences.",
        "references": ["Vaswani et al. 2017, Attention Is All You Need — introduces "
                       "the Transformer, which relies entirely on self-attention."],
    })
    assert not result_is_error(r)
    assert result_text(r).strip(), "citation-check returned empty"


@pytest.mark.asyncio
async def test_dataset_generation_from_seeds():
    r = await _run("dataset-generation", "generate_from_seeds", {
        "seeds": ["What is the capital of France?"],
        "template": "Generate a short factual question similar to: {seed}",
        "n_per_seed": 1,
    })
    assert not result_is_error(r)
    assert result_text(r).strip(), "dataset-generation returned empty"


@pytest.mark.asyncio
async def test_rebuttal_response_parse_reviews():
    r = await _run("rebuttal-response", "parse_reviews", {
        "review_text": "Review 1: The method is interesting but the evaluation is weak; "
                       "the baselines are missing and the ablation is incomplete.",
    })
    assert not result_is_error(r)
    assert result_text(r).strip(), "rebuttal-response returned empty"


@pytest.mark.asyncio
async def test_paper_to_slides_generate_outline(tmp_path):
    import json
    paper = tmp_path / "paper.json"
    paper.write_text(json.dumps({
        "metadata": {"title": "A Tiny Study of Widgets"},
        "figures": [],
        "markdown": "# Intro\nWidgets matter.\n# Methods\nWe measured widgets.\n"
                    "# Results\nWidgets improved 10%.\n# Conclusion\nWidgets help.\n",
    }))
    r = await _run("paper-to-slides", "generate_outline",
                   {"parsed_paper_path": str(paper), "talk_duration_min": 5})
    assert not result_is_error(r)
    assert result_text(r).strip(), "paper-to-slides returned empty"


@pytest.mark.asyncio
async def test_academic_writing_style_transfer():
    # academic-writing gained its server.py wrapper (was discoverable-but-unrunnable).
    # style_transfer is the cheapest end-to-end check: pure single LLM pass, no
    # external citation APIs. Proves the DSPy->GEMMA_BASE wiring works through MCP.
    import json
    r = await _run(
        "academic-writing", "style_transfer",
        {"text": "We tried a bunch of models and the big one worked best.",
         "source_style": "casual", "target_style": "formal"},
    )
    assert not result_is_error(r)
    payload = json.loads(result_text(r))
    assert "error" not in payload, f"academic-writing errored: {payload.get('error')}"
    assert payload.get("result", "").strip(), "style_transfer returned empty"
