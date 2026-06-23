---
name: test-gen
description: >
  Mutation-guided unit test generation. Generates tests for a Python source file,
  runs mutation testing (mutmut), and iteratively improves tests to kill surviving
  mutants. Use when you need high-quality test coverage for agent-written code.
  Requires code-sandbox skill for isolated test execution.
trigger: "Use when generating or improving unit tests for Python code"
tools:
  - generate
  - run_mutations
  - improve
version: "0.1.0"
license: MIT
requires: [code-sandbox]
---

# Test-Gen Skill

You have access to the `test_gen` MCP server, which generates high-quality unit
tests using mutation testing as the quality signal (Meta ACH / arXiv:2501.12862).
A test suite is "good" not when it has coverage, but when it kills mutants —
small semantic faults injected into the source. Surviving mutants are gaps.

## When to Use

Use this skill whenever you have written or modified Python code and need tests
that actually catch regressions, not just tests that execute lines.

## Available Tools

### `generate`

Generate an initial unit test suite for a source file.

```json
{ "source_file": "src/calc.py", "existing_tests": "" }
```

Returns JSON: `{"test_code": "...", "explanation": "..."}`.

### `run_mutations`

Run mutation testing (mutmut) on a source file with a test file. Executes
inside the code-sandbox skill for isolation.

```json
{ "source_file": "src/calc.py", "test_file": "tests/test_calc.py" }
```

Returns JSON: `{"mutation_score": 0.82, "surviving_mutants": ["<diff>", ...],
"killed_count": 18, "total_count": 22, "raw_output": "..."}`.

### `improve`

Given surviving mutants, generate additional tests that target those specific
fault classes.

```json
{
  "source_file": "src/calc.py",
  "test_file": "tests/test_calc.py",
  "surviving_mutants": ["<diff>", "<diff>"]
}
```

Returns JSON: `{"additional_test_code": "..."}`.

## Workflow (the brain orchestrates this loop)

1. Call `generate` to produce an initial test suite. Write `test_code` to a file.
2. Call `run_mutations` on the source + test file. Read `mutation_score` and
   `surviving_mutants`.
3. If `mutation_score` is below target (e.g. 0.90) and mutants survive, call
   `improve` with the surviving mutant diffs. Append `additional_test_code` to
   the test file.
4. Re-run `run_mutations`. Repeat until the score converges (stops improving) or
   the target is reached. Cap iterations (e.g. 4) to avoid runaway loops.

## Notes

- The MCP server is stateless: it does not remember prior iterations. You must
  pass the current test file each round.
- Test execution and mutation runs happen in the code-sandbox; nothing runs on
  the host directly.
- Generated tests are pytest-style.
