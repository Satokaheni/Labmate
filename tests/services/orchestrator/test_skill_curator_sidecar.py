from pathlib import Path

from services.orchestrator import skill_curator as sc


def test_state_roundtrip(tmp_path: Path):
    p = tmp_path / "state" / "curator.json"
    st = sc.CuratorState(last_run_at=123.0, paused=True, run_count=4)
    sc.save_state(p, st)
    loaded = sc.load_state(p)
    assert loaded == st


def test_missing_sidecar_returns_default(tmp_path: Path):
    loaded = sc.load_state(tmp_path / "nope.json")
    assert loaded == sc.CuratorState()
    assert loaded.last_run_at == 0.0
    assert loaded.paused is False
    assert loaded.run_count == 0


def test_corrupt_sidecar_returns_default(tmp_path: Path):
    p = tmp_path / "curator.json"
    p.write_text("{ not json", encoding="utf-8")
    assert sc.load_state(p) == sc.CuratorState()
