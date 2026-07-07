"""Unit tests for eval/local workspace-portability (cases_for + resolver).

The local eval's fixtures + task strings hardcode the RunPod "/workspace/ab_*.py"
paths. cases_for(root) rebases the TASK strings onto a real workspace root so the
harness (WORKSPACE_PATH=root) is told the same paths reset_fixtures wrote to — the
enabling step for running the eval on a Mac/any client with no /workspace mount.
"""

from __future__ import annotations

import os

from eval.local.cases import CASES, cases_for


def test_cases_for_none_is_noop():
    assert cases_for(None) == [dict(c) for c in CASES]


def test_cases_for_workspace_literal_is_noop():
    # "/workspace" == the RunPod default → byte-identical (baseline comparability).
    assert cases_for("/workspace") == [dict(c) for c in CASES]


def test_cases_for_rebases_task_paths():
    out = cases_for("/Users/x/.data/eval-workspace")
    joined = " ".join(c["task"] for c in out)
    assert "/workspace" not in joined
    assert "/Users/x/.data/eval-workspace/ab_factorial.py" in out[0]["task"]
    assert "/Users/x/.data/eval-workspace/ab_buggy.py" in out[1]["task"]


def test_cases_for_preserves_ids_and_count():
    out = cases_for("/tmp/ws")
    assert [c["id"] for c in out] == [c["id"] for c in CASES]
    assert len(out) == 6  # c1,c2,c3,c6 compound + c4,c5 controls


def test_cases_for_strips_trailing_slash():
    assert "/tmp/ws/ab_off.py" in cases_for("/tmp/ws/")[2]["task"]


def test_resolve_workspace_root_override_sets_env(monkeypatch, tmp_path):
    from eval.local.run_local_eval import _resolve_workspace_root

    monkeypatch.delenv("WORKSPACE_PATH", raising=False)
    target = str(tmp_path / "ws")
    root = _resolve_workspace_root(target)
    assert root == target
    assert os.environ["WORKSPACE_PATH"] == target
    assert os.path.isdir(target)


def test_resolve_workspace_root_env_wins(monkeypatch, tmp_path):
    from eval.local.run_local_eval import _resolve_workspace_root

    env_ws = str(tmp_path / "from_env")
    monkeypatch.setenv("WORKSPACE_PATH", env_ws)
    assert _resolve_workspace_root(None) == env_ws
