from __future__ import annotations

from unittest.mock import patch

import pytest

from services.skills.critique.schemas import Critique, Issue
from services.skills.critique.critique_skill import CritiqueSkill
from tests.services.skills.critique.conftest import MockLM


# ---------------------------------------------------------------------------
# Task 9.2 — Schema validation tests
# ---------------------------------------------------------------------------

def test_critique_rejects_empty_issues_without_justification():
    with pytest.raises(ValueError):
        Critique(
            verdict="pass", severity="low", score=0.95,
            issues_found=[], constitutional_violations=[],
            suggested_revision="", evidence=[], confidence=0.9,
            no_issues_justification=None,
        )


def test_critique_accepts_empty_issues_with_justification():
    crit = Critique(
        verdict="pass", severity="low", score=0.95,
        issues_found=[], constitutional_violations=[],
        suggested_revision="", evidence=[], confidence=0.9,
        no_issues_justification="All tests pass and no lint findings.",
    )
    assert crit.verdict == "pass"


def test_critique_accepts_nonempty_issues():
    crit = Critique(
        verdict="revise", severity="medium", score=0.5,
        issues_found=[Issue(location="f.py:1", category="bug", explanation="x")],
        constitutional_violations=[], suggested_revision="fix",
        evidence=[], confidence=0.7,
    )
    assert len(crit.issues_found) == 1


# ---------------------------------------------------------------------------
# Task 9.3 — ground_with_signals runs pytest
# ---------------------------------------------------------------------------

def test_ground_with_signals_code_runs_pytest():
    skill = CritiqueSkill(lm_client=MockLM())
    with patch("services.skills.critique.critique_skill.subprocess.run") as run:
        run.return_value = type("R", (), {"stdout": "2 passed", "stderr": ""})()
        signals = skill.ground_with_signals(
            output="code", critique_type="code", test_suite_path="tests/test_x.py",
        )
    assert run.call_count == 1
    cmd = run.call_args_list[0].args[0]
    assert cmd[:3] == ["python", "-m", "pytest"]
    assert "tests/test_x.py" in cmd
    assert signals.test_output == "2 passed"


# ---------------------------------------------------------------------------
# Task 9.4 — ground_with_signals runs ruff
# ---------------------------------------------------------------------------

def test_ground_with_signals_code_runs_ruff():
    skill = CritiqueSkill(lm_client=MockLM())
    with patch("services.skills.critique.critique_skill.subprocess.run") as run:
        run.return_value = type("R", (), {"stdout": "All checks passed", "stderr": ""})()
        signals = skill.ground_with_signals(
            output="code", critique_type="code", lint_target="cache.py",
        )
    assert run.call_count == 1
    cmd = run.call_args_list[0].args[0]
    assert cmd[0] == "ruff"
    assert "cache.py" in cmd
    assert signals.lint_output == "All checks passed"


# ---------------------------------------------------------------------------
# Task 9.5 — loop stops at iteration 1 on verdict == "pass"
# ---------------------------------------------------------------------------

def test_critique_stops_on_pass_verdict(make_critique):
    passing = make_critique(verdict="pass", score=0.6)
    lm = MockLM(chat_returns=[passing])
    skill = CritiqueSkill(lm_client=lm)
    with patch.object(skill, "ground_with_signals") as g:
        from services.skills.critique.schemas import ExternalSignals
        g.return_value = ExternalSignals()
        crit, out = skill.critique("code", task="t", critique_type="code")
    assert crit.verdict == "pass"
    assert len(lm.chat_calls) == 1
    assert out == "code"


# ---------------------------------------------------------------------------
# Task 9.6 — loop stops when score >= STOP_THRESHOLD
# ---------------------------------------------------------------------------

def test_critique_stops_on_score_threshold(make_critique):
    high = make_critique(verdict="revise", score=0.92)
    lm = MockLM(chat_returns=[high])
    skill = CritiqueSkill(lm_client=lm)
    with patch.object(skill, "ground_with_signals") as g:
        from services.skills.critique.schemas import ExternalSignals
        g.return_value = ExternalSignals()
        crit, out = skill.critique("code", task="t", critique_type="code")
    assert crit.score >= CritiqueSkill.STOP_THRESHOLD
    assert len(lm.chat_calls) == 1


# ---------------------------------------------------------------------------
# Task 9.7 — best-so-far retention when a revision scores lower
# ---------------------------------------------------------------------------

def test_critique_retains_best_when_revision_scores_lower(make_critique):
    # Round 1: score 0.7 on original (not passing, below threshold).
    # Round 2: score 0.3 on the revision -> discard, keep original.
    # Round 3: score 0.2 on the next revision -> still discard, keep original.
    r1 = make_critique(verdict="revise", severity="low", score=0.7, confidence=0.9)
    r2 = make_critique(verdict="revise", severity="low", score=0.3, confidence=0.9)
    r3 = make_critique(verdict="revise", severity="low", score=0.2, confidence=0.9)
    lm = MockLM(
        chat_returns=[r1, r2, r3],
        complete_returns=[
            "- lesson one",                    # round 1 _reflect
            "REVISED_OUTPUT_TOTALLY_DIFFERENT_TEXT",  # round 1 _refine
            "- lesson two",                    # round 2 _reflect
            "ANOTHER_REVISION_QUITE_DIFFERENT",  # round 2 _refine
        ],
    )
    skill = CritiqueSkill(lm_client=lm)
    # Patch _token_diff to return large diff so convergence never trips.
    with patch.object(skill, "ground_with_signals") as g, \
         patch("services.skills.critique.critique_skill._token_diff", return_value=100):
        from services.skills.critique.schemas import ExternalSignals
        g.return_value = ExternalSignals()
        crit, out = skill.critique("ORIGINAL_OUTPUT", task="t", critique_type="code")
    assert out == "ORIGINAL_OUTPUT"
    assert crit.score == 0.2


# ---------------------------------------------------------------------------
# Task 9.8 — DoT convergence exit when _token_diff < 5
# ---------------------------------------------------------------------------

def test_critique_exits_on_convergence(make_critique):
    r1 = make_critique(verdict="revise", severity="low", score=0.6, confidence=0.9)
    lm = MockLM(chat_returns=[r1], complete_returns=["- lesson", "code"])
    skill = CritiqueSkill(lm_client=lm)
    # Patch _token_diff to return 0 (convergence) so the loop exits after round 1.
    with patch.object(skill, "ground_with_signals") as g, \
         patch("services.skills.critique.critique_skill._token_diff", return_value=0):
        from services.skills.critique.schemas import ExternalSignals
        g.return_value = ExternalSignals()
        crit, out = skill.critique("code", task="t", critique_type="code")
    # Only one evaluator round: refine returned "code" == candidate, token_diff 0 < 5.
    assert len(lm.chat_calls) == 1
    assert out == "code"


# ---------------------------------------------------------------------------
# Task 9.9 — escalation_router for severity=critical
# ---------------------------------------------------------------------------

def test_escalation_router_true_for_critical(make_critique):
    skill = CritiqueSkill(lm_client=MockLM())
    crit = make_critique(severity="critical")
    assert skill.escalation_router(crit) is True


# ---------------------------------------------------------------------------
# Task 9.10 — escalation_router for security-category issue
# ---------------------------------------------------------------------------

def test_escalation_router_true_for_security_issue(make_critique, make_issue):
    skill = CritiqueSkill(lm_client=MockLM())
    crit = make_critique(severity="low", issues_found=[make_issue(category="security")])
    assert skill.escalation_router(crit) is True


# ---------------------------------------------------------------------------
# Task 9.11 — escalation_router returns False for routine medium severity
# ---------------------------------------------------------------------------

def test_escalation_router_false_for_medium_no_security(make_critique, make_issue):
    skill = CritiqueSkill(lm_client=MockLM())
    crit = make_critique(severity="medium", issues_found=[make_issue(category="bug")])
    assert skill.escalation_router(crit) is False


# ---------------------------------------------------------------------------
# Task 9.12 — memory window is bounded to MEMORY_WINDOW
# ---------------------------------------------------------------------------

def test_memory_window_is_bounded(make_critique):
    # Three non-passing rounds, all below threshold, high confidence so each
    # produces a reflection. MAX_ITERS=3 means at most 3 reflections, but we
    # assert _refine never sees more than MEMORY_WINDOW.
    crits = [
        make_critique(verdict="revise", severity="low", score=0.1 * (i + 1), confidence=0.9)
        for i in range(3)
    ]
    # Distinct revisions so convergence never trips.
    revisions = ["alpha beta gamma delta", "epsilon zeta eta theta", "iota kappa lambda mu"]
    complete_returns = []
    for rev in revisions:
        complete_returns.append("- lesson")  # _reflect
        complete_returns.append(rev)          # _refine
    lm = MockLM(chat_returns=crits, complete_returns=complete_returns)
    skill = CritiqueSkill(lm_client=lm)

    seen_memory_sizes = []
    orig_refine = skill._refine

    def spy_refine(task, output, crit, memory):
        seen_memory_sizes.append(len(memory))
        return orig_refine(task, output, crit, memory)

    with patch.object(skill, "ground_with_signals") as g, \
         patch.object(skill, "_refine", side_effect=spy_refine), \
         patch("services.skills.critique.critique_skill._token_diff", return_value=100):
        from services.skills.critique.schemas import ExternalSignals
        g.return_value = ExternalSignals()
        skill.critique("start text here", task="t", critique_type="code")

    assert seen_memory_sizes  # refine was called
    assert max(seen_memory_sizes) <= CritiqueSkill.MEMORY_WINDOW
