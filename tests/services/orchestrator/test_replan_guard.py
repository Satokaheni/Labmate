from __future__ import annotations

from services.orchestrator.replan_guard import normalize_subgoal, count_skill_uses


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
