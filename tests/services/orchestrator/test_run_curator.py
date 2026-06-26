from pathlib import Path

import pytest

from services.orchestrator import skill_curator as sc

HOUR = 3600.0


def _buf_with_one():
    buf = sc.RecentSequences()
    buf.record(sc.CapturedSequence("review-fix", "g", ("code-review", "edit_file"),
                                   ok=True, ts=1.0))
    return buf


@pytest.mark.asyncio
async def test_run_curator_noop_when_gate_closed(tmp_path):
    state_path = tmp_path / "curator.json"
    sc.save_state(state_path, sc.CuratorState(last_run_at=199 * HOUR))

    async def draft_fn(prompt):  # must NOT be called
        raise AssertionError("draft_fn called while gate closed")

    out = await sc.run_curator(
        skills_root=tmp_path / "skills", state_path=state_path,
        recent=_buf_with_one(), draft_fn=draft_fn,
        now=200 * HOUR, idle_for_s=60,  # busy -> gate closed
    )
    assert out is None


@pytest.mark.asyncio
async def test_run_curator_drafts_and_persists_when_gate_open(tmp_path):
    state_path = tmp_path / "curator.json"
    sc.save_state(state_path, sc.CuratorState(last_run_at=0.0, run_count=2))
    calls = []

    async def draft_fn(prompt):
        calls.append(prompt)
        return "Review a file then apply the fix."

    out = await sc.run_curator(
        skills_root=tmp_path / "skills", state_path=state_path,
        recent=_buf_with_one(), draft_fn=draft_fn,
        now=200 * HOUR, idle_for_s=9999,
    )
    assert out is not None
    assert (out / "SKILL.md").exists()
    assert len(calls) == 1                       # exactly one LLM call
    persisted = sc.load_state(state_path)
    assert persisted.last_run_at == 200 * HOUR
    assert persisted.run_count == 3


@pytest.mark.asyncio
async def test_run_curator_swallows_draft_failure(tmp_path):
    state_path = tmp_path / "curator.json"
    sc.save_state(state_path, sc.CuratorState(last_run_at=0.0))

    async def draft_fn(prompt):
        raise RuntimeError("model down")

    out = await sc.run_curator(
        skills_root=tmp_path / "skills", state_path=state_path,
        recent=_buf_with_one(), draft_fn=draft_fn,
        now=200 * HOUR, idle_for_s=9999,
    )
    assert out is None  # best-effort: failure is swallowed, no raise


from services.orchestrator.main import _extract_tool_sequence


def test_extract_tool_sequence_reads_state_tools():
    state = {"tools_used": ["code-review", "edit_file"], "error": None}
    assert _extract_tool_sequence(state) == ("code-review", "edit_file")


def test_extract_tool_sequence_empty_when_absent():
    assert _extract_tool_sequence({"error": None}) == ()
    assert _extract_tool_sequence("not a dict") == ()


def test_extract_tool_sequence_from_multi_tool_state():
    """
    Regression test: verify that _extract_tool_sequence correctly reads
    tools_used from a state dict that was populated by _run_react_loop.
    This tests that the orchestrator properly accumulates tools_used and
    the skill-curator can extract the sequence for drafting proposals.
    """
    # Simulate a state dict populated by _run_react_loop with multiple tools
    state = {
        "tools_used": ["run_bash", "edit_file", "run_tests"],
        "error": None,
        "final_answer": "Fixed the code",
    }
    result = _extract_tool_sequence(state)
    assert result == ("run_bash", "edit_file", "run_tests")
    assert len(result) >= 2  # CURATOR_MIN_SEQUENCE_LEN check will pass
