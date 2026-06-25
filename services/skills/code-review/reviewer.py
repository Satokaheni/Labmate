"""Multi-angle adversarial code reviewer.

Pipeline:
  1. Ground  — run ruff/mypy on changed files; parse diff metadata
  2. Scan    — one LLM call with 5 analysis angles → up to 40 candidates
  3. Verify  — one LLM call batch-verifies all candidates → CONFIRMED/PLAUSIBLE/REFUTED
  4. Gap     — one fresh-eyes LLM call looking for what scan missed
  5. Rank    — merge, deduplicate by (file, line), sort by severity, return top k
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

from schemas import Candidate, ReviewResult, ScanResult, VerifiedFinding, VerifyResult

log = logging.getLogger("code_review.reviewer")

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

SCAN_SYSTEM = """\
You are an adversarial code reviewer. Your job is to find real bugs — not style nits.
For the provided code diff (or file content), analyze using ALL FIVE angles below and
return ONLY a JSON object matching the schema.

ANALYSIS ANGLES:
A. Correctness    — wrong conditions, off-by-one, null/undefined deref, missing await,
                    falsy-zero checks, wrong-variable copy-paste, swallowed errors.
B. Security       — injection (SQL/command/path), auth bypass, secrets in code,
                    insecure defaults, missing input validation at system boundaries.
C. Removed behavior — for every line DELETED, name the invariant it enforced. If the
                    new code does not re-establish it, that is a finding.
D. Language pitfalls — Python: mutable default args, late-binding closures, bare except.
                    JS/TS: == coercion, closure-captured loop var, missing await on promise.
                    General: float equality, timezone drift, regex metachar escape.
E. Wrapper/proxy correctness — if a class wraps another, verify every method routes
                    to the delegate and does not re-enter a cache or recurse.

OUTPUT SCHEMA (JSON only, no markdown):
{
  "candidates": [
    {
      "file": "path/to/file.py",
      "line": 42,
      "summary": "one sentence naming the bug",
      "failure_scenario": "concrete input/state → wrong output or crash",
      "severity": "critical|high|medium|low",
      "category": "correctness|security|performance|removed_behavior|conventions",
      "angle": "A|B|C|D|E"
    }
  ]
}

Return at most 8 candidates per angle (40 total). If nothing is found for an angle,
omit those entries. Return ONLY the JSON object — no commentary, no code fences.
"""

VERIFY_SYSTEM = """\
You are a code review verifier. For each candidate finding below, decide:
  CONFIRMED — you can name the exact inputs/state that trigger it and the wrong output or crash.
  PLAUSIBLE — the mechanism is real but the trigger is uncertain (timing, env, config).
  REFUTED   — factually wrong (the code doesn't do that) or guarded elsewhere.

Return ONLY a JSON object. Include ONLY CONFIRMED and PLAUSIBLE findings.

OUTPUT SCHEMA (JSON only, no markdown):
{
  "findings": [
    {
      "file": "path/to/file.py",
      "line": 42,
      "summary": "...",
      "failure_scenario": "...",
      "severity": "critical|high|medium|low",
      "category": "correctness|security|performance|removed_behavior|conventions",
      "confidence": "confirmed|plausible"
    }
  ]
}
"""

GAP_SYSTEM = """\
You are a code reviewer doing a final gap sweep. The first pass already found these issues:

{existing}

Re-read the code looking ONLY for real bugs NOT already in that list. Focus on:
- Moved/extracted code that dropped a guard
- Setup/teardown asymmetry (resource leaks, missing cleanup)
- Config defaults that are wrong for production
- Second-order effects: a caller that now receives a different type/shape

Return at most 8 NEW findings in the same JSON schema as before:
{{"candidates": [{{"file": "...", "line": ..., "summary": "...", "failure_scenario": "...", "severity": "critical|high|medium|low", "category": "correctness|security|performance|removed_behavior|conventions", "angle": "gap"}}]}}

If nothing new, return {{"candidates": []}}. ONLY JSON — no commentary.
"""


def _run(cmd: list[str], cwd: str | None = None) -> tuple[str, int]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=30)
        return (r.stdout + r.stderr).strip(), r.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "", -1


def _ground(diff: str | None, path: str | None) -> tuple[str, int]:
    """Run ruff + mypy on changed files. Returns (lint_output, issue_count)."""
    targets: list[str] = []

    if diff:
        for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE):
            p = m.group(1)
            if p.endswith(".py") and Path(p).exists():
                targets.append(p)

    if path and Path(path).exists():
        targets.append(path)

    if not targets:
        return "", 0

    py_targets = [t for t in targets if t.endswith(".py")]
    parts: list[str] = []
    count = 0

    if py_targets:
        out, _ = _run(["ruff", "check", "--output-format=concise"] + py_targets)
        if out:
            parts.append(f"[ruff]\n{out}")
            count += out.count("\n")

        out, _ = _run(
            ["mypy", "--no-error-summary", "--ignore-missing-imports"] + py_targets
        )
        if out:
            parts.append(f"[mypy]\n{out}")
            count += out.count("error:")

    return "\n\n".join(parts), count


def _build_scan_prompt(diff: str | None, path: str | None, lint: str) -> str:
    sections: list[str] = []
    if lint:
        sections.append(f"STATIC ANALYSIS OUTPUT:\n{lint}")
    if diff:
        sections.append(f"GIT DIFF:\n{diff[:24_000]}")  # cap to stay in context
    elif path and Path(path).exists():
        content = Path(path).read_text(errors="replace")[:24_000]
        sections.append(f"FILE CONTENT ({path}):\n{content}")
    return "\n\n".join(sections)


class CodeReviewer:
    def __init__(self, lm_client) -> None:
        self._lm = lm_client

    def review(
        self,
        diff: str | None = None,
        path: str | None = None,
        k: int = 15,
    ) -> ReviewResult:
        if not diff and not path:
            raise ValueError("provide diff or path")

        lint_out, lint_count = _ground(diff, path)
        body = _build_scan_prompt(diff, path, lint_out)

        # --- Scan ---
        scan_raw = self._lm.chat(
            response_model=ScanResult,
            messages=[
                {"role": "system", "content": SCAN_SYSTEM},
                {"role": "user", "content": body},
            ],
            temperature=0.2,
        )
        candidates = scan_raw.candidates

        verified: list[VerifiedFinding] = []
        if candidates:
            # --- Verify ---
            cand_text = "\n".join(
                f"{i+1}. [{c.angle}] {c.file}:{c.line} — {c.summary}\n"
                f"   Scenario: {c.failure_scenario}"
                for i, c in enumerate(candidates)
            )
            verify_raw = self._lm.chat(
                response_model=VerifyResult,
                messages=[
                    {"role": "system", "content": VERIFY_SYSTEM},
                    {
                        "role": "user",
                        "content": f"CODE:\n{body}\n\nCANDIDATES:\n{cand_text}",
                    },
                ],
                temperature=0.1,
            )
            verified = verify_raw.findings

        # --- Gap sweep ---
        existing_summary = "\n".join(
            f"- {f.file}:{f.line} {f.summary}" for f in verified
        ) or "(none yet)"
        gap_raw = self._lm.chat(
            response_model=ScanResult,
            messages=[
                {
                    "role": "system",
                    "content": GAP_SYSTEM.format(existing=existing_summary),
                },
                {"role": "user", "content": body},
            ],
            temperature=0.2,
        )
        for c in gap_raw.candidates:
            verified.append(
                VerifiedFinding(
                    file=c.file,
                    line=c.line,
                    summary=c.summary,
                    failure_scenario=c.failure_scenario,
                    severity=c.severity,
                    category=c.category,
                    confidence="plausible",
                )
            )

        # Deduplicate by (file, line, summary[:40]) then rank
        seen: set[tuple] = set()
        deduped: list[VerifiedFinding] = []
        for f in verified:
            key = (f.file, f.line, f.summary[:40])
            if key not in seen:
                seen.add(key)
                deduped.append(f)

        deduped.sort(
            key=lambda f: (
                SEVERITY_RANK.get(f.severity, 9),
                0 if f.confidence == "confirmed" else 1,
            )
        )

        return ReviewResult(
            findings=deduped[:k],
            lint_issues=lint_count,
            angles_run=5,
        )
