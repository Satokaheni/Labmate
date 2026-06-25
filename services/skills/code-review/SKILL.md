---
name: code-review
description: >
  Multi-angle adversarial review of SOURCE CODE ONLY — diffs, code files, or
  repositories. Runs 5 independent analysis angles (correctness, security, removed
  behavior, language pitfalls, wrapper correctness), batch-verifies candidates,
  does a gap sweep, and returns code findings ranked by severity. Use ONLY when the
  thing being reviewed is program source code: "review this code/diff/PR", "find
  bugs in foo.py", "audit my changes", "check this file for security issues". Do
  NOT use for reviewing prose, essays, research papers, documentation, or any
  natural-language WRITING — those go to the critique skill (which reviews and
  improves writing). If the target is not code, this skill is the wrong choice.
trigger: "Use ONLY when reviewing program source code (a diff, code file, or repo) — never prose or documents"
tools:
  - code_review
version: "0.1.0"
license: MIT
requires: []
---

# Code Review Skill

Adversarial code review inspired by the multi-angle, verify-then-sweep pattern.
Unlike the `critique` skill (which improves output iteratively), this skill hunts
for bugs and does not revise anything.

## MCP tool

This skill exposes exactly one tool, named **`code_review`** (with an underscore —
NOT the hyphenated skill name). Call it like:

```json
{"tool": "code_review", "arguments": {"diff": "<git diff text>", "path": "<file or dir>", "k": 15}}
```

Provide `diff` OR `path` (at least one); `k` is the optional max findings (default 15).

## When to use

- "Review this diff / PR"
- "Find bugs in `services/foo/bar.py`"
- "Audit the changes I just made"
- "Check this file for security issues"

## How it works

1. **Ground** — runs `ruff` and `mypy` on changed Python files; parse diff metadata.
   External signals are injected into the scan prompt as evidence.

2. **Scan** — single Gemma call with all 5 angles in the system prompt. Returns up
   to 40 raw candidates (8 per angle).

3. **Verify** — second Gemma call batch-verifies all candidates and returns only
   CONFIRMED and PLAUSIBLE findings (REFUTED are dropped).

4. **Gap sweep** — third Gemma call as a fresh reviewer looking only for defects
   not already in the verified list (setup/teardown asymmetry, dropped guards,
   production config defaults, second-order type mismatches).

5. **Rank** — deduplicate by (file, line, summary prefix), sort by severity then
   confidence, return top `k` (default 15).

## Usage

```python
from services.skills.code_review.reviewer import CodeReviewer

reviewer = CodeReviewer(lm_client)

# From a git diff
result = reviewer.review(diff=open("patch.diff").read())

# From a file
result = reviewer.review(path="services/orchestrator/main.py")

for f in result.findings:
    print(f"{f.severity:8s} {f.file}:{f.line}  {f.summary}")
    print(f"         → {f.failure_scenario}")
```

## Output schema

```json
{
  "findings": [
    {
      "file": "services/orchestrator/main.py",
      "line": 142,
      "summary": "Missing await on async_cleanup() leaves connection open",
      "failure_scenario": "When the session ends normally, async_cleanup is scheduled but never awaited — the DB connection pool exhausts after ~100 requests.",
      "severity": "high",
      "category": "correctness",
      "confidence": "confirmed"
    }
  ],
  "lint_issues": 3,
  "angles_run": 5,
  "total": 7
}
```

## Analysis angles

| Angle | What it hunts |
|-------|--------------|
| A — Correctness | Wrong conditions, null deref, missing await, swallowed errors |
| B — Security | Injection, auth bypass, secrets in code, insecure defaults |
| C — Removed behavior | Deleted guards/validations not re-established in new code |
| D — Language pitfalls | Mutable defaults, late-binding, == coercion, float equality |
| E — Wrapper/proxy | Methods that re-enter a cache or recurse instead of delegating |
| Gap sweep | Dropped guards, resource leaks, wrong production defaults |
