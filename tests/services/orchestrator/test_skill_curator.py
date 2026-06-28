from types import SimpleNamespace

import frontmatter as _frontmatter
import pytest

from services.orchestrator import events as _events
from services.orchestrator import skill_curator as sc

HOUR = 3600.0


class _RecordingEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, type, **fields):
        self.events.append((type, fields))


def test_ring_buffer_keeps_only_successful_multitool_sequences():
    buf = sc.RecentSequences(maxlen=3)
    buf.record(sc.CapturedSequence("a", "goal a", ("t1", "t2"), ok=True, ts=1.0))
    buf.record(sc.CapturedSequence("b", "goal b", ("t1",), ok=True, ts=2.0))      # too short
    buf.record(sc.CapturedSequence("c", "goal c", ("t1", "t2"), ok=False, ts=3.0))  # failed
    snap = buf.snapshot()
    assert [s.name for s in snap] == ["a"]


def test_ring_buffer_evicts_oldest_beyond_maxlen():
    buf = sc.RecentSequences(maxlen=2)
    for i in range(4):
        buf.record(sc.CapturedSequence(f"s{i}", "g", ("t1", "t2"), ok=True, ts=float(i)))
    assert [s.name for s in buf.snapshot()] == ["s2", "s3"]


def _state(last_run_at=0.0, paused=False):
    return SimpleNamespace(last_run_at=last_run_at, paused=paused)


def test_gate_closed_before_interval():
    st = _state(last_run_at=0.0)
    # 1h since last run, but interval is 168h
    assert sc.should_run_now(st, now=1 * HOUR, interval_hours=168,
                             min_idle_hours=2, idle_for_s=9999) is False


def test_gate_closed_when_busy():
    st = _state(last_run_at=0.0)
    assert sc.should_run_now(st, now=200 * HOUR, interval_hours=168,
                             min_idle_hours=2, idle_for_s=60) is False


def test_gate_open_after_interval_and_idle():
    st = _state(last_run_at=0.0)
    assert sc.should_run_now(st, now=200 * HOUR, interval_hours=168,
                             min_idle_hours=2, idle_for_s=9999) is True


def test_gate_closed_when_paused():
    st = _state(last_run_at=0.0, paused=True)
    assert sc.should_run_now(st, now=200 * HOUR, interval_hours=168,
                             min_idle_hours=2, idle_for_s=9999) is False


DAY = 86400.0


def test_sweep_archives_long_unused_skill():
    usages = [sc.SkillUsage("old-tool", last_used_at=0.0, success_count=3)]
    verdicts = sc.sweep_transitions(usages, now=100 * DAY)
    assert verdicts["old-tool"] == "archived"


def test_sweep_keeps_recent_skill_active():
    usages = [sc.SkillUsage("calc", last_used_at=100 * DAY - 10, success_count=5)]
    verdicts = sc.sweep_transitions(usages, now=100 * DAY)
    assert verdicts["calc"] == "active"


def test_sweep_marks_idle_skill_stale():
    # idle 20 days: past the 14-day stale line, short of the 60-day archive line
    usages = [sc.SkillUsage("rusty", last_used_at=80 * DAY, success_count=2)]
    verdicts = sc.sweep_transitions(usages, now=100 * DAY)
    assert verdicts["rusty"] == "stale"


@pytest.mark.asyncio
async def test_propose_skill_writes_staged_draft_and_emits(tmp_path):
    root = tmp_path / "skills"
    emitter = _RecordingEmitter()
    token = _events.current_emitter.set(emitter)
    try:
        seq = sc.CapturedSequence(
            "review-fix", "Review then fix app.py",
            ("code-review", "edit_file"), ok=True, ts=1.0,
        )
        out = await sc.propose_skill(root, seq, "Review a file then apply the fix.")
    finally:
        _events.current_emitter.reset(token)

    skill_md = root / ".proposed" / "review-fix" / "SKILL.md"
    stub = root / ".proposed" / "review-fix" / "server.py.stub"
    assert out == skill_md.parent
    assert skill_md.exists() and stub.exists()

    post = _frontmatter.load(str(skill_md))
    assert post["name"] == "review-fix"
    assert post["provenance"] == "agent-created"
    assert "code-review" in post.content and "edit_file" in post.content

    stub_text = stub.read_text(encoding="utf-8")
    assert "NOT FUNCTIONAL" in stub_text
    assert "NotImplementedError" in stub_text

    assert any(
        t == "skill.proposed" and f.get("name") == "review-fix"
        for t, f in emitter.events
    )


@pytest.mark.asyncio
async def test_propose_skill_does_not_touch_active_catalog(tmp_path):
    root = tmp_path / "skills"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "SKILL.md").write_text(
        "---\nname: calc\ndescription: math\n---\nbody\n", encoding="utf-8"
    )
    seq = sc.CapturedSequence("review-fix", "g", ("a", "b"), ok=True, ts=1.0)
    await sc.propose_skill(root, seq, "desc")
    # Active skill dir is untouched; the draft lands ONLY under .proposed/
    assert (root / "calc" / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert not (root / "review-fix").exists()
    assert (root / ".proposed" / "review-fix" / "SKILL.md").exists()
