# CritiqueSkill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build CritiqueSkill — a grounded reflexion loop that gathers external signals (tests, lint, retrieval) before every LLM evaluator invocation, with best-so-far retention, DoT convergence detection, and escalation to multi-agent debate for critical findings.

**Architecture:** Single-agent two-role design (Generator and Critic use separate system prompts). The Evaluator always runs external oracles (pytest/ruff for code; citation/IMRaD for writing) first, injecting results as quoted evidence into the evaluator prompt. Best-so-far retention prevents reflection poisoning. The token-diff convergence check detects DoT before hitting the iteration cap. Multi-agent debate is gated strictly to severity=critical or security category.

**Tech Stack:** Python 3.11+, `instructor`, `pydantic>=2`, `transformers` (AutoTokenizer for _token_diff), `subprocess` (pytest/ruff), `pytest`

---

## Phase 0 — Scaffolding

### Task 0.1 — Create the skill directory tree

- [ ] Create the directories for the skill and its tests:

```bash
mkdir -p services/skills/critique
mkdir -p tests/services/skills/critique
```

### Task 0.2 — Create `requirements.txt`

- [ ] Write `services/skills/critique/requirements.txt`:

```
instructor>=1.0
pydantic>=2
transformers>=4.40
ruff>=0.4
pytest>=8.0
```

### Task 0.3 — Create package `__init__.py` files

- [ ] Create `services/skills/critique/__init__.py`:

```python
from .critique_skill import CritiqueSkill
from .schemas import Critique, ExternalSignals, Issue, Reflection

__all__ = ["CritiqueSkill", "Critique", "ExternalSignals", "Issue", "Reflection"]
```

- [ ] Create `tests/services/skills/critique/__init__.py` (empty file):

```python
```

---

## Phase 1 — Pydantic Schemas (`schemas.py`)

### Task 1.1 — Write the module header and `Issue` model

- [ ] Create `services/skills/critique/schemas.py` with the header and `Issue`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator


class Issue(BaseModel):
    location: str       # file:line for code; paragraph id for writing; step id for reasoning
    category: Literal["bug", "style", "factual", "security", "logic", "clarity"]
    explanation: str
    grounded_by: str | None = None  # e.g. "pytest:test_auth failed"
```

### Task 1.2 — Add the `Critique` model with the empty-issues validator

- [ ] Append the `Critique` model to `services/skills/critique/schemas.py`:

```python
class Critique(BaseModel):
    verdict: Literal["pass", "revise", "fail"]
    severity: Literal["low", "medium", "high", "critical"]
    score: float                        # 0.0 to 1.0
    issues_found: list[Issue]
    constitutional_violations: list[str]
    suggested_revision: str
    evidence: list[str]                 # external signal quotes (verbatim)
    confidence: float                   # 0.0 to 1.0
    no_issues_justification: str | None = None

    @model_validator(mode="after")
    def require_justification_for_empty_issues(self) -> "Critique":
        if not self.issues_found and not self.no_issues_justification:
            raise ValueError(
                "issues_found is empty but no_issues_justification was not provided. "
                "A critique must explain why no issues were found."
            )
        return self
```

### Task 1.3 — Add the `Reflection` and `ExternalSignals` models

- [ ] Append the remaining models to `services/skills/critique/schemas.py`:

```python
class Reflection(BaseModel):
    lessons: list[str]   # bounded; concrete changes to make on next attempt


class ExternalSignals(BaseModel):
    test_output: str | None = None
    lint_output: str | None = None
    execution_result: str | None = None
    retrieval_snippets: list[str] = []
    cove_verification: list[dict] | None = None   # [{question, answer, contradicts_draft}]
```

---

## Phase 2 — Module-Level Helpers (`critique_skill.py`)

### Task 2.1 — Write the module header and tokenizer-backed `_token_diff`

- [ ] Create `services/skills/critique/critique_skill.py` with the imports, tokenizer, and `_token_diff`. Use the Gemma tokenizer — never tiktoken (project critical rule #3):

```python
from __future__ import annotations

import subprocess
from typing import Literal

from transformers import AutoTokenizer

from .schemas import Critique, ExternalSignals, Issue, Reflection

_TOKENIZER = AutoTokenizer.from_pretrained("google/gemma-4-9b-it")


def _token_diff(a: str, b: str) -> int:
    """Token-level symmetric-difference size between two outputs.

    Uses the Gemma SentencePiece tokenizer (never tiktoken). A small value
    means the revision is materially identical to the previous output, which
    is the computational signature of Degeneration-of-Thought (DoT).
    """
    ta = set(_TOKENIZER.encode(a))
    tb = set(_TOKENIZER.encode(b))
    return len(ta.symmetric_difference(tb))
```

### Task 2.2 — Add the default constitution helper

- [ ] Append `_default_constitution` to `services/skills/critique/critique_skill.py`:

```python
def _default_constitution() -> list[str]:
    """Enumerated written constitution; critiques reference these ids in
    constitutional_violations to keep audits machine-readable (spec C5)."""
    return [
        "C1: Every factual claim must be grounded in an external signal or citation.",
        "C2: Code changes must not introduce security vulnerabilities.",
        "C3: Output must directly address the stated task.",
        "C4: No new unreported numbers or claims may be introduced during revision.",
        "C5: Style and clarity must not regress relative to the prior best output.",
    ]
```

### Task 2.3 — Add the writing-critique structural helper stubs

- [ ] Append the writing-grounding helpers to `services/skills/critique/critique_skill.py`. These are deterministic structural checks used by `ground_with_signals` for `critique_type="writing"`:

```python
def _check_imrad_structure(output: str) -> list[str]:
    """Return a list of structural findings about the draft's IMRaD ordering.

    Deterministic check: confirms canonical section headers appear in order.
    Returns human-readable snippets to inject as evidence (empty list = clean).
    """
    canonical = [
        "Introduction", "Background", "Methods",
        "Experimental Setup", "Results", "Discussion", "Conclusion",
    ]
    found = [name for name in canonical if name.lower() in output.lower()]
    findings: list[str] = []
    last_index = -1
    for name in found:
        idx = canonical.index(name)
        if idx < last_index:
            findings.append(f"IMRaD ordering violation: '{name}' appears out of canonical order.")
        last_index = idx
    if not found:
        findings.append("No recognizable IMRaD section headers found in draft.")
    return findings


def _check_contradiction(answer: str, output: str) -> bool:
    """Cheap deterministic contradiction flag for CoVe factored verification.

    True if the isolated verification answer is non-empty and does not appear
    as a substring of the draft (i.e. the draft cannot be confirmed by it).
    A real implementation may upgrade this to an entailment model.
    """
    answer = answer.strip()
    if not answer:
        return False
    return answer.lower() not in output.lower()
```

---

## Phase 3 — `CritiqueSkill` Class Skeleton

### Task 3.1 — Write the class definition, constants, and `__init__`

- [ ] Append the class skeleton to `services/skills/critique/critique_skill.py`:

```python
class CritiqueSkill:
    """Grounded critique-reflexion loop for code and writing output.

    Design rules:
    - External signals are gathered BEFORE the LLM evaluator is invoked.
    - DoT is mitigated by feeding fresh signals every round and enforcing a
      token-diff convergence check.
    - Best-so-far is always retained; a lower-scoring revision is discarded.
    - Escalation to multi-agent debate is gated to severity=critical/security only.
    """

    MAX_ITERS: int = 3
    STOP_THRESHOLD: float = 0.90
    MIN_CONFIDENCE: float = 0.50
    MEMORY_WINDOW: int = 3

    def __init__(self, lm_client, constitution: list[str] | None = None):
        self._lm = lm_client            # instructor-wrapped LLM client
        self._constitution = constitution or _default_constitution()
```

---

## Phase 4 — External Grounding (`ground_with_signals`)

### Task 4.1 — Implement the code-critique branch (pytest + ruff)

- [ ] Add `ground_with_signals` to the `CritiqueSkill` class. The test suite is ALWAYS run first when a path is provided (spec section 5.3 — critiquing un-executed code is prohibited):

```python
    def ground_with_signals(
        self,
        output: str,
        critique_type: Literal["code", "writing"],
        test_suite_path: str | None = None,
        lint_target: str | None = None,
    ) -> ExternalSignals:
        """Gather all available external signals before the LLM evaluator is called.

        For code critique: ALWAYS runs the test suite and linter if paths are
        provided. Critiquing code that was never executed is prohibited.

        For writing critique: runs IMRaD structure validation and CoVe factored
        verification.
        """
        signals = ExternalSignals()

        if critique_type == "code":
            if test_suite_path:
                result = subprocess.run(
                    ["python", "-m", "pytest", test_suite_path, "--tb=short", "-q"],
                    capture_output=True, text=True, timeout=120,
                )
                signals.test_output = result.stdout + result.stderr
            if lint_target:
                result = subprocess.run(
                    ["ruff", "check", lint_target],
                    capture_output=True, text=True, timeout=30,
                )
                signals.lint_output = result.stdout

        elif critique_type == "writing":
            signals.retrieval_snippets = _check_imrad_structure(output)
            questions = self._plan_verification_questions(output)
            signals.cove_verification = [
                {"question": q, "answer": self._answer_in_isolation(q), "contradicts_draft": None}
                for q in questions
            ]
            for item in signals.cove_verification:
                item["contradicts_draft"] = _check_contradiction(item["answer"], output)

        return signals
```

### Task 4.2 — Add the CoVe factored-verification helpers

- [ ] Add `_plan_verification_questions` and `_answer_in_isolation` to the `CritiqueSkill` class. CoVe answers must be produced WITHOUT the original output in context (spec pitfall — CoVe factored variant):

```python
    def _plan_verification_questions(self, output: str) -> list[str]:
        """Plan factual verification questions about the draft (CoVe planning).

        The draft IS in context here — only the planning step sees it.
        """
        prompt = (
            "Read the following draft and list up to 5 specific, checkable factual "
            "claims as verification questions, one per line:\n\n"
            f"{output}"
        )
        raw = self._lm.complete(prompt)
        return [line.strip("- ").strip() for line in raw.strip().split("\n") if line.strip()][:5]

    def _answer_in_isolation(self, question: str) -> str:
        """Answer a single verification question WITHOUT the draft in context.

        Isolation is mandatory: if the draft were present, the model would parrot
        its original (possibly wrong) reasoning instead of independently checking.
        """
        prompt = (
            "Answer this question independently and concisely, using only your own "
            f"knowledge. Do not assume any prior context:\n\n{question}"
        )
        return self._lm.complete(prompt).strip()
```

---

## Phase 5 — Evaluator, Reflection, and Refinement

### Task 5.1 — Implement `_invoke_evaluator` with separate adversarial critic prompt

- [ ] Add `_invoke_evaluator` to the `CritiqueSkill` class. The critic uses a SEPARATE adversarial system prompt and never sees the generation chain-of-thought (spec section 5.1). `instructor` enforces the `Critique` schema and re-asks on validation failure:

```python
    def _invoke_evaluator(
        self, task: str, output: str, signals: ExternalSignals, memory: list[Reflection]
    ) -> Critique:
        """Invoke the Critic role to produce a structured Critique.

        The Critic is a rigorous external reviewer (adversarial framing, low
        temperature). It receives the external signals as quoted evidence and
        must not contradict them. instructor coerces output into the Critique
        model and re-asks automatically on validation failure (non-empty
        critique contract).
        """
        constitution_text = "\n".join(self._constitution)
        evidence_text = (
            f"test_output:\n{signals.test_output}\n\n"
            f"lint_output:\n{signals.lint_output}\n\n"
            f"execution_result:\n{signals.execution_result}\n\n"
            f"retrieval_snippets:\n{signals.retrieval_snippets}\n\n"
            f"cove_verification:\n{signals.cove_verification}"
        )
        system = (
            "You are a rigorous external reviewer. Your job is to find flaws, not to "
            "affirm. Ground every issue in the supplied external signals. Quote the "
            "signal that grounds each issue in the `evidence` field. If you find no "
            "issues, you MUST provide a no_issues_justification.\n\n"
            f"Constitution (reference principle ids in constitutional_violations):\n{constitution_text}"
        )
        user = (
            f"Task:\n{task}\n\n"
            f"Output under review:\n{output}\n\n"
            f"External signals (authoritative — do not contradict):\n{evidence_text}"
        )
        return self._lm.chat(
            response_model=Critique,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
        )
```

### Task 5.2 — Implement `_reflect`

- [ ] Add `_reflect` to the `CritiqueSkill` class. The reflection prompt sees task, output, and critique — never the generator chain-of-thought (spec section 6.4):

```python
    def _reflect(self, task: str, output: str, crit: Critique) -> Reflection:
        """Convert the evaluator's critique into concise verbal lessons for
        episodic memory. Caps lessons at 5."""
        prompt = (
            f"Task: {task}\n\n"
            f"Output produced:\n{output}\n\n"
            f"Critique:\n{crit.model_dump_json(indent=2)}\n\n"
            "In 2-5 bullet points, write concise lessons about what to change on the "
            "next attempt. Be concrete and actionable. Reference specific issues from "
            "the critique."
        )
        raw = self._lm.complete(prompt)
        lessons = [line.strip("- ").strip() for line in raw.strip().split("\n") if line.strip()]
        return Reflection(lessons=lessons[:5])
```

### Task 5.3 — Implement `_refine`

- [ ] Add `_refine` to the `CritiqueSkill` class. The refiner (Generator role) sees all lessons from the sliding window:

```python
    def _refine(
        self, task: str, output: str, crit: Critique, memory: list[Reflection]
    ) -> str:
        """Produce a revised output conditioned on the prior output and episodic
        memory. The refiner sees all lessons from the sliding window."""
        lessons_text = "\n".join(
            f"- {lesson}" for r in memory for lesson in r.lessons
        )
        prompt = (
            f"Task: {task}\n\n"
            f"Previous output:\n{output}\n\n"
            f"Critique summary:\n{crit.suggested_revision}\n\n"
            f"Lessons from prior rounds:\n{lessons_text}\n\n"
            "Produce a revised output that addresses the critique and applies the "
            "lessons. Do not introduce new issues. Preserve all valid content from "
            "the previous output."
        )
        return self._lm.complete(prompt)
```

---

## Phase 6 — Escalation Router and Multi-Agent Debate

### Task 6.1 — Implement `escalation_router`

- [ ] Add `escalation_router` to the `CritiqueSkill` class. Gated strictly to severity=critical or any security-category issue (spec section 5.5):

```python
    def escalation_router(self, crit: Critique) -> bool:
        """Return True iff the critique should escalate to multi-agent debate.

        Reserved for severity=critical or security-category issues. All other
        issues use single-agent revision to bound inference cost.
        """
        if crit.severity == "critical":
            return True
        if any(issue.category == "security" for issue in crit.issues_found):
            return True
        return False
```

### Task 6.2 — Add debate helper methods

- [ ] Add the debate primitives to the `CritiqueSkill` class:

```python
    def _debate_position(
        self,
        task: str,
        output: str,
        trigger_crit: Critique,
        role: str,
        opposing_position: str | None = None,
    ) -> str:
        """Produce a single debater position. Debater B must rebut A before
        stating its own position."""
        rebut = (
            f"\n\nThe opposing debater argued:\n{opposing_position}\n\n"
            "Explicitly rebut the points above before stating your own position."
            if opposing_position else ""
        )
        prompt = (
            f"You are {role}, a debater reviewing a critical finding.\n\n"
            f"Task: {task}\n\nOutput under review:\n{output}\n\n"
            f"Triggering critique:\n{trigger_crit.model_dump_json(indent=2)}{rebut}"
        )
        return self._lm.complete(prompt)

    def _rebuttal(self, position_a: str, position_b: str) -> str:
        """Debater A sees B's rebuttal and may revise."""
        prompt = (
            f"Debater A, your original position was:\n{position_a}\n\n"
            f"Debater B responded:\n{position_b}\n\n"
            "Respond to B's rebuttal. You may revise your position if warranted."
        )
        return self._lm.complete(prompt)

    def _dissent(self, task: str, output: str, consensus_position: str) -> str:
        """Dissent agent: find flaws in a high-confidence consensus to guard
        against confident-wrong convergence."""
        prompt = (
            f"Two debaters converged on this consensus:\n{consensus_position}\n\n"
            f"Task: {task}\n\nOutput under review:\n{output}\n\n"
            "Your sole job is to find flaws in the consensus. Argue the strongest "
            "case that the consensus is wrong."
        )
        return self._lm.complete(prompt)

    def _judge(
        self,
        task: str,
        output: str,
        position_a: str,
        position_b: str,
        rebuttal_a: str,
        dissent: str | None = None,
    ) -> str:
        """Judge adjudicates both positions plus the rebuttal exchange (and any
        dissent). Returns a textual judgment to be coerced into a Critique."""
        dissent_text = f"\n\nDissent position to address:\n{dissent}" if dissent else ""
        return self._lm.complete(
            f"You are the Judge. Adjudicate the following debate and produce a final "
            f"verdict. You may disagree with both debaters.\n\n"
            f"Task: {task}\n\nOutput:\n{output}\n\n"
            f"Debater A:\n{position_a}\n\nDebater B:\n{position_b}\n\n"
            f"A's rebuttal:\n{rebuttal_a}{dissent_text}"
        )
```

### Task 6.3 — Add the `_positions_converge` helper

- [ ] Add the convergence helper used to decide whether to invoke the Dissent agent:

```python
    @staticmethod
    def _positions_converge(position_a: str, position_b: str) -> bool:
        """Heuristic: positions converge if their token overlap is high."""
        ta = set(_TOKENIZER.encode(position_a))
        tb = set(_TOKENIZER.encode(position_b))
        if not ta or not tb:
            return False
        overlap = len(ta & tb) / len(ta | tb)
        return overlap >= 0.80
```

### Task 6.4 — Implement `run_debate`

- [ ] Add `run_debate` to the `CritiqueSkill` class. Returns `(final_critique, revised_output)` from the judge's verdict (spec section 6.5):

```python
    def run_debate(
        self, task: str, output: str, crit: Critique
    ) -> tuple[Critique, str]:
        """Two-debater + judge multi-agent debate for severity=critical findings.

        Each debater must explicitly rebut the other before the judge adjudicates.
        A Dissent agent is invoked if both debaters converge at high confidence.
        Returns (final_critique, revised_output).
        """
        position_a = self._debate_position(task, output, crit, role="debater_a")
        position_b = self._debate_position(
            task, output, crit, role="debater_b", opposing_position=position_a
        )
        rebuttal_a = self._rebuttal(position_a, position_b)

        if self._positions_converge(position_a, position_b):
            dissent = self._dissent(task, output, position_a)
            judgment = self._judge(
                task, output, position_a, position_b, rebuttal_a, dissent=dissent
            )
        else:
            judgment = self._judge(task, output, position_a, position_b, rebuttal_a)

        final_crit = self._lm.chat(
            response_model=Critique,
            messages=[{"role": "user", "content": judgment}],
        )
        revised = self._refine(task, output, final_crit, memory=[])
        return final_crit, revised
```

---

## Phase 7 — Main Loop (`critique`)

### Task 7.1 — Implement the grounded reflexion loop

- [ ] Add the `critique` method to the `CritiqueSkill` class. This is the orchestrating loop: it grounds with fresh signals every round (DoT mitigation), retains best-so-far, checks convergence, and escalates when gated. Note the best-so-far retention bug in the spec stub is corrected here so a lower-scoring revision is actually discarded:

```python
    def critique(
        self,
        output: str,
        task: str,
        critique_type: Literal["code", "writing"] = "code",
        test_suite_path: str | None = None,
        lint_target: str | None = None,
    ) -> tuple[Critique, str]:
        """Run the grounded reflexion loop. Returns (final_critique, best_output).

        - Fresh external signals are gathered every round (mandatory DoT mitigation).
        - best_output / best_score track the highest-scoring candidate; a revision
          that scores lower than best_score is discarded.
        - Convergence (token_diff < 5) exits early to avoid DoT plateau waste.
        - Escalation to debate is gated to severity=critical / security.
        """
        best_output = output
        best_score = 0.0
        candidate = output
        memory: list[Reflection] = []
        last_crit: Critique | None = None

        for _ in range(self.MAX_ITERS):
            signals = self.ground_with_signals(
                output=candidate,
                critique_type=critique_type,
                test_suite_path=test_suite_path,
                lint_target=lint_target,
            )
            crit = self._invoke_evaluator(task, candidate, signals, memory)
            last_crit = crit

            # Best-so-far retention: only adopt the candidate if it scored higher.
            if crit.score > best_score:
                best_score = crit.score
                best_output = candidate

            if self.escalation_router(crit):
                return self.run_debate(task, best_output, crit)

            if crit.verdict == "pass" or crit.score >= self.STOP_THRESHOLD:
                return crit, best_output

            if crit.confidence >= self.MIN_CONFIDENCE:
                reflection = self._reflect(task, candidate, crit)
                memory = (memory + [reflection])[-self.MEMORY_WINDOW:]

            revised = self._refine(task, candidate, crit, memory)

            # Convergence check: nearly identical revision means DoT — exit.
            if _token_diff(revised, candidate) < 5:
                break

            candidate = revised

        assert last_crit is not None
        return last_crit, best_output
```

---

## Phase 8 — SKILL.md

### Task 8.1 — Write `SKILL.md`

- [ ] Create `services/skills/critique/SKILL.md`:

```markdown
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
```

---

## Phase 9 — Tests

### Task 9.1 — Write `conftest.py` with the mock LLM fixture

- [ ] Create `tests/services/skills/critique/conftest.py`. The mock LLM lets each test script the `chat` (structured `Critique`) and `complete` (free-text) returns:

```python
from __future__ import annotations

import pytest

from services.skills.critique.schemas import Critique, Issue


class MockLM:
    """Scriptable stand-in for an instructor-wrapped LLM client.

    chat_returns: queue of Critique objects returned by .chat()
    complete_returns: queue of strings returned by .complete()
    """

    def __init__(self, chat_returns=None, complete_returns=None):
        self._chat_returns = list(chat_returns or [])
        self._complete_returns = list(complete_returns or [])
        self.chat_calls = []
        self.complete_calls = []

    def chat(self, response_model=None, messages=None, **kwargs):
        self.chat_calls.append({"messages": messages, "kwargs": kwargs})
        return self._chat_returns.pop(0)

    def complete(self, prompt: str) -> str:
        self.complete_calls.append(prompt)
        if self._complete_returns:
            return self._complete_returns.pop(0)
        return ""


@pytest.fixture
def make_issue():
    def _make(category="bug", location="f.py:1", explanation="x", grounded_by=None):
        return Issue(
            location=location, category=category,
            explanation=explanation, grounded_by=grounded_by,
        )
    return _make


@pytest.fixture
def make_critique(make_issue):
    def _make(
        verdict="revise", severity="medium", score=0.5,
        issues_found=None, constitutional_violations=None,
        suggested_revision="fix it", evidence=None, confidence=0.7,
        no_issues_justification=None,
    ):
        return Critique(
            verdict=verdict, severity=severity, score=score,
            issues_found=issues_found if issues_found is not None else [make_issue()],
            constitutional_violations=constitutional_violations or [],
            suggested_revision=suggested_revision,
            evidence=evidence or [],
            confidence=confidence,
            no_issues_justification=no_issues_justification,
        )
    return _make
```

### Task 9.2 — Test the `Critique` empty-issues validator

- [ ] Create `tests/services/skills/critique/test_critique_skill.py` with the schema validation tests:

```python
from __future__ import annotations

from unittest.mock import patch

import pytest

from services.skills.critique.schemas import Critique, Issue


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
```

### Task 9.3 — Test `ground_with_signals` runs pytest for code

- [ ] Append to `test_critique_skill.py`. Mock `subprocess.run` to assert pytest is invoked:

```python
from services.skills.critique.critique_skill import CritiqueSkill
from tests.services.skills.critique.conftest import MockLM


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
```

### Task 9.4 — Test `ground_with_signals` runs ruff for code

- [ ] Append to `test_critique_skill.py`:

```python
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
```

### Task 9.5 — Test the loop stops at iteration 1 on `verdict == "pass"`

- [ ] Append to `test_critique_skill.py`. Patch `ground_with_signals` to avoid subprocess and assert only one evaluator call:

```python
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
```

### Task 9.6 — Test the loop stops when `score >= STOP_THRESHOLD`

- [ ] Append to `test_critique_skill.py`:

```python
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
```

### Task 9.7 — Test best-so-far retention when a revision scores lower

- [ ] Append to `test_critique_skill.py`. Round 1 scores high (0.7) but does not stop; round 2 revision scores lower (0.3). The returned output must be the original (best) candidate:

```python
def test_critique_retains_best_when_revision_scores_lower(make_critique):
    # Round 1: score 0.7 on original (not passing, below threshold).
    # Round 2: score 0.3 on the revision -> discard, keep original.
    r1 = make_critique(verdict="revise", severity="low", score=0.7, confidence=0.9)
    r2 = make_critique(verdict="revise", severity="low", score=0.3, confidence=0.9)
    lm = MockLM(
        chat_returns=[r1, r2],
        complete_returns=["- lesson one", "REVISED_OUTPUT_TOTALLY_DIFFERENT_TEXT"],
    )
    skill = CritiqueSkill(lm_client=lm)
    with patch.object(skill, "ground_with_signals") as g:
        from services.skills.critique.schemas import ExternalSignals
        g.return_value = ExternalSignals()
        crit, out = skill.critique("ORIGINAL_OUTPUT", task="t", critique_type="code")
    assert out == "ORIGINAL_OUTPUT"
    assert crit.score == 0.3
```

### Task 9.8 — Test DoT convergence exit when `_token_diff < 5`

- [ ] Append to `test_critique_skill.py`. The refine returns a near-identical string so the convergence check trips and the loop breaks after one round:

```python
def test_critique_exits_on_convergence(make_critique):
    r1 = make_critique(verdict="revise", severity="low", score=0.6, confidence=0.9)
    lm = MockLM(chat_returns=[r1], complete_returns=["- lesson", "code"])
    skill = CritiqueSkill(lm_client=lm)
    with patch.object(skill, "ground_with_signals") as g:
        from services.skills.critique.schemas import ExternalSignals
        g.return_value = ExternalSignals()
        crit, out = skill.critique("code", task="t", critique_type="code")
    # Only one evaluator round: refine returned "code" == candidate, token_diff 0 < 5.
    assert len(lm.chat_calls) == 1
    assert out == "code"
```

### Task 9.9 — Test `escalation_router` for severity=critical

- [ ] Append to `test_critique_skill.py`:

```python
def test_escalation_router_true_for_critical(make_critique):
    skill = CritiqueSkill(lm_client=MockLM())
    crit = make_critique(severity="critical")
    assert skill.escalation_router(crit) is True
```

### Task 9.10 — Test `escalation_router` for a security-category issue

- [ ] Append to `test_critique_skill.py`:

```python
def test_escalation_router_true_for_security_issue(make_critique, make_issue):
    skill = CritiqueSkill(lm_client=MockLM())
    crit = make_critique(severity="low", issues_found=[make_issue(category="security")])
    assert skill.escalation_router(crit) is True
```

### Task 9.11 — Test `escalation_router` returns False for routine medium severity

- [ ] Append to `test_critique_skill.py`:

```python
def test_escalation_router_false_for_medium_no_security(make_critique, make_issue):
    skill = CritiqueSkill(lm_client=MockLM())
    crit = make_critique(severity="medium", issues_found=[make_issue(category="bug")])
    assert skill.escalation_router(crit) is False
```

### Task 9.12 — Test the memory window is bounded to MEMORY_WINDOW

- [ ] Append to `test_critique_skill.py`. Run the full 3-iteration loop and assert `_refine` never receives more than 3 reflections. We capture the `memory` argument passed to `_refine`:

```python
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
         patch.object(skill, "_refine", side_effect=spy_refine):
        from services.skills.critique.schemas import ExternalSignals
        g.return_value = ExternalSignals()
        skill.critique("start text here", task="t", critique_type="code")

    assert seen_memory_sizes  # refine was called
    assert max(seen_memory_sizes) <= CritiqueSkill.MEMORY_WINDOW
```

### Task 9.13 — Run the full test suite

- [ ] Run the tests and confirm all pass:

```bash
python -m pytest tests/services/skills/critique/ -v
```

---

## Self-Review Checklist

- [ ] All `critique` early-exit paths covered: `verdict=="pass"`, `score >= STOP_THRESHOLD`, convergence, escalation, iteration cap.
- [ ] DoT mitigation present: `ground_with_signals` called inside the loop every round (Task 7.1).
- [ ] Best-so-far retention discards lower-scoring revisions (Task 7.1, tested Task 9.7).
- [ ] Non-empty critique contract enforced by `model_validator` (Task 1.2, tested Task 9.2).
- [ ] Single-agent two-role: separate adversarial Critic system prompt; Critic never sees generation CoT (Task 5.1).
- [ ] Token counting uses Gemma `AutoTokenizer`, never tiktoken (Task 2.1).
- [ ] Escalation gated to severity=critical / security only (Task 6.1, tested 9.9–9.11).
- [ ] CoVe answers produced in isolation without the draft in context (Task 4.2).
- [ ] Memory bounded to `MEMORY_WINDOW=3` (Task 7.1, tested Task 9.12).
- [ ] No placeholder code — every task shows actual code.
- [ ] Type/method names consistent: `Critique`, `Issue`, `Reflection`, `ExternalSignals` used throughout.
