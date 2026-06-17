from __future__ import annotations
import json
import pytest

from services.orchestrator.types import (
    Status, Goal, State, create_goal, update_status, get_ready_goals, now_iso,
)


@pytest.mark.mocked
class TestStatus:
    def test_all_values_are_strings(self):
        for s in Status:
            assert isinstance(s.value, str)

    def test_required_values_present(self):
        expected = {"PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "BLOCKED", "AWAITING_APPROVAL"}
        assert {s.value for s in Status} == expected


@pytest.mark.mocked
class TestCreateGoal:
    def test_root_goal_has_correct_fields(self):
        tree: dict = {}
        create_goal(tree, "root", None, "Implement feature X")
        g = tree["root"]
        assert g["id"] == "root"
        assert g["parent_id"] is None
        assert g["children"] == []
        assert g["description"] == "Implement feature X"
        assert g["status"] == Status.PENDING.value
        assert g["result"] is None
        assert g["error"] is None
        assert g["attempts"] == 0
        assert g["started_at"] is None
        assert g["updated_at"] is None

    def test_child_goal_is_added_to_parent_children(self):
        tree: dict = {}
        create_goal(tree, "root", None, "Root task")
        create_goal(tree, "child1", "root", "Sub-task 1")
        assert "child1" in tree["root"]["children"]

    def test_create_goal_returns_tree(self):
        tree: dict = {}
        result = create_goal(tree, "g1", None, "desc")
        assert result is tree

    def test_create_goal_missing_parent_does_not_raise(self):
        tree: dict = {}
        create_goal(tree, "orphan", "nonexistent", "orphan task")
        assert "orphan" in tree


@pytest.mark.mocked
class TestUpdateStatus:
    def test_updates_status_field(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        update_status(tree, "g1", Status.IN_PROGRESS)
        assert tree["g1"]["status"] == Status.IN_PROGRESS.value

    def test_sets_updated_at_iso_string(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        update_status(tree, "g1", Status.COMPLETED)
        updated = tree["g1"]["updated_at"]
        assert isinstance(updated, str)
        assert updated.endswith("Z")

    def test_extra_kwargs_are_stored(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        update_status(tree, "g1", Status.COMPLETED, result="output text", error=None)
        assert tree["g1"]["result"] == "output text"
        assert tree["g1"]["error"] is None

    def test_returns_tree(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        result = update_status(tree, "g1", Status.COMPLETED)
        assert result is tree


@pytest.mark.mocked
class TestGetReadyGoals:
    def test_pending_leaf_is_ready(self):
        tree: dict = {}
        create_goal(tree, "root", None, "task")
        ready = get_ready_goals(tree)
        assert len(ready) == 1
        assert ready[0]["id"] == "root"

    def test_goal_with_pending_child_is_not_ready(self):
        tree: dict = {}
        create_goal(tree, "root", None, "parent task")
        create_goal(tree, "child1", "root", "child task")
        ready = get_ready_goals(tree)
        assert all(g["id"] != "root" for g in ready)

    def test_goal_with_all_completed_children_is_ready(self):
        tree: dict = {}
        create_goal(tree, "root", None, "parent task")
        create_goal(tree, "child1", "root", "child task")
        update_status(tree, "child1", Status.COMPLETED)
        ready = get_ready_goals(tree)
        ids = [g["id"] for g in ready]
        assert "root" in ids

    def test_in_progress_goal_is_not_ready(self):
        tree: dict = {}
        create_goal(tree, "g1", None, "task")
        update_status(tree, "g1", Status.IN_PROGRESS)
        assert get_ready_goals(tree) == []

    def test_multiple_independent_pending_leaves_all_returned(self):
        tree: dict = {}
        create_goal(tree, "root", None, "parent")
        create_goal(tree, "c1", "root", "child 1")
        create_goal(tree, "c2", "root", "child 2")
        ready = get_ready_goals(tree)
        ids = {g["id"] for g in ready}
        assert ids == {"c1", "c2"}


@pytest.mark.mocked
class TestStateJsonSerializable:
    def test_state_survives_json_round_trip(self):
        tree: dict = {}
        create_goal(tree, "root", None, "task")
        state = {
            "session_id": "test-session-001",
            "goal_tree": tree,
            "current_goal_id": "root",
            "step_markers": {},
            "messages": [],
            "error": None,
        }
        serialized = json.dumps(state)
        restored = json.loads(serialized)
        assert restored["goal_tree"]["root"]["id"] == "root"
        assert restored["session_id"] == "test-session-001"
        assert restored["goal_tree"]["root"]["status"] == "PENDING"
