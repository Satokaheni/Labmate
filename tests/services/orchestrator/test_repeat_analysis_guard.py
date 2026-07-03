from services.orchestrator.repeat_analysis_guard import (
    analysis_key,
    build_analysis_steer,
    is_guarded_analysis,
    repeat_analysis_guard_enabled,
)


def test_default_guarded_skills_include_review_not_sandbox():
    assert is_guarded_analysis("code-review")
    assert is_guarded_analysis("critique")
    assert not is_guarded_analysis("code-sandbox")  # re-running after an edit is legit
    assert not is_guarded_analysis("run_tests")


def test_flag_defaults_on(monkeypatch):
    # Adopted default ON 2026-07-03 after the c2 A/B (no regression, cheaper, curbs churn tail).
    monkeypatch.delenv("ENABLE_REPEAT_ANALYSIS_GUARD", raising=False)
    assert repeat_analysis_guard_enabled() is True
    monkeypatch.setenv("ENABLE_REPEAT_ANALYSIS_GUARD", "0")
    assert repeat_analysis_guard_enabled() is False


def test_key_same_target_ignores_reworded_args():
    a = analysis_key("code-review", {"file": "ab_buggy.py"})
    b = analysis_key("code-review", {"file": "ab_buggy.py", "prompt": "look again more carefully"})
    assert a == b  # defeats the arg-variation evasion the LoopDetector suffers from


def test_key_different_target_differs():
    assert analysis_key("code-review", {"file": "a.py"}) != analysis_key(
        "code-review", {"file": "b.py"}
    )


def test_key_no_target_falls_back_to_skill_only():
    assert analysis_key("critique", {"prompt": "x"}) == "critique"


def test_env_override_of_guarded_set(monkeypatch):
    monkeypatch.setenv("REPEAT_ANALYSIS_SKILLS", "repo-fault-localize, code-review")
    assert is_guarded_analysis("repo-fault-localize")
    assert not is_guarded_analysis("critique")


def test_steer_names_skill_and_target_and_points_to_edit():
    obs = build_analysis_steer("code-review", "code-review::ab_buggy.py")
    assert obs["response"]["status"] == "already_analyzed"
    msg = obs["response"]["message"].lower()
    assert "code-review" in msg and "ab_buggy.py" in msg
    assert "write_file" in msg or "edit" in msg
