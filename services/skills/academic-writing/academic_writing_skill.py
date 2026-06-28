"""AcademicWritingSkill — composable IMRaD academic writing pipeline.

A plain Python pipeline class (DSPy modules back every LLM call); each method is
independently importable and testable. It is NOT itself an MCP server — server.py
in this directory wraps these methods as MCP tools so the skill-worker can
discover and dispatch it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import dspy

from citation_validator import (
    CitationResult,
    CitationValidator,
    deduplicate_keys,
)
from dspy_modules import (
    ChainOfDensityModule,
    OutlineModule,
    SectionDraftModule,
    StyleTransferModule,
)
from storm_modules import (
    ExpertInterviewer,
    InterviewExchange,
    Perspective,
    PerspectiveGenerator,
    ResearchNotes,
    ResearchNotesSynthesizer,
)

MAX_COD_ITERATIONS = 3
COD_WORD_TOLERANCE = 0.05  # +/- 5% allowed around target_words
TOKENIZER_NAME = "google/gemma-4-9b-it"


@dataclass
class Ref:
    id: str
    title: str
    abstract: str
    bibtex: str
    doi: str | None = None
    arxiv_id: str | None = None


@dataclass
class Section:
    name: str
    ref_ids: list[str]
    key_points: list[str]
    word_budget: int = 500


@dataclass
class Outline:
    sections: list[Section]


IMRAD_ORDER = [
    "Abstract",
    "Introduction",
    "Background",
    "Methods",
    "Experimental Setup",
    "Results",
    "Discussion",
    "Conclusion",
    "References",
]

_IMRAD_ROLES = {
    "Introduction": "Permitted: motivation, problem statement, contributions. "
                    "Prohibited: results, conclusions.",
    "Background": "Permitted: related work, prior techniques. Prohibited: novel claims.",
    "Methods": "Permitted: approach, algorithm, design. "
               "Prohibited: results, evaluation numbers.",
    "Experimental Setup": "Permitted: datasets, configuration, hardware. "
                          "Prohibited: result numbers, interpretation.",
    "Results": "Permitted: figures, tables, numbers from the supplied notes only. "
               "Prohibited: interpretation.",
    "Discussion": "Permitted: interpretation, limitations, future work. "
                  "Prohibited: new unreported numbers.",
    "Conclusion": "Permitted: summary of contributions. "
                  "Prohibited: new claims not in Results.",
}


def _imrad_role_description(section_name: str) -> str:
    return _IMRAD_ROLES.get(section_name, "Follow standard academic conventions for this section.")


def _parse_outline_json(raw: str) -> list[Section]:
    """Parse the DSPy outline JSON output into Section objects.

    Tolerates leading/trailing prose around the JSON object.
    """
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"Outline output contained no JSON object: {raw[:200]!r}")
    data = json.loads(m.group(0))
    sections = []
    for s in data.get("sections", []):
        sections.append(
            Section(
                name=s["name"],
                ref_ids=list(s.get("ref_ids", [])),
                key_points=list(s.get("key_points", [])),
                word_budget=int(s.get("word_budget", 500)),
            )
        )
    return sections


def _parse_perspectives_json(raw: str) -> list[Perspective]:
    """Parse the PerspectiveGenerator JSON array into Perspective objects.

    Tolerates leading/trailing prose around the JSON array.
    """
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise ValueError(f"Perspectives output contained no JSON array: {raw[:200]!r}")
    data = json.loads(m.group(0))
    perspectives = []
    for p in data:
        perspectives.append(
            Perspective(
                role=p["role"],
                focus=p.get("focus", ""),
                questions=list(p.get("questions", [])),
            )
        )
    return perspectives


def _parse_notes_json(raw: str) -> tuple[list[str], list[str]]:
    """Parse the SynthesizeNotes JSON object into (key_findings, suggested_sections)."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"Notes output contained no JSON object: {raw[:200]!r}")
    data = json.loads(m.group(0))
    key_findings = list(data.get("key_findings", []))
    suggested_sections = list(data.get("suggested_sections", []))
    return key_findings, suggested_sections


def _extract_cited_refs(answer: str, allowed_ids: set[str]) -> list[str]:
    """Collect [ref_id] markers in the answer, keeping only ids in the allowlist.

    Prevents the interviewer from "citing" refs that were never supplied.
    """
    found = re.findall(r"\[([^\]]+)\]", answer)
    return [rid.strip() for rid in found if rid.strip() in allowed_ids]


def _bibtex_key(bibtex: str) -> str:
    """Extract the citation key from a BibTeX entry string."""
    m = re.search(r"@\w+\s*\{\s*([^,]+),", bibtex)
    if not m:
        raise ValueError(f"Cannot extract bibtex key from entry: {bibtex[:80]!r}")
    return m.group(1).strip()


_tokenizer = None


def _get_tokenizer():
    """Lazy-load the Gemma tokenizer so tests don't need a GPU environment."""
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_NAME)
    return _tokenizer


def _count_words(text: str) -> int:
    return len(text.split())


def _enforce_word_count(text: str, target_words: int) -> str:
    """Truncate to target_words if the text exceeds it. Word count must never grow."""
    words = text.split()
    if len(words) > target_words:
        return " ".join(words[:target_words])
    return text


_PROTECTED_RE = re.compile(r"\\cite\{[^}]*\}|\\ref\{[^}]*\}|\d+(?:\.\d+)?")


def _extract_protected_tokens(text: str) -> list[str]:
    """Collect all \\cite{}, \\ref{}, and numeric tokens in order of appearance."""
    return _PROTECTED_RE.findall(text)


def _protected_tokens_intact(out: str, original_tokens: list[str]) -> bool:
    """True iff the multiset of protected tokens is unchanged after transfer."""
    return sorted(_extract_protected_tokens(out)) == sorted(original_tokens)


def _load_formal_exemplars() -> str:
    return (
        "casual: We tried a bunch of models and the big one worked best.\n"
        "formal: We evaluated several models; the largest configuration achieved the best performance.\n"
        "casual: It's pretty obvious this helps a lot.\n"
        "formal: These results suggest a substantial improvement.\n"
        "casual: We didn't see any problems with the data.\n"
        "formal: No anomalies were observed in the dataset."
    )


class AcademicWritingSkill:
    """Composable skill set for producing IMRaD-structured academic papers.

    Each method is independently testable and invokable. This class is not itself
    an MCP server; server.py wraps these methods as MCP tools.
    """

    def __init__(self, lm: dspy.LM, citation_validator: CitationValidator | None = None):
        self._lm = lm
        dspy.configure(lm=lm)
        self._outline_module = OutlineModule()
        self._section_module = SectionDraftModule()
        self._cod_module = ChainOfDensityModule()
        self._tst_module = StyleTransferModule()
        self._perspective_module = PerspectiveGenerator()
        self._interview_module = ExpertInterviewer()
        self._synthesis_module = ResearchNotesSynthesizer()
        self._validator = citation_validator or CitationValidator()

    def research_topic(self, topic: str, refs: list[Ref],
                       n_perspectives: int = 3) -> ResearchNotes:
        """STORM-style perspective-guided pre-writing research phase.

        Generates `n_perspectives` diverse expert perspectives, interviews each one's
        questions against the supplied references (grounded answers only), then
        synthesizes the exchanges into structured ResearchNotes. The resulting
        `key_findings` are intended to seed `outline_skill` (Section.key_points).

        Raises ValueError if perspective generation yields no usable perspectives.
        """
        ref_dicts = [{"id": r.id, "title": r.title, "abstract": r.abstract} for r in refs]
        allowed_ids = {r.id for r in refs}

        # Stage 1: generate diverse expert perspectives.
        p_result = self._perspective_module(topic=topic, n_perspectives=n_perspectives)
        perspectives = _parse_perspectives_json(p_result.perspectives)
        if not perspectives:
            raise ValueError("research_topic: perspective generation returned no perspectives")

        # Stage 2: interview every question across all perspectives, grounded in refs.
        exchanges: list[InterviewExchange] = []
        for perspective in perspectives:
            for question in perspective.questions:
                i_result = self._interview_module(question=question, references=ref_dicts)
                answer = i_result.answer
                exchanges.append(
                    InterviewExchange(
                        question=question,
                        answer=answer,
                        cited_refs=_extract_cited_refs(answer, allowed_ids),
                    )
                )

        # Stage 3: synthesize the exchanges into structured notes.
        s_input = [{"question": e.question, "answer": e.answer} for e in exchanges]
        s_result = self._synthesis_module(topic=topic, exchanges=s_input)
        key_findings, suggested_sections = _parse_notes_json(s_result.notes)

        return ResearchNotes(
            topic=topic,
            perspectives=perspectives,
            exchanges=exchanges,
            key_findings=key_findings,
            suggested_sections=suggested_sections,
        )

    def outline_skill(self, topic: str, refs: list[Ref]) -> Outline:
        """STORM two-stage outline: cluster refs by IMRaD section, emit fixed scaffold.

        Returns an Outline with sections in canonical IMRAD_ORDER.
        Raises ValueError if any mandatory IMRaD section is missing.
        """
        ref_dicts = [{"id": r.id, "title": r.title, "abstract": r.abstract} for r in refs]
        result = self._outline_module(topic=topic, references=ref_dicts)
        outline = Outline(sections=_parse_outline_json(result.outline))

        produced = {s.name for s in outline.sections}
        required = [s for s in IMRAD_ORDER if s not in ("Abstract", "References")]
        missing = [s for s in required if s not in produced]
        if missing:
            raise ValueError(f"Outline missing required IMRaD sections: {missing}")

        order_map = {name: i for i, name in enumerate(IMRAD_ORDER)}
        outline.sections.sort(key=lambda s: order_map.get(s.name, 99))
        return outline

    def draft_section(self, section_name: str, refs: list[Ref], notes: str) -> str:
        """Per-section draft. Never drafts the whole paper in one call.

        Returns markdown/LaTeX with inline \\cite{key} citations.
        Raises ValueError if the output cites a key not in the supplied refs.
        """
        valid_keys = {_bibtex_key(r.bibtex) for r in refs}
        ref_context = [
            {"key": _bibtex_key(r.bibtex), "title": r.title, "abstract": r.abstract}
            for r in refs
        ]

        result = self._section_module(
            section_name=section_name,
            references=ref_context,
            notes=notes,
            imrad_role=_imrad_role_description(section_name),
        )
        text = result.section_text

        cited_keys = set(re.findall(r"\\cite\{([^}]+)\}", text))
        unknown = cited_keys - valid_keys
        if unknown:
            raise ValueError(
                f"Section '{section_name}' cited unvalidated keys: {sorted(unknown)}. "
                "Re-invoke with an explicit key allowlist."
            )
        return text

    def validate_citations(self, bibtex_entries: list[str]) -> list[CitationResult]:
        """Deterministic-first citation validation cascade. Non-optional.

        Cascade: DOI -> Crossref, arXiv -> arXiv API, title -> Semantic Scholar,
        LLM fallback (advisory only). Callers MUST filter to valid=True before
        including any entry in the bibliography. Colliding keys are disambiguated
        with a/b/c suffixes.
        """
        results = self._validator.validate(bibtex_entries)
        return deduplicate_keys(results)

    def chain_of_density(self, text: str, target_words: int,
                         iterations: int = MAX_COD_ITERATIONS) -> str:
        """Iterative Chain-of-Density summarization (Adams et al. 2023).

        Starts SPARSE; adds 1-3 missing salient entities per iteration while holding
        word count == target_words. Starting dense is the most common misapplication
        and is explicitly avoided here. Word count must not increase across iterations.
        """
        summary = self._cod_module.initial_sparse(text, target_words)
        summary = _enforce_word_count(summary, target_words)

        for _ in range(iterations):
            missing = self._cod_module.identify_missing(text, summary)
            if not missing:
                break  # converged; no salient entities remain to add
            summary = self._cod_module.densify(summary, missing, target_words)
            summary = _enforce_word_count(summary, target_words)

        return summary

    def style_transfer(self, text: str,
                       source_style: str = "casual",
                       target_style: str = "formal") -> str:
        """Single prompt-based Text Style Transfer pass over the assembled draft.

        Preserves all \\cite{}, \\ref{}, and numeric tokens verbatim. A post-transfer
        diff check rejects the output if any protected token was altered; on rejection
        it retries once with a stricter instruction, then raises ValueError.
        """
        protected_tokens = _extract_protected_tokens(text)

        for _ in range(2):
            result = self._tst_module(
                text=text,
                source_style=source_style,
                target_style=target_style,
                exemplars=_load_formal_exemplars(),
            )
            out = result.transferred_text
            if _protected_tokens_intact(out, protected_tokens):
                return out

        raise ValueError(
            "style_transfer: \\cite{}/\\ref{}/numeric tokens were altered after 2 attempts. "
            "Manual review required."
        )
