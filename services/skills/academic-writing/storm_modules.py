"""STORM-pattern pre-writing research modules for the academic writing pipeline.

STORM's core insight: high-quality long-form writing requires asking the right
questions first. The pre-writing phase simulates diverse expert perspectives and
"interviews" them against the supplied reference material, then synthesizes the
exchanges into structured research notes that seed the outline.

All LLM calls go through DSPy modules (consistent with the rest of the skill) so
prompts can be optimized without touching skill code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import dspy


@dataclass
class Perspective:
    role: str              # e.g. "Machine learning researcher focused on efficiency"
    focus: str             # what aspect of the topic they care about
    questions: list[str] = field(default_factory=list)


@dataclass
class InterviewExchange:
    question: str
    answer: str            # grounded in refs
    cited_refs: list[str] = field(default_factory=list)  # ref ids used in the answer


@dataclass
class ResearchNotes:
    topic: str
    perspectives: list[Perspective]
    exchanges: list[InterviewExchange]
    key_findings: list[str]        # synthesized; feeds outline_skill's key_points
    suggested_sections: list[str]  # section-name suggestions


class GeneratePerspectives(dspy.Signature):
    """Generate N distinct expert perspectives on a research topic.
    Each perspective should represent a different viewpoint: methodology, application,
    theory, critique. Avoid overlapping roles.
    Output JSON: [{"role": str, "focus": str, "questions": [str, ...]}]
    Each perspective must include 2-4 specific, answerable questions about the topic.
    """

    topic = dspy.InputField()
    n_perspectives = dspy.InputField(desc="number of perspectives to generate")
    perspectives = dspy.OutputField(desc="JSON array of perspective objects")


class PerspectiveGenerator(dspy.Module):
    def __init__(self):
        self.gen = dspy.ChainOfThought(GeneratePerspectives)

    def forward(self, topic, n_perspectives) -> dspy.Prediction:
        return self.gen(topic=topic, n_perspectives=str(n_perspectives))


class SimulateInterview(dspy.Signature):
    """Answer a research question as an expert, grounded ONLY in the supplied references.
    Cite specific references by their id using inline [ref_id] markers.
    Do NOT invent facts not present in the references. If the references do not cover
    the question, say so explicitly rather than fabricating an answer.
    """

    question = dspy.InputField()
    references = dspy.InputField(desc="list of {id, title, abstract}")
    answer = dspy.OutputField(desc="grounded answer with [ref_id] inline citations")


class ExpertInterviewer(dspy.Module):
    def __init__(self):
        self.gen = dspy.ChainOfThought(SimulateInterview)

    def forward(self, question, references) -> dspy.Prediction:
        return self.gen(question=question, references=str(references))


class SynthesizeNotes(dspy.Signature):
    """Synthesize research exchanges into structured notes for paper writing.
    key_findings are concise, self-contained claims supported by the exchanges; they
    will become the key_points of the paper outline.
    suggested_sections are candidate IMRaD-compatible section names implied by the findings.
    Output JSON: {"key_findings": [str, ...], "suggested_sections": [str, ...]}
    """

    topic = dspy.InputField()
    exchanges = dspy.InputField(desc="list of {question, answer}")
    notes = dspy.OutputField(desc="JSON synthesis object")


class ResearchNotesSynthesizer(dspy.Module):
    def __init__(self):
        self.gen = dspy.ChainOfThought(SynthesizeNotes)

    def forward(self, topic, exchanges) -> dspy.Prediction:
        return self.gen(topic=topic, exchanges=str(exchanges))
