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
