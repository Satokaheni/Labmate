from types import SimpleNamespace

from services.orchestrator import skill_curator as sc

HOUR = 3600.0


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
