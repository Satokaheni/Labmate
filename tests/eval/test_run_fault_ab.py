"""Fixture-based unit tests for the pure fault-injection recovery scorer.

CI-safe: no GEMMA_BASE, no Redis, no model, no orchestrator internals — plain
dataclasses in, plain dataclasses/dicts out. This is the offline-testable core
of the fault-injection A/B harness (eval/orchestrator_ab/run_fault_ab.py); the
live kill+restart runner in the same module is RunPod-only and gated behind
LIVE_TESTS (see TestLiveRunnerGating below).
"""

from __future__ import annotations

import pytest

from eval.orchestrator_ab.run_fault_ab import (
    FaultTrajectory,
    RecoveryScore,
    score_recovery,
    summarize,
)


def _base_traj(**overrides) -> FaultTrajectory:
    """A minimal, otherwise-neutral trajectory fixture; override fields per test."""
    fields = {
        "engine": "lite",
        "kill_point": "mid_execute",
        "turns_before_kill": 3,
        "resumed": True,
        "resumed_from_turn": 3,
        "final_ok": True,
        "expected_ok": True,
    }
    fields.update(overrides)
    return FaultTrajectory(**fields)


class TestScoreRecovery:
    def test_clean_recovery(self):
        traj = _base_traj(
            resumed=True,
            turns_before_kill=3,
            resumed_from_turn=3,
            final_ok=True,
            expected_ok=True,
        )
        score = score_recovery(traj)
        assert score == RecoveryScore(
            recovered=True,
            work_redone=False,
            final_correct=True,
            resume_turn=3,
        )

    def test_resumed_from_scratch_is_work_redone_but_can_still_recover(self):
        # Resumed, but from turn 0 despite having reached turn 3 before the kill:
        # progress was lost, though the final outcome is still correct. This is
        # the checkpoint-quality signal that separates graph vs lite.
        traj = _base_traj(
            resumed=True,
            turns_before_kill=3,
            resumed_from_turn=0,
            final_ok=True,
            expected_ok=True,
        )
        score = score_recovery(traj)
        assert score.recovered is True
        assert score.work_redone is True
        assert score.final_correct is True
        assert score.resume_turn == 0

    def test_did_not_resume_never_recovers_regardless_of_final_ok(self):
        traj = _base_traj(
            resumed=False,
            turns_before_kill=3,
            resumed_from_turn=0,
            final_ok=True,
            expected_ok=True,
        )
        score = score_recovery(traj)
        assert score.recovered is False
        # work_redone requires `resumed` to be True per the brief.
        assert score.work_redone is False
        assert score.final_correct is True

    def test_wrong_terminal_outcome_is_not_recovered(self):
        traj = _base_traj(
            resumed=True,
            turns_before_kill=3,
            resumed_from_turn=3,
            final_ok=False,
            expected_ok=True,
        )
        score = score_recovery(traj)
        assert score.final_correct is False
        assert score.recovered is False

    def test_unknown_kill_point_raises_value_error(self):
        traj = _base_traj(kill_point="during_lunch")
        with pytest.raises(ValueError, match="unknown kill_point"):
            score_recovery(traj)

    def test_known_kill_points_all_score(self):
        for kp in ("before_execute", "mid_execute", "at_approval"):
            traj = _base_traj(kill_point=kp)
            # Should not raise.
            score_recovery(traj)


class TestSummarize:
    def test_aggregates_two_engines_correctly(self):
        trajectories = [
            # graph: 2 runs, both recover cleanly.
            _base_traj(
                engine="graph",
                resumed=True,
                turns_before_kill=2,
                resumed_from_turn=2,
                final_ok=True,
                expected_ok=True,
            ),
            _base_traj(
                engine="graph",
                resumed=True,
                turns_before_kill=4,
                resumed_from_turn=4,
                final_ok=True,
                expected_ok=True,
            ),
            # lite: 2 runs, one clean recovery, one restarted-from-scratch
            # (work redone) but still finalizes correctly.
            _base_traj(
                engine="lite",
                resumed=True,
                turns_before_kill=2,
                resumed_from_turn=2,
                final_ok=True,
                expected_ok=True,
            ),
            _base_traj(
                engine="lite",
                resumed=True,
                turns_before_kill=3,
                resumed_from_turn=0,
                final_ok=True,
                expected_ok=True,
            ),
        ]
        agg = summarize(trajectories)
        assert agg["graph"] == {
            "runs": 2,
            "recovered": 2,
            "work_redone": 0,
            "final_correct": 2,
        }
        assert agg["lite"] == {
            "runs": 2,
            "recovered": 2,
            "work_redone": 1,
            "final_correct": 2,
        }

    def test_empty_list(self):
        assert summarize([]) == {}


class TestLiveRunnerGating:
    """The live kill+restart runner is RunPod-only; it must no-op cleanly in
    CI without importing any orchestrator/GPU internals.
    """

    def test_main_without_live_tests_env_is_a_clean_noop(self, monkeypatch, capsys):
        monkeypatch.delenv("LIVE_TESTS", raising=False)
        from eval.orchestrator_ab.run_fault_ab import main

        result = main(["--engine", "lite", "--kill-point", "mid_execute", "--n", "1"])
        # Should return cleanly (None or 0), never raise, never import orchestrator.
        assert result in (None, 0)
        captured = capsys.readouterr()
        assert "LIVE_TESTS" in captured.out or "RunPod" in captured.out

    def test_main_without_live_tests_does_not_touch_stdout_json_rpc(self, monkeypatch, capsys):
        monkeypatch.delenv("LIVE_TESTS", raising=False)
        from eval.orchestrator_ab.run_fault_ab import main

        main(["--engine", "graph", "--kill-point", "before_execute", "--n", "1"])
        captured = capsys.readouterr()
        # Plain CLI human text only — not a JSON-RPC frame.
        assert not captured.out.strip().startswith("{")

    def test_module_imports_with_zero_orchestrator_deps(self):
        # Importing the pure scorer + dataclasses must not drag in the
        # orchestrator package (graph.py, lite_orchestrator.py, main.py, etc).
        import sys

        before = {m for m in sys.modules if m.startswith("services.orchestrator")}
        from eval.orchestrator_ab.run_fault_ab import (  # noqa: F401
            FaultTrajectory,
            score_recovery,
            summarize,
        )

        after = {m for m in sys.modules if m.startswith("services.orchestrator")}
        assert after == before, f"orchestrator modules imported eagerly: {after - before}"
