from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Literal

from .schemas import Critique, ExternalSignals, Issue, Reflection

# Lazy tokenizer: loaded on first use so import of this module does not
# trigger the heavy transformers + sklearn stack at test-collection time.
_TOKENIZER = None


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        from transformers import AutoTokenizer
        _TOKENIZER = AutoTokenizer.from_pretrained("google/gemma-4-9b-it")
    return _TOKENIZER


def _token_diff(a: str, b: str) -> int:
    """Token-level symmetric-difference size between two outputs.

    Uses the Gemma SentencePiece tokenizer (never tiktoken). A small value
    means the revision is materially identical to the previous output, which
    is the computational signature of Degeneration-of-Thought (DoT).
    """
    tok = _get_tokenizer()
    ta = set(tok.encode(a))
    tb = set(tok.encode(b))
    return len(ta.symmetric_difference(tb))


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

    @staticmethod
    def _positions_converge(position_a: str, position_b: str) -> bool:
        """Heuristic: positions converge if their token overlap is high."""
        tok = _get_tokenizer()
        ta = set(tok.encode(position_a))
        tb = set(tok.encode(position_b))
        if not ta or not tb:
            return False
        overlap = len(ta & tb) / len(ta | tb)
        return overlap >= 0.80

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
