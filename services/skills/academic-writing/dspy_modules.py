"""DSPy signatures and modules for the academic writing pipeline.

All LLM calls in the pipeline go through these modules so prompts can be
optimized without touching skill code (STORM pattern).
"""
from __future__ import annotations

import dspy


class GenerateIMRaDOutline(dspy.Signature):
    """Produce an IMRaD outline from a topic and reference list.
    Assign each reference id to the most relevant section.
    Output JSON: {"sections": [{"name": str, "ref_ids": [...], "key_points": [...], "word_budget": int}]}
    Section names must be drawn from: Introduction, Background, Methods,
    Experimental Setup, Results, Discussion, Conclusion.
    Sections must appear in that canonical order.
    """

    topic = dspy.InputField()
    references = dspy.InputField(desc="list of {id, title, abstract}")
    outline = dspy.OutputField(desc="JSON outline as described in the docstring")


class OutlineModule(dspy.Module):
    def __init__(self):
        self.gen = dspy.ChainOfThought(GenerateIMRaDOutline)

    def forward(self, topic, references) -> dspy.Prediction:
        return self.gen(topic=topic, references=str(references))


class DraftSection(dspy.Signature):
    """Draft one section of an academic paper.
    Use ONLY the supplied references and notes.
    Cite with \\cite{key}. Do not cite any key not in the references list.
    Follow the IMRaD role constraints for this section.
    """

    section_name = dspy.InputField()
    references = dspy.InputField(desc="list of {key, title, abstract}")
    notes = dspy.InputField()
    imrad_role = dspy.InputField(desc="what is permitted and prohibited in this section")
    section_text = dspy.OutputField(desc="section text with \\cite{key} inline citations")


class SectionDraftModule(dspy.Module):
    def __init__(self):
        self.gen = dspy.ChainOfThought(DraftSection)

    def forward(self, section_name, references, notes, imrad_role) -> dspy.Prediction:
        return self.gen(
            section_name=section_name,
            references=str(references),
            notes=notes,
            imrad_role=imrad_role,
        )


class InitialSparseSummary(dspy.Signature):
    """Write a SPARSE summary of the source text at exactly the target word count.
    Use FEW named entities. Prioritize readability and narrative flow over coverage.
    Do NOT pack entities. This is the first pass of Chain-of-Density and must start sparse.
    """

    source_text = dspy.InputField()
    target_words = dspy.InputField(desc="exact target word count for the summary")
    summary = dspy.OutputField(desc="sparse summary at the target word count")


class IdentifyMissingEntities(dspy.Signature):
    """Identify 1-3 salient named entities present in the source text but MISSING
    from the current summary. Return only entity names, comma-separated.
    Return an empty string if no salient entities are missing.
    """

    source_text = dspy.InputField()
    current_summary = dspy.InputField()
    missing_entities = dspy.OutputField(desc="1-3 comma-separated entity names, or empty string")


class DensifySummary(dspy.Signature):
    """Rewrite the summary to ADD the given missing entities while HOLDING the word
    count fixed at the target. Fuse and compress existing content to make room; do
    NOT increase length. The rewrite must remain readable.
    """

    current_summary = dspy.InputField()
    missing_entities = dspy.InputField(desc="entities to incorporate")
    target_words = dspy.InputField(desc="word count to hold fixed")
    summary = dspy.OutputField(desc="densified summary at the same word count")


class ChainOfDensityModule(dspy.Module):
    def __init__(self):
        self._sparse = dspy.ChainOfThought(InitialSparseSummary)
        self._identify = dspy.ChainOfThought(IdentifyMissingEntities)
        self._densify = dspy.ChainOfThought(DensifySummary)

    def initial_sparse(self, text: str, target_words: int) -> str:
        return self._sparse(source_text=text, target_words=str(target_words)).summary

    def identify_missing(self, text: str, summary: str) -> list[str]:
        raw = self._identify(source_text=text, current_summary=summary).missing_entities
        return [e.strip() for e in raw.split(",") if e.strip()]

    def densify(self, summary: str, missing_entities: list[str], target_words: int) -> str:
        return self._densify(
            current_summary=summary,
            missing_entities=", ".join(missing_entities),
            target_words=str(target_words),
        ).summary


class TransferStyle(dspy.Signature):
    """Convert the input text from a casual register to formal academic prose.
    Preserve ALL factual content, citations (\\cite{...}), figure references
    (\\ref{...}), and numerical values verbatim. Do not add or remove claims.
    Apply, in order: formality, appropriate hedging, removal of colloquialisms
    and contractions.
    """

    text = dspy.InputField()
    source_style = dspy.InputField()
    target_style = dspy.InputField()
    exemplars = dspy.InputField(desc="few-shot casual->formal sentence pairs")
    transferred_text = dspy.OutputField(desc="formal academic rewrite with all tokens preserved")


class StyleTransferModule(dspy.Module):
    def __init__(self):
        self.gen = dspy.ChainOfThought(TransferStyle)

    def forward(self, text, source_style, target_style, exemplars) -> dspy.Prediction:
        return self.gen(
            text=text,
            source_style=source_style,
            target_style=target_style,
            exemplars=exemplars,
        )
