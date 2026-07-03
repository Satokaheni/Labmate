"""Tests for eval.provenance module."""

from eval.provenance import (
    TRACKED_FLAGS,
    build_provenance,
    capture_env_flags,
    compare_provenance,
)


def test_capture_env_flags_picks_only_tracked():
    env = {"SEQUENCING_MODE": "react", "ROUTE_EDIT_TO_REACT": "1", "HOME": "/x"}
    snap = capture_env_flags(env)
    assert snap["SEQUENCING_MODE"] == "react"
    assert snap["ROUTE_EDIT_TO_REACT"] == "1"
    assert "HOME" not in snap


def test_build_provenance_shape():
    p = build_provenance(
        "gemma-4-12b-it-UD-Q4_K_XL.gguf", "abc1234", "2026-07-02T10:00:00", {"X": "1"}
    )
    assert p["model"].endswith(".gguf")
    assert p["git_sha"] == "abc1234"
    assert p["captured_at"] == "2026-07-02T10:00:00"
    assert p["env"] == {"X": "1"}


def test_compare_provenance_flags_axis_drift():
    a = build_provenance("31b.gguf", "aaa", "t1", {})
    b = build_provenance("12b.gguf", "bbb", "t2", {})
    warnings = compare_provenance(a, b)
    assert any("model" in w for w in warnings)
    assert any("git_sha" in w for w in warnings)


def test_compare_provenance_clean_when_same():
    a = build_provenance("12b.gguf", "aaa", "t1", {})
    b = build_provenance("12b.gguf", "aaa", "t2", {})
    assert compare_provenance(a, b) == []


def test_tracked_flags_covers_the_load_bearing_ones():
    for f in ("SEQUENCING_MODE", "ROUTE_EDIT_TO_REACT", "MAX_GOAL_ATTEMPTS"):
        assert f in TRACKED_FLAGS
