from __future__ import annotations

from services.orchestrator.replan_guard import normalize_subgoal, count_skill_uses, replan_should_stop, ReplanStop


def test_normalize_lowercases_strips_and_collapses_whitespace():
    assert normalize_subgoal("  Fix  the   Bug ") == "fix bug"


def test_normalize_strips_trailing_punctuation_and_articles():
    # Near-identical phrasings must normalize to the same string so the
    # duplicate guard treats them as the same sub-goal.
    a = normalize_subgoal("Run repo-fault-localize on the module.")
    b = normalize_subgoal("run repo fault localize on module")
    assert a == b


def test_normalize_empty_and_none_safe():
    assert normalize_subgoal("") == ""
    assert normalize_subgoal(None) == ""  # type: ignore[arg-type]


def test_count_skill_uses_counts_across_history_steps():
    history = [
        {"step": "a", "ok": True, "summary": "", "skills": ["repo-fault-localize"]},
        {"step": "b", "ok": True, "summary": "", "skills": ["code-review", "repo-fault-localize"]},
        {"step": "c", "ok": True, "summary": "", "skills": []},
    ]
    assert count_skill_uses(history, "repo-fault-localize") == 2
    assert count_skill_uses(history, "code-review") == 1
    assert count_skill_uses(history, "nonexistent") == 0


def test_count_skill_uses_tolerates_missing_skills_key():
    history = [{"step": "a", "ok": True, "summary": ""}]  # no "skills"
    assert count_skill_uses(history, "anything") == 0


def _h(step, skills=None):
    return {"step": step, "ok": True, "summary": "", "skills": list(skills or [])}


def test_stop_on_identical_consecutive_subgoal():
    history = [_h("Run repo-fault-localize on the module")]
    res = replan_should_stop("run repo fault localize on module", history)
    assert isinstance(res, ReplanStop)
    assert res.stop is True
    assert res.reason == "duplicate_subgoal"


def test_no_stop_on_distinct_subgoals():
    history = [_h("Generate unit tests for factorial")]
    res = replan_should_stop("Fix the off-by-one bug in factorial", history)
    assert res.stop is False
    assert res.reason == ""


def test_stop_when_same_skill_used_beyond_cap():
    # repo-fault-localize already ran twice; cap is 2 -> a 3rd use must stop.
    history = [
        _h("localize the fault", skills=["repo-fault-localize"]),
        _h("localize it again", skills=["repo-fault-localize"]),
    ]
    res = replan_should_stop("localize the fault once more", history, max_skill_repeats=2)
    assert res.stop is True
    assert res.reason == "skill_repeat_cap"


def test_no_stop_when_skill_under_cap():
    history = [_h("localize the fault", skills=["repo-fault-localize"])]
    res = replan_should_stop("now fix the bug", history, max_skill_repeats=2)
    assert res.stop is False


def test_duplicate_check_only_against_most_recent_step():
    # An older identical step that was followed by a DIFFERENT step is not a
    # no-progress loop; only an immediate repeat of the last step trips.
    history = [_h("review the file"), _h("fix the bug")]
    res = replan_should_stop("review the file", history)
    assert res.stop is False


def test_empty_history_never_stops():
    assert replan_should_stop("do anything", []).stop is False


def test_empty_next_subgoal_does_not_falsely_dup():
    history = [_h("")]
    # an empty next is handled by the loop's own done/empty check, not here;
    # the guard must not crash and must not claim a duplicate on empty==empty.
    res = replan_should_stop("", history)
    assert res.stop is False
