from services.orchestrator import skill_curator as sc


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
