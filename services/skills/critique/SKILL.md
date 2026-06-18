---
name: critique
description: >
  Grounded reflexion loop for reviewing code and writing output. Gathers external
  signals (test suite, linter, citation validation) before invoking the LLM evaluator.
  Use when you need to review, score, and iteratively improve a piece of code or writing.
  External grounding is mandatory — pure self-critique is prohibited.
trigger: "Use when reviewing or iteratively improving code or writing output"
version: "0.1.0"
license: MIT
requires: []
---

# Critique Skill

A grounded critique-reflexion loop implementing the Reflexion architecture with
mandatory external grounding (CRITIC). It reviews code and writing output, scores
it, and iteratively revises — up to 3 rounds — stopping early on a passing verdict,
on `score >= 0.90`, or on DoT convergence.

## When to use

- Reviewing a code diff or function for bugs, security issues, and style.
- Reviewing an assembled academic draft for clarity, factual grounding, and IMRaD
  structure.
- Any time an output should be scored and iteratively improved against an oracle.

## How it works

1. `ground_with_signals()` runs external oracles FIRST — pytest + ruff for code;
   IMRaD structure check + CoVe factored verification for writing. The test suite
   is ALWAYS run for code critique when a path is provided. Critiquing un-executed
   code is prohibited.
2. The Critic (separate adversarial system prompt, low temperature) produces a
   structured `Critique`, grounded in the signals as quoted evidence.
3. Best-so-far retention discards any revision that scores lower than the prior best.
4. A token-diff convergence check detects Degeneration-of-Thought and exits early.
5. `severity=critical` or any `category=security` issue escalates to a 2-debater +
   judge multi-agent debate; routine quality issues never escalate.

## Usage

```python
from services.skills.critique import CritiqueSkill

skill = CritiqueSkill(lm_client=instructor_wrapped_client)
final_crit, best_output = skill.critique(
    output=code_string,
    task="Implement a thread-safe LRU cache",
    critique_type="code",
    test_suite_path="tests/test_cache.py",
    lint_target="cache.py",
)
```

## Key constraints

- External grounding is mandatory every round (DoT mitigation).
- Token counting uses the Gemma tokenizer, never tiktoken.
- The Critic never sees the generator's chain-of-thought.
- `issues_found` must be non-empty OR `no_issues_justification` must be provided
  (enforced by Pydantic + instructor re-ask).
