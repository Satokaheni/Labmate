"""Unit tests for CodeReviewer — all LLM calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent / "services" / "skills" / "code-review"))

from schemas import Candidate, ReviewResult, ScanResult, VerifiedFinding, VerifyResult
from reviewer import CodeReviewer, _ground, _build_scan_prompt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,4 +1,4 @@
 def divide(a, b):
-    return a / b
+    return a // b
"""

FINDING = VerifiedFinding(
    file="foo.py",
    line=2,
    summary="Integer division silently changes semantics",
    failure_scenario="divide(5, 2) now returns 2 instead of 2.5 — callers expecting float get wrong results",
    severity="high",
    category="correctness",
    confidence="confirmed",
)


def _make_client(scan_candidates=None, verify_findings=None, gap_candidates=None):
    """Build a mock lm_client whose .chat() dispatches on response_model type."""
    client = MagicMock()
    scan_calls = [0]

    def _chat(*, response_model, messages, temperature=0.2):
        if response_model is VerifyResult:
            return VerifyResult(findings=verify_findings or [])
        # ScanResult: first call = scan, subsequent = gap
        scan_calls[0] += 1
        if scan_calls[0] == 1:
            return ScanResult(candidates=scan_candidates or [])
        return ScanResult(candidates=gap_candidates or [])

    client.chat.side_effect = _chat
    return client


# ---------------------------------------------------------------------------
# _ground
# ---------------------------------------------------------------------------

def test_ground_returns_empty_for_nonexistent_path():
    out, count = _ground(diff=None, path="/nonexistent/path/file.py")
    assert out == ""
    assert count == 0


def test_ground_returns_empty_when_no_targets():
    out, count = _ground(diff=None, path=None)
    assert out == ""
    assert count == 0


# ---------------------------------------------------------------------------
# _build_scan_prompt
# ---------------------------------------------------------------------------

def test_scan_prompt_includes_diff():
    prompt = _build_scan_prompt(diff=SIMPLE_DIFF, path=None, lint="")
    assert "GIT DIFF" in prompt
    assert "divide" in prompt


def test_scan_prompt_includes_lint_when_present():
    prompt = _build_scan_prompt(diff=SIMPLE_DIFF, path=None, lint="foo.py:1:1: E501")
    assert "STATIC ANALYSIS" in prompt
    assert "E501" in prompt


# ---------------------------------------------------------------------------
# CodeReviewer.review
# ---------------------------------------------------------------------------

def test_review_raises_without_diff_or_path():
    reviewer = CodeReviewer(_make_client())
    with pytest.raises(ValueError, match="provide diff or path"):
        reviewer.review()


def test_review_returns_verified_findings():
    client = _make_client(
        scan_candidates=[
            Candidate(
                file="foo.py", line=2,
                summary="Integer division silently changes semantics",
                failure_scenario="divide(5,2) returns 2 not 2.5",
                severity="high", category="correctness", angle="A",
            )
        ],
        verify_findings=[FINDING],
    )
    reviewer = CodeReviewer(client)
    result = reviewer.review(diff=SIMPLE_DIFF)

    assert isinstance(result, ReviewResult)
    assert len(result.findings) == 1
    assert result.findings[0].severity == "high"
    assert result.findings[0].confidence == "confirmed"
    assert result.angles_run == 5


def test_review_respects_k_limit():
    many_findings = [
        VerifiedFinding(
            file="f.py", line=i,
            summary=f"bug {i}",
            failure_scenario="...",
            severity="low",
            category="correctness",
            confidence="plausible",
        )
        for i in range(20)
    ]
    client = _make_client(verify_findings=many_findings)
    reviewer = CodeReviewer(client)
    result = reviewer.review(diff=SIMPLE_DIFF, k=5)
    assert len(result.findings) <= 5


def test_review_deduplicates_same_file_line():
    dup = VerifiedFinding(
        file="foo.py", line=2,
        summary="Integer division silently changes semantics",
        failure_scenario="...",
        severity="high", category="correctness", confidence="confirmed",
    )
    # Gap sweep returns the same finding — should be deduplicated
    gap_dup = Candidate(
        file="foo.py", line=2,
        summary="Integer division silently changes semantics",
        failure_scenario="...",
        severity="high", category="correctness", angle="gap",
    )
    client = _make_client(verify_findings=[FINDING], gap_candidates=[gap_dup])
    reviewer = CodeReviewer(client)
    result = reviewer.review(diff=SIMPLE_DIFF)
    assert len(result.findings) == 1


def test_review_sorts_critical_before_low():
    findings = [
        VerifiedFinding(file="f.py", line=1, summary="low bug", failure_scenario="...",
                        severity="low", category="correctness", confidence="plausible"),
        VerifiedFinding(file="f.py", line=2, summary="critical bug", failure_scenario="...",
                        severity="critical", category="security", confidence="confirmed"),
    ]
    dummy_candidate = Candidate(
        file="f.py", line=1, summary="low bug", failure_scenario="...",
        severity="low", category="correctness", angle="A",
    )
    client = _make_client(scan_candidates=[dummy_candidate], verify_findings=findings)
    reviewer = CodeReviewer(client)
    result = reviewer.review(diff=SIMPLE_DIFF)
    assert result.findings[0].severity == "critical"
    assert result.findings[1].severity == "low"


def test_review_empty_scan_still_runs_gap():
    # Even with no scan candidates, gap sweep runs and can surface findings
    gap_cand = Candidate(
        file="foo.py", line=10,
        summary="Unclosed file handle",
        failure_scenario="open() without context manager leaks fd on exception",
        severity="medium", category="correctness", angle="gap",
    )
    client = _make_client(scan_candidates=[], verify_findings=[], gap_candidates=[gap_cand])
    reviewer = CodeReviewer(client)
    result = reviewer.review(diff=SIMPLE_DIFF)
    assert len(result.findings) == 1
    assert result.findings[0].confidence == "plausible"
