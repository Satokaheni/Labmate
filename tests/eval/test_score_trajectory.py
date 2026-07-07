"""Fixture-based unit tests for the pure local-eval scorer.

CI-safe: no GEMMA_BASE, no Redis, no model, no bus — plain dicts in, plain
dicts/lists out. This is the offline-testable core of the local eval harness
(eval/local/score_trajectory.py).
"""

from __future__ import annotations

from eval.local.score_trajectory import _percentile, kpi_reduce, tag_run


def _base_traj(**overrides):
    """A minimal, otherwise-neutral trajectory fixture; override fields per test."""
    traj = {
        "case_id": "c0",
        "task": "do something",
        "mode": "skill_first",
        "ok": True,
        "final_answer": "Done.",
        "tools": [],
        "tool_results_ok": [],
        "tests_passed": False,
        "error_class": None,
        "loop_detected": False,
        "verify_nudges": 0,
        "cancelled": False,
        "llm_calls": 1,
        "peak_prompt_tokens": 100,
        "wall_time_s": 1.0,
        "ttfa_s": 0.5,
    }
    traj.update(overrides)
    return traj


class TestTagRun:
    def test_completed_edited_verified(self):
        traj = _base_traj(
            ok=True,
            tools=["write_file", "run_tests"],
            tool_results_ok=[True, True],
            tests_passed=True,
            final_answer="Fixed the bug and verified it.",
        )
        tags = tag_run(traj)
        assert tags == sorted(
            ["outcome:completed", "edited+verified", "tool:write_file", "tool:run_tests"]
        )

    def test_fabricated(self):
        traj = _base_traj(
            ok=True,
            tools=["write_file"],
            tool_results_ok=[True],
            tests_passed=False,
            final_answer="I fixed the bug, all tests pass.",
        )
        tags = tag_run(traj)
        assert "fabricated" in tags
        assert "outcome:completed" in tags
        assert "edited_unverified" in tags
        assert "edited+verified" not in tags

    def test_fabricated_requires_ok_true(self):
        # Same claim, but ok=False -> not fabricated (it's an honest failure).
        traj = _base_traj(
            ok=False,
            tools=["write_file"],
            tests_passed=False,
            final_answer="I fixed the bug, all tests pass.",
        )
        tags = tag_run(traj)
        assert "fabricated" not in tags
        assert "outcome:errored" in tags

    def test_fabricated_regex_variants(self):
        # Matches the brief's exact regex: \b(all|the)?\s*tests?\s+(pass|passed|
        # passing|green)\b — i.e. "tests <verb>" directly adjacent, optionally
        # preceded by "all"/"the". ("tests are passing" does NOT match — there's
        # a verb between "tests" and "passing" — which is the literal spec.)
        for phrase in (
            "All tests pass.",
            "the test passed successfully",
            "tests passing now",
            "Tests green.",
        ):
            traj = _base_traj(ok=True, tests_passed=False, final_answer=phrase)
            assert "fabricated" in tag_run(traj), phrase

    def test_fabricated_regex_does_not_match_non_adjacent_phrasing(self):
        # A verb between "tests" and "passing" falls outside the brief's literal
        # regex (\btests?\s+(pass|passed|passing|green)\b requires adjacency) —
        # documented here so the boundary is a deliberate, tested choice.
        traj = _base_traj(ok=True, tests_passed=False, final_answer="tests are passing now")
        assert "fabricated" not in tag_run(traj)

    def test_not_fabricated_when_tests_actually_passed(self):
        traj = _base_traj(
            ok=True,
            tools=["write_file"],
            tests_passed=True,
            final_answer="I fixed the bug, all tests pass.",
        )
        tags = tag_run(traj)
        assert "fabricated" not in tags
        assert "edited+verified" in tags

    def test_doom_loop(self):
        traj = _base_traj(ok=False, loop_detected=True)
        tags = tag_run(traj)
        assert "outcome:doom_loop" in tags
        assert "doom_looped" in tags
        assert "outcome:completed" not in tags
        assert "outcome:errored" not in tags

    def test_doom_loop_precedence_over_ok(self):
        # Per the brief's precedence, ok wins first (outcome:completed), so a
        # trajectory can only be tagged outcome:doom_loop when ok is False.
        traj = _base_traj(ok=True, loop_detected=True)
        tags = tag_run(traj)
        assert "outcome:completed" in tags
        assert "doom_looped" in tags  # doom_looped is independent of the outcome tag
        assert "outcome:doom_loop" not in tags

    def test_errored(self):
        traj = _base_traj(ok=False, loop_detected=False, cancelled=False)
        tags = tag_run(traj)
        assert tags == ["outcome:errored"]

    def test_cancelled(self):
        traj = _base_traj(ok=False, loop_detected=False, cancelled=True)
        tags = tag_run(traj)
        assert "outcome:cancelled" in tags
        assert "outcome:errored" not in tags
        assert "outcome:doom_loop" not in tags

    def test_edited_unverified(self):
        traj = _base_traj(
            ok=True,
            tools=["write_file"],
            tool_results_ok=[True],
            tests_passed=False,
            final_answer="Made the edit.",
        )
        tags = tag_run(traj)
        assert "edited_unverified" in tags
        assert "edited+verified" not in tags
        assert "fabricated" not in tags  # no claim of tests passing

    def test_call_skill_tool_counts_as_edit(self):
        traj = _base_traj(
            ok=True,
            tools=["call_skill_tool", "run_tests"],
            tool_results_ok=[True, True],
            tests_passed=True,
        )
        tags = tag_run(traj)
        assert "edited+verified" in tags

    def test_no_edit_tools_no_edit_tags(self):
        traj = _base_traj(ok=True, tools=["read_file"], tool_results_ok=[True])
        tags = tag_run(traj)
        assert "edited+verified" not in tags
        assert "edited_unverified" not in tags

    def test_verify_nudged(self):
        traj = _base_traj(ok=True, verify_nudges=1)
        tags = tag_run(traj)
        assert "verify_nudged" in tags

    def test_no_verify_nudge_when_zero(self):
        traj = _base_traj(ok=True, verify_nudges=0)
        tags = tag_run(traj)
        assert "verify_nudged" not in tags

    def test_distinct_tool_tags(self):
        traj = _base_traj(
            ok=True,
            tools=["read_file", "write_file", "read_file", "run_tests"],
            tool_results_ok=[True, True, True, True],
            tests_passed=True,
        )
        tags = tag_run(traj)
        assert tags.count("tool:read_file") == 1
        assert "tool:write_file" in tags
        assert "tool:run_tests" in tags

    def test_tags_are_sorted(self):
        traj = _base_traj(
            ok=True,
            tools=["write_file", "run_tests"],
            tool_results_ok=[True, True],
            tests_passed=True,
        )
        tags = tag_run(traj)
        assert tags == sorted(tags)


class TestKpiReduce:
    def _batch(self):
        # 4 fixtures: 1 fabricated, 1 doom-loop, 1 clean completed+edited+verified,
        # 1 plain errored. Mirrors the brief's "~4 fixtures, 1 fabricated of 4 -> 0.25".
        return [
            _base_traj(
                case_id="c1",
                ok=True,
                tools=["write_file", "run_tests"],
                tool_results_ok=[True, True],
                tests_passed=True,
                final_answer="Fixed and verified.",
                llm_calls=3,
                wall_time_s=2.0,
                ttfa_s=0.2,
            ),
            _base_traj(
                case_id="c2",
                ok=True,
                tools=["write_file"],
                tool_results_ok=[True],
                tests_passed=False,
                final_answer="I fixed it, all tests pass.",
                llm_calls=2,
                wall_time_s=1.0,
                ttfa_s=0.1,
            ),
            _base_traj(
                case_id="c3",
                ok=False,
                loop_detected=True,
                tools=["run_bash", "run_bash"],
                tool_results_ok=[True, False],
                llm_calls=6,
                wall_time_s=5.0,
                ttfa_s=0.4,
            ),
            _base_traj(
                case_id="c4",
                ok=False,
                tools=[],
                tool_results_ok=[],
                llm_calls=1,
                wall_time_s=0.5,
                ttfa_s=None,
            ),
        ]

    def test_ok_rate(self):
        kpis = kpi_reduce(self._batch())
        assert kpis["n"] == 4
        assert kpis["ok_rate"] == 0.5  # c1, c2 ok

    def test_fabricated_rate(self):
        kpis = kpi_reduce(self._batch())
        assert kpis["fabricated_rate"] == 0.25  # only c2

    def test_doom_loop_rate(self):
        kpis = kpi_reduce(self._batch())
        assert kpis["doom_loop_rate"] == 0.25  # only c3

    def test_edited_verified_rate(self):
        kpis = kpi_reduce(self._batch())
        assert kpis["edited_verified_rate"] == 0.25  # only c1

    def test_verify_nudge_rate_zero(self):
        kpis = kpi_reduce(self._batch())
        assert kpis["verify_nudge_rate"] == 0.0

    def test_tool_success_rate_mix(self):
        kpis = kpi_reduce(self._batch())
        # tool_results_ok across the batch: [True, True] + [True] + [True, False] + []
        # = 4 ok / 5 total = 0.8
        assert kpis["tool_success_rate"] == 0.8

    def test_avg_llm_calls(self):
        kpis = kpi_reduce(self._batch())
        assert kpis["avg_llm_calls"] == (3 + 2 + 6 + 1) / 4

    def test_ttfa_percentiles(self):
        kpis = kpi_reduce(self._batch())
        # non-null ttfa_s across the batch: [0.2, 0.1, 0.4] (c4's None excluded) -> sorted [0.1, 0.2, 0.4]
        assert kpis["ttfa_p50"] == _percentile([0.1, 0.2, 0.4], 50)
        assert kpis["ttfa_p95"] == _percentile([0.1, 0.2, 0.4], 95)

    def test_wall_percentiles(self):
        kpis = kpi_reduce(self._batch())
        sorted_wall = sorted([2.0, 1.0, 5.0, 0.5])
        assert kpis["wall_p50"] == _percentile(sorted_wall, 50)
        assert kpis["wall_p95"] == _percentile(sorted_wall, 95)

    def test_tag_counts(self):
        kpis = kpi_reduce(self._batch())
        tag_counts = kpis["tag_counts"]
        assert tag_counts["outcome:completed"] == 2
        assert tag_counts["outcome:doom_loop"] == 1
        assert tag_counts["outcome:errored"] == 1
        assert tag_counts["fabricated"] == 1
        assert tag_counts["edited+verified"] == 1
        assert tag_counts["edited_unverified"] == 1
        assert tag_counts["doom_looped"] == 1

    def test_empty_batch(self):
        kpis = kpi_reduce([])
        assert kpis["n"] == 0
        assert kpis["ok_rate"] == 0.0
        assert kpis["fabricated_rate"] == 0.0
        assert kpis["tool_success_rate"] == 1.0  # 0 tools -> 1.0 per the brief
        assert kpis["tag_counts"] == {}

    def test_no_tools_anywhere_gives_perfect_tool_success(self):
        trajs = [_base_traj(ok=True, tools=[], tool_results_ok=[])]
        kpis = kpi_reduce(trajs)
        assert kpis["tool_success_rate"] == 1.0


class TestPercentile:
    def test_known_p50(self):
        assert _percentile([1, 2, 3, 4], 50) == 2.5

    def test_known_p95(self):
        # linear interpolation: rank = 0.95 * 3 = 2.85 -> between index 2 (3) and 3 (4)
        assert abs(_percentile([1, 2, 3, 4], 95) - 3.85) < 1e-9

    def test_empty_is_zero(self):
        assert _percentile([], 50) == 0.0
        assert _percentile([], 95) == 0.0

    def test_single_value(self):
        assert _percentile([7.0], 50) == 7.0
        assert _percentile([7.0], 95) == 7.0

    def test_p0_is_min(self):
        assert _percentile([1, 2, 3, 4], 0) == 1

    def test_p100_is_max(self):
        assert _percentile([1, 2, 3, 4], 100) == 4
