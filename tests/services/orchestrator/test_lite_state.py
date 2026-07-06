from __future__ import annotations

from services.orchestrator.lite_state import build_initial_state, restore, snapshot


def test_build_initial_state_matches_run_task_shape():
    s = build_initial_state("do a thing", "sess-1", user_id="u1", workspace_id="w1")
    assert s["session_id"] == "sess-1"
    assert s["current_goal_id"] == "root"
    assert s["root_goal"] == "do a thing"
    assert s["goal_tree"]["root"]["description"] == "do a thing"
    assert s["verify_retries"] == 0 and s["direct_answer"] is False
    assert s["messages"] == [] and s["final_answer"] == ""


def test_snapshot_restore_round_trips():
    import json

    s = build_initial_state("t", "sess")
    snap = snapshot(s)
    assert json.loads(json.dumps(snap)) == snap  # JSON-safe
    assert restore(snap)["goal_tree"]["root"]["description"] == "t"
