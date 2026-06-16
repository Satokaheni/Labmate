# Writing & Critique Skills Spec

**Project**: Labmate — local autonomous writing + coding agent running on Gemma 4 MoE (RTX A6000 48 GB)
**Status**: Draft v1.0
**Date**: 2026-06-15

---

## 1. Overview

Labmate needs two complementary skill families:

1. **Academic Writing Skills** — produce IMRaD-structured academic papers through a hierarchical
   pipeline of composable, independently-testable stages: outline → section → paragraph → sentence.
   Each stage is a bounded LLM call; the full paper is never generated in one shot.

2. **Critique & Reflexion Skills** — review both code and writing output through a
   grounded self-reflection loop where an external signal (test runner, linter, retrieval,
   or a structured rubric) is the primary evaluator. Pure intrinsic self-critique is
   explicitly prohibited as the primary feedback source.

The two skill families are designed to compose: the writing pipeline generates a draft, and the
critique loop reviews and iteratively revises it before the draft is accepted. The same critique
machinery applies to code diffs and prose equally.

**Critical constraints for this project**:

- Citation hallucination rates of 11-57% across deployed LLMs mean the validation cascade
  is non-optional and must run before any citation is accepted into the bibliography.
- Degeneration-of-Thought (DoT) means the reflexion loop must be fed a fresh external signal
  every round; pure self-critique across iterations produces convergence on wrong answers.
- Context budget on a single A6000 is finite; per-section drafting keeps each call well under
  the coherence-degradation threshold of ~3,000 generated tokens.

---

## 2. Architecture

### 2.1 Academic Writing Pipeline (outline → section → paragraph)

The pipeline follows the STORM two-stage pattern (Stanford OVAL, NAACL 2024) and the AI Scientist
per-section LaTeX writing pattern (SakanaAI, Nature 2024).

**Stage 1 — Pre-writing (Research & Outline)**

- Gather references via RAG over local PDF library + live Semantic Scholar query.
- Cluster references by IMRaD section relevance.
- Generate a fixed IMRaD scaffold via `outline_skill()` with references bucketed per section.
- Validate all gathered BibTeX entries through the citation validation cascade before any
  reference is locked into the outline. Hallucinated citations caught here cost one cascade
  call; hallucinated citations caught at submission cost retraction.

**Stage 2 — Writing (Section-by-Section)**

- Invoke `draft_section()` for each section in canonical IMRaD order:
  Introduction → Background/Related Work → Methods → Experimental Setup → Results → Discussion/Conclusion.
- Each call receives only that section's reference subset and notes; never the full library.
- Prompt explicitly forbids citing any reference not in the supplied validated set.
- After all sections are assembled, run `style_transfer()` as a unification pass.
- Run `chain_of_density()` over the assembled abstract.

**Stage 3 — Critique-Revise**

- Hand the assembled draft to `CritiqueSkill` for a writing critique.
- Up to 3 rounds of reflexion; each round grounds feedback in retrieval + rubric checks.
- Final draft accepted only after critique verdict reaches `pass` or the iteration cap is hit.

### 2.2 Critique-Revise Loop (single-agent two-role)

The loop implements the Reflexion architecture (Shinn et al., NeurIPS 2023) with mandatory
external grounding (CRITIC, Gou et al., ICLR 2024):

```
Actor (Generator)
    |
    v
Evaluator (external oracle first; LLM-as-judge fallback only)
    |
    v
Self-Reflection module --> episodic memory (bounded sliding window)
    |
    v
Refiner (Actor + memory context)
    |
    v [repeat max 3 rounds or until verdict == 'pass']
```

Key rules:
- The Generator and Critic roles NEVER collapse into one prompt.
- The Evaluator sources external signals before forming its verdict.
- A wrong/biased critique never overwrites a candidate that scored higher than the revision.
- On `severity == 'critical'` or any `category == 'security'` issue, the loop escalates to
  multi-agent debate rather than single-agent revision.
- The loop halts when `verdict == 'pass'`, `score >= stop_threshold`, or `max_iters` is reached.

### 2.3 Integration: Writing Generates, Critique Reviews

```
AcademicWritingSkill                CritiqueSkill
        |                                 |
  outline_skill()                   evaluate(draft)
        |                                 |
  [per-section] draft_section()    ground_with_signals()
        |                                 |
  validate_citations()             reflexion_loop()
        |                                 |
  chain_of_density()               escalation_router()
        |                                 |
  style_transfer()                  verdict / revision
        |_________________________________|
                      |
               Final Accepted Draft
```

`AcademicWritingSkill.run_pipeline()` calls `CritiqueSkill.critique(draft, critique_type='writing')`
after assembling the draft. The critique produces a `Critique` with `suggested_revision`; the
writing skill applies the revision and re-runs only the affected sections before the next round.

### 2.4 ASCII Diagram

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │                         Labmate Agent                            │
 │                                                                     │
 │  ┌──────────────────────────────────────────────────────────────┐  │
 │  │                  AcademicWritingSkill                        │  │
 │  │                                                              │  │
 │  │  Topic + Local PDFs + Semantic Scholar                       │  │
 │  │       │                                                      │  │
 │  │       ▼                                                      │  │
 │  │  [outline_skill]  ──► IMRaD Scaffold (sections + ref buckets)│  │
 │  │       │                                                      │  │
 │  │       ▼   per section (in canonical order)                   │  │
 │  │  [validate_citations] ──► DOI/Crossref/Semantic Scholar      │  │
 │  │       │                    cascade  ──► hallucination flag   │  │
 │  │       │                                                      │  │
 │  │       ▼                                                      │  │
 │  │  [draft_section × N] ──► section texts with \cite{key}      │  │
 │  │       │                                                      │  │
 │  │       ▼                                                      │  │
 │  │  [style_transfer] ──► tone-unified full draft               │  │
 │  │       │                                                      │  │
 │  │       ▼                                                      │  │
 │  │  [chain_of_density] ──► compressed abstract                 │  │
 │  └──────────────────────────────┬───────────────────────────────┘  │
 │                                 │ draft                             │
 │                                 ▼                                   │
 │  ┌──────────────────────────────────────────────────────────────┐  │
 │  │                       CritiqueSkill                          │  │
 │  │                                                              │  │
 │  │  ┌──────────┐     ┌──────────────────┐    ┌──────────────┐  │  │
 │  │  │  Actor   │────►│    Evaluator     │───►│  Reflector   │  │  │
 │  │  │(Refiner) │     │  external first  │    │  (memory)    │  │  │
 │  │  └────▲─────┘     │ tests/lint/RAG   │    └──────┬───────┘  │  │
 │  │       │           │ LLM-judge fallback│           │          │  │
 │  │       │           └────────┬─────────┘           │          │  │
 │  │       │ [max 3 rounds]     │ severity==critical   │          │  │
 │  │       │                    ▼                      │          │  │
 │  │       │           ┌─────────────────┐             │          │  │
 │  │       │           │ Escalation      │             │          │  │
 │  │       │           │ Debate (2 + judge)            │          │  │
 │  │       │           └─────────────────┘             │          │  │
 │  │       └───────────────────────────────────────────┘          │  │
 │  │                                                              │  │
 │  │                verdict: pass | revise | fail                 │  │
 │  └──────────────────────────────────────────────────────────────┘  │
 └─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Academic Writing Skills

### 3.1 IMRaD Scaffold (Introduction, Methods, Results, Discussion)

IMRaD is the mandatory structure for academic papers produced by Labmate.

**Canonical section order** (enforced by the outline skill and validated post-generation):

1. Abstract (produced last via Chain-of-Density, placed first)
2. Introduction
3. Background / Related Work
4. Methods
5. Experimental Setup
6. Results
7. Discussion
8. Conclusion
9. References

**Enforcement rules**:

- `outline_skill()` always emits sections in the canonical order above. It must not allow the
  user to reorder Methods after Results or fold Discussion into Results.
- After all sections are drafted, a structural validator checks the rendered document against
  the schema. Any section missing or out of order causes the pipeline to re-invoke the outline
  skill before proceeding.
- Each section node in the outline carries `{name, ref_ids, key_points, word_budget}` so
  section drafting is fully specified without needing to read the other sections.

**IMRaD role constraints** embedded in each section's draft prompt:

| Section | Permitted content | Prohibited content |
|---|---|---|
| Introduction | Motivation, problem statement, contributions | Results, conclusions |
| Background | Related work, prior techniques | Novel claims |
| Methods | Approach, algorithm, design | Results, evaluation numbers |
| Results | Figures, tables, numbers from supplied notes only | Interpretation |
| Discussion | Interpretation, limitations, future work | New unreported numbers |
| Conclusion | Summary of contributions | New claims not in Results |

### 3.2 Two-Stage Pipeline (STORM/AI Scientist Pattern)

The pipeline is grounded in two validated open-source systems:

**STORM (Stanford OVAL, NAACL 2024, `stanford-oval/storm`)**:
- Stage 1 (pre-writing): perspective-guided question asking + simulated expert conversations
  to build a knowledge base and generate a structured outline.
- Stage 2 (writing): section-by-section generation grounded in the knowledge base with citations.
- Built on DSPy: each stage is a declarative, optimizable module, not a hardcoded prompt.

**AI Scientist (SakanaAI, Nature 2024, `SakanaAI/AI-Scientist`)**:
- Per-section LaTeX writing via the Aider file-editing agent.
- 20 rounds of Semantic Scholar citation retrieval per section.
- Explicit instruction per section: "use only real results and real citations."
- Automated LLM peer-review loop after full draft assembly.

**Labmate adaptation**:
- Uses DSPy modules for each stage (same as STORM) to allow prompt optimization without
  touching skill code.
- Follows AI Scientist's per-section citation-retrieval pattern (RAG per section, not one
  global retrieval for the whole paper).
- Replaces Aider with Labmate's own file-editing skill to stay within the local stack.
- The automated reviewer is `CritiqueSkill` with `critique_type='writing'`.

### 3.3 Chain-of-Density Summarization

Chain-of-Density (Adams et al., EMNLP 2023, arXiv:2309.04269) produces the abstract.

**Algorithm**:

```
iteration 0:  Generate a sparse summary at target_words. Few named entities; high readability.
iteration 1:  Identify 1-3 salient entities MISSING from the previous summary.
              Rewrite the summary to ADD them while HOLDING length == target_words.
iteration 2:  Repeat: find missing salient entities, add, hold length.
iteration 3:  Final iteration. Output is the abstract.
```

**Critical rules**:

- Start SPARSE. Do not start dense. "Start dense" is the most common misapplication and
  produces unreadable entity-jammed output.
- Length is FIXED each iteration. Only entity density rises. After each pass, count words
  and truncate-then-retry if the count exceeds `target_words`.
- The word count must not increase across iterations.
- `target_words` for a conference abstract is typically 150-250 words.

**Why not one-shot summarization**: one-shot compression at high density consistently loses
narrative flow and omits context that lower-density summaries preserve. CoD produces summaries
human evaluators rate higher on both informativeness and readability (Adams et al. ablation).

### 3.4 Citation Validation Cascade (Deterministic-First)

**This is non-optional. Citation hallucination rates of 11-57% across deployed LLMs have
been measured empirically. Every LLM-generated citation must pass this cascade before it
enters the bibliography.**

The cascade is deterministic-first, LLM-last. LLM participation is a fallback only for
entries that passed all deterministic checks but had ambiguous metadata.

```
Input: raw BibTeX entry string (LLM-generated)
         │
         ▼
Step 1: regex / bibtexparser parse
  → extract doi, arxiv_id, url, eprint fields
         │
         ├─── identifier found ──────────────────────────────────────┐
         │                                                           │
         ▼                                                           ▼
Step 2a: DOI → Crossref (habanero)        Step 2b: arXiv ID → arXiv API
  or PubMed ID → PubMed API                   title match + author overlap
  Compare: title (fuzzy ≥ 0.85),                    ≥ 0.60 threshold
           author overlap ≥ 0.60
         │                                           │
    match ──► VALID (source: crossref/arxiv)    mismatch ──► FLAGGED
    mismatch ─► FLAGGED (identifier conflict)
         │
         │ (no identifier found)
         ▼
Step 3: Semantic Scholar title search (semanticscholar Python client)
  Compare: author overlap ≥ 0.60
         │
    match ──► VALID (source: semantic_scholar)
    no match ─► FLAGGED (likely hallucinated)
         │
         │ (optional Step 4, only if all deterministic steps fail)
         ▼
Step 4: LLM deep-web-search fallback (refchecker pattern)
  Only invoked on entries with plausible structure but no identifier;
  result is advisory, not authoritative; output is always FLAGGED_REVIEW
```

**Outcomes**:

- `VALID`: entry enters `references.bib` with normalized BibTeX from the API response.
- `FLAGGED`: entry is not included. It is written to `citations_flagged.json` for human review.
  The draft text replaces the citation with `[CITATION NEEDED: <title>]`.
- No LLM-generated citation text is ever trusted verbatim; the normalized BibTeX from the
  API response replaces the LLM output entirely.

**BibTeX key deduplication**: keys follow `firstauthorYEAR` convention. When two entries
produce the same key, append `a`, `b`, `c` disambiguators (`smith2024a`, `smith2024b`).

**Author-overlap threshold**: the refchecker project uses a 60% author-overlap threshold.
Labmate adopts the same threshold as a minimum; raising it to 0.80 reduces false-positives
on multi-author papers.

### 3.5 Text Style Transfer (casual → formal academic, prompt-based)

Style transfer unifies register across sections that were drafted independently and may have
drifted in tone. It runs as a single pass over the assembled draft after all sections are written,
not section-by-section during drafting.

**Approach**: prompt-based Text Style Transfer (TST) using in-context formal exemplars. No
fine-tuning is required; zero-shot and few-shot LLM prompting now matches or exceeds trained TST
models on formality transfer benchmarks (Ostheimer et al. 2023; survey by 2024 TST Survey).

**Prompt construction**:

```
System: You are an academic writing editor. Convert the following text from casual
        register to formal academic prose. Preserve all factual content, citations,
        and technical terms verbatim. Do not add claims or remove existing ones.

Exemplars: [3-5 sentence pairs: casual → formal, drawn from published papers]

Input: <assembled draft section>
```

**Dimensions applied in order**:
1. Formality (casual → formal)
2. Politeness / hedging (assertions → appropriately hedged academic claims)
3. Simplification removal (colloquialisms, contractions, first-person informality)

**Constraint**: style transfer must not alter citations, figures references, or numerical
values. A post-transfer diff check compares all `\cite{}`, `\ref{}`, and numeric tokens
between input and output; any mismatch fails the transfer and triggers a retry with a
stricter preservation instruction.

---

## 4. AcademicWritingSkill Implementation

### 4.1 Class Outline

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

import bibtexparser
import dspy
from habanero import Crossref
from pydantic import BaseModel
from semanticscholar import SemanticScholar


POLITE_EMAIL = "your-email@domain.com"  # Crossref polite pool
AUTHOR_OVERLAP_THRESHOLD = 0.60
TITLE_MATCH_THRESHOLD = 0.85
MAX_COD_ITERATIONS = 3


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


class CitationResult(BaseModel):
    entry_id: str
    valid: bool
    source: str | None = None            # 'crossref' | 'arxiv' | 'semantic_scholar' | 'llm_fallback'
    flagged_for_review: bool = False
    normalized_bibtex: str | None = None
    conflict_reason: str | None = None


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


class AcademicWritingSkill:
    """
    Composable skill set for producing IMRaD-structured academic papers.
    Each method is independently testable and invokable.

    Usage:
        skill = AcademicWritingSkill(lm=dspy.LM("ollama/gemma4"))
        outline = skill.outline_skill(topic, refs)
        validated = skill.validate_citations([r.bibtex for r in refs])
        good_refs = [r for r, v in zip(refs, validated) if v.valid]
        sections = {s.name: skill.draft_section(s.name, good_refs, notes) for s in outline.sections}
        draft = "\n\n".join(sections[n] for n in IMRAD_ORDER if n in sections)
        draft = skill.style_transfer(draft)
        abstract = skill.chain_of_density(raw_abstract, target_words=200)
    """

    def __init__(self, lm: dspy.LM, polite_email: str = POLITE_EMAIL):
        self._lm = lm
        self._cr = Crossref(mailto=polite_email)
        self._ss = SemanticScholar()
        dspy.configure(lm=lm)
        self._outline_module = OutlineModule()
        self._section_module = SectionDraftModule()
        self._cod_module = ChainOfDensityModule()
        self._tst_module = StyleTransferModule()
```

### 4.2 `outline_skill()`

```python
    def outline_skill(self, topic: str, refs: list[Ref]) -> Outline:
        """
        Two-stage STORM pattern:
        1. Cluster refs by IMRaD section relevance.
        2. Generate a fixed IMRaD scaffold with refs bucketed per section.

        Returns Outline with sections in canonical IMRAD_ORDER.
        Raises ValueError if any mandatory section is missing from the output.
        """
        ref_dicts = [{"id": r.id, "title": r.title, "abstract": r.abstract} for r in refs]
        result = self._outline_module(topic=topic, references=ref_dicts)
        outline = Outline(sections=_parse_outline_json(result.outline))

        # Structural validation: enforce canonical ordering
        produced = [s.name for s in outline.sections]
        required = [s for s in IMRAD_ORDER if s not in ("Abstract", "References")]
        missing = [s for s in required if s not in produced]
        if missing:
            raise ValueError(f"Outline missing required IMRaD sections: {missing}")

        # Sort sections into canonical order
        order_map = {name: i for i, name in enumerate(IMRAD_ORDER)}
        outline.sections.sort(key=lambda s: order_map.get(s.name, 99))
        return outline
```

```python
# DSPy module backing outline_skill
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

    def forward(self, topic: str, references: list[dict]) -> dspy.Prediction:
        return self.gen(topic=topic, references=str(references))
```

### 4.3 `draft_section(section_name, refs, notes)`

```python
    def draft_section(self, section_name: str, refs: list[Ref], notes: str) -> str:
        """
        Per-section draft call. NEVER drafts the whole paper in one call.
        Prompt enforces:
          - Use only supplied notes and figures (no model memory for results).
          - Cite only validated refs from the supplied list.
          - Stay within the section's IMRaD role (no Results content in Methods, etc.).
          - Emit inline citations as \\cite{key}.

        Returns markdown/LaTeX string with inline \\cite{key} references.
        Raises ValueError if the output cites a key not in the supplied refs.
        """
        valid_keys = {_bibtex_key(r.bibtex) for r in refs}
        ref_context = [{"key": _bibtex_key(r.bibtex), "title": r.title,
                        "abstract": r.abstract} for r in refs]

        result = self._section_module(
            section_name=section_name,
            references=ref_context,
            notes=notes,
            imrad_role=_imrad_role_description(section_name),
        )
        text = result.section_text

        # Guard: reject any citation key not in the validated set
        cited_keys = set(re.findall(r"\\cite\{([^}]+)\}", text))
        unknown = cited_keys - valid_keys
        if unknown:
            raise ValueError(
                f"Section '{section_name}' cited unvalidated keys: {unknown}. "
                "Re-invoking with explicit key allowlist."
            )
        return text
```

### 4.4 `chain_of_density(text, target_words, iterations=3)`

```python
    def chain_of_density(self, text: str, target_words: int,
                         iterations: int = MAX_COD_ITERATIONS) -> str:
        """
        Iterative Chain-of-Density summarization (Adams et al. 2023).
        Starts sparse; adds 1-3 missing salient entities per iteration
        while holding word count == target_words.

        IMPORTANT: starts sparse, not dense. Starting dense is the
        most common misapplication and produces unreadable output.

        Args:
            text: source text to summarize (e.g. full paper body for abstract).
            target_words: fixed word count for every iteration.
            iterations: number of densification passes (default 3).

        Returns the final densified summary at target_words ± 5%.
        """
        summary = self._cod_module.initial_sparse(text, target_words)
        summary = _enforce_word_count(summary, target_words)

        for i in range(iterations):
            missing_entities = self._cod_module.identify_missing(text, summary)
            if not missing_entities:
                break  # converged; no more salient entities to add
            summary = self._cod_module.densify(summary, missing_entities, target_words)
            summary = _enforce_word_count(summary, target_words)  # truncate-and-retry on overrun

        return summary
```

### 4.5 `validate_citations(bibtex_entries)`

```python
    def validate_citations(self, bibtex_entries: list[str]) -> list[CitationResult]:
        """
        Deterministic-first citation validation cascade.

        Citation hallucination rates of 11-57% across deployed LLMs make this
        cascade non-optional. Every entry must pass before entering references.bib.

        Cascade order:
          1. regex parse → extract DOI / arXiv ID
          2a. DOI → Crossref (habanero) title + author-overlap check
          2b. arXiv ID → arXiv API title + author-overlap check
          3. No identifier → Semantic Scholar title search + author-overlap check
          4. LLM fallback (advisory only; result is always FLAGGED_REVIEW)

        Returns one CitationResult per entry. Callers must filter to valid=True
        before including any entry in the bibliography.
        """
        return [self._validate_one(entry) for entry in bibtex_entries]

    def _validate_one(self, entry: str) -> CitationResult:
        try:
            rec = bibtexparser.loads(entry).entries[0]
        except Exception:
            return CitationResult(entry_id="?", valid=False, flagged_for_review=True,
                                  conflict_reason="bibtexparser failed to parse entry")

        entry_id = rec.get("ID", "unknown")
        ident, ident_type = _extract_identifier(rec)

        if ident:
            if ident_type == "doi":
                try:
                    meta = self._cr.works(ids=ident)
                    if _title_match(meta, rec) and _author_overlap(meta, rec) >= AUTHOR_OVERLAP_THRESHOLD:
                        return CitationResult(
                            entry_id=entry_id, valid=True, source="crossref",
                            normalized_bibtex=_crossref_to_bibtex(meta, entry_id)
                        )
                    return CitationResult(entry_id=entry_id, valid=False, flagged_for_review=True,
                                          conflict_reason="DOI resolves but title/author mismatch")
                except Exception:
                    pass  # fall through to Semantic Scholar

            elif ident_type == "arxiv":
                meta = _arxiv_lookup(ident)
                if meta and _title_match(meta, rec) and _author_overlap(meta, rec) >= AUTHOR_OVERLAP_THRESHOLD:
                    return CitationResult(
                        entry_id=entry_id, valid=True, source="arxiv",
                        normalized_bibtex=_arxiv_to_bibtex(meta, entry_id)
                    )
                return CitationResult(entry_id=entry_id, valid=False, flagged_for_review=True,
                                      conflict_reason="arXiv ID resolves but title/author mismatch")

        # Step 3: no identifier — title search
        title = rec.get("title", "")
        if title:
            hits = self._ss.search_paper(title, limit=3)
            for hit in hits:
                if _author_overlap_ss(hit, rec) >= AUTHOR_OVERLAP_THRESHOLD:
                    return CitationResult(
                        entry_id=entry_id, valid=True, source="semantic_scholar",
                        normalized_bibtex=_ss_to_bibtex(hit, entry_id)
                    )

        # Step 4: LLM fallback — advisory only
        return CitationResult(
            entry_id=entry_id, valid=False, flagged_for_review=True,
            conflict_reason="No identifier found; title search returned no author-overlap match. "
                            "Likely hallucinated."
        )
```

### 4.6 `style_transfer(text)`

```python
    def style_transfer(self, text: str,
                       source_style: str = "casual",
                       target_style: str = "formal") -> str:
        """
        Prompt-based Text Style Transfer unification pass.
        Run ONCE over the fully assembled draft (not per-section).

        Preserves all \\cite{}, \\ref{}, and numeric tokens verbatim.
        Post-transfer diff check rejects the output if any citation,
        figure reference, or number was altered.

        Returns the style-transferred text, or raises ValueError if
        the integrity check fails after 2 retries.
        """
        protected_tokens = _extract_protected_tokens(text)

        for attempt in range(2):
            result = self._tst_module(
                text=text,
                source_style=source_style,
                target_style=target_style,
                exemplars=_load_formal_exemplars(),
            )
            out = result.transferred_text
            if _protected_tokens_intact(out, protected_tokens):
                return out
            # On first failure, add explicit preservation instruction and retry
            text = text  # unchanged input for retry

        raise ValueError(
            "style_transfer: citation/reference/numeric tokens were altered after 2 attempts. "
            "Manual review required."
        )
```

---

## 5. Critique & Reflexion Skills

### 5.1 Single-Agent Two-Role Design

Labmate runs on a single local Gemma model. Multi-agent debate with two separate model
instances is available as an escalation path (severity=critical or security issues) but is
not the default, because it doubles inference cost on the A6000.

The default design is **single-agent two-role**:

- **Generator role**: produces the initial output and each revised candidate. Uses the
  standard system prompt with creative/constructive framing.
- **Critic role**: evaluates the output. Uses a SEPARATE, ADVERSARIAL system prompt.
  Lower temperature than the generator. The critic is never told it is judging its own
  output (sycophancy mitigation).

**Sycophancy mitigation for single-model design** (documented in SycEval and
"Challenging the Evaluator", arXiv:2509.16533):

1. Distinct adversarial critic system prompt — frames the critic as a rigorous external
   reviewer whose job is to find flaws, not to affirm.
2. Lower critic temperature (0.1-0.2 vs. 0.7 for the generator).
3. Ground every critique in objective signals (test results, linter output, retrieval).
4. Non-empty critique contract: Pydantic validation enforces that `issues_found` is non-empty
   OR `no_issues_justification` is provided. Unjustified empty critiques are retried via
   Instructor re-ask.
5. Never show the critic the generation chain-of-thought; only show the final output to
   evaluate.

### 5.2 Structured Critique Schema

```python
class Issue(BaseModel):
    location: str       # file:line for code; paragraph id for writing; step id for reasoning
    category: str       # 'bug' | 'style' | 'factual' | 'security' | 'logic' | 'clarity'
    explanation: str
    grounded_by: str | None = None  # e.g. "pytest:test_auth failed", "mypy: incompatible types"

class Critique(BaseModel):
    verdict: Literal["pass", "revise", "fail"]
    severity: Literal["low", "medium", "high", "critical"]  # drives escalation_router
    score: float                        # 0.0 to 1.0
    issues_found: list[Issue]           # MUST be non-empty OR no_issues_justification provided
    constitutional_violations: list[str] # principle ids violated (empty list is valid)
    suggested_revision: str             # actionable revision text or patch
    evidence: list[str]                 # external-feedback citations quoted verbatim
    confidence: float                   # 0.0 to 1.0; gates reflection poisoning guard
    no_issues_justification: str | None = None  # required if issues_found is empty
```

**Contract enforcement**: Pydantic validates every `Critique` instance. If `issues_found` is
empty and `no_issues_justification` is None, Instructor triggers an automatic re-ask. Unjustified
empty critiques after 2 re-asks are treated as a failed critique generation, and the loop
continues without consuming a reflexion round.

### 5.3 Grounded Feedback (tests + linter + execution, NOT pure self-judgment)

**The Degeneration-of-Thought (DoT) failure makes external grounding mandatory.**

DoT (Liang et al. arXiv:2305.19118, EMNLP 2024) describes what happens when a model self-critiques
across multiple rounds without fresh external signal: after 3-4 iterations, the model becomes
confident in its first answer and stops generating novel thoughts even when that answer is wrong.
The model enters a loop of superficial paraphrasing that looks like reflection but changes nothing
of substance.

The consequence for Labmate's architecture:

> Every reflexion round MUST feed a fresh external signal into the evaluator prompt.
> A critique built purely from the model's own intrinsic judgment is insufficient as the
> sole feedback source and must not drive revision decisions alone.
> (Huang et al., ICLR 2024; Gou et al. CRITIC, ICLR 2024)

**Evaluator signal priority order**:

1. **Deterministic oracles** (highest priority, always preferred):
   - Code: `pytest` / `unittest` output, `mypy` type-check, `ruff` lint, JSON schema validator.
   - Writing: citation validation results, word-count checker, IMRaD structure validator,
     retrieval grounding (does the claimed fact appear in a retrieved passage?).
2. **Decoupled LLM verification (CoVe Factored)**:
   - Plan verification questions about the output.
   - Answer each verification question in isolation WITHOUT the original output in context.
   - If any verification answer contradicts the output, the contradiction is added to the
     critique evidence.
3. **LLM-as-judge** (lowest priority, fallback only):
   - Used only where no programmatic oracle exists.
   - Requires: structured rubric, separate model temperature, calibration against labeled examples.
   - Must never be the sole source for a `severity=critical` verdict.

**For code critique specifically**: the test suite is ALWAYS run first. A static code critique
that was never executed is prohibited. The `ground_with_signals()` method is called before the
LLM evaluator prompt is assembled; test/lint results are injected into the evaluator context
as quoted evidence.

### 5.4 Bounded Iteration with Episodic Memory (max 3 rounds)

```
max_iters = 3   # hard cap; never exceed regardless of score
stop_threshold = 0.90   # score threshold for early exit
min_confidence = 0.50   # minimum critique confidence to trigger a reflexion update
MEMORY_WINDOW = 3       # maximum number of reflection objects kept in context
```

**Best-so-far retention**: the loop tracks the output with the highest evaluator score across
all iterations. If a revised output scores lower than the previous best, the revision is
discarded and the previous best is retained. This prevents reflection poisoning (a biased
critique steering the actor away from a correct answer it already had).

**Episodic memory as bounded sliding window**: `Reflection` objects are kept in a list of at
most `MEMORY_WINDOW` items. Oldest reflections are dropped when the window is full. This
prevents context overflow and dilution of the most recent lessons.

**Convergence check**: after each iteration, compute the token-level diff between the new
output and the previous. If the diff falls below a materiality threshold (e.g. <5 tokens
changed), declare convergence and exit even if the iteration cap has not been reached. This
detects the DoT pattern computationally: if the model is just paraphrasing without substance,
the loop terminates rather than consuming remaining iteration budget.

### 5.5 Escalation to Multi-Agent Debate (severity=critical only)

Multi-agent debate (Du et al., ICML 2024) is the escalation path for high-stakes findings.
It approximates the external decoupled perspective that Huang et al. show is necessary for
reliable self-correction.

**Trigger conditions** (`escalation_router` returns True):
- `crit.severity == "critical"`, OR
- Any `Issue` in `crit.issues_found` has `category == "security"`.

**Debate protocol**:
- 2 independent debater instances (or 2 sequential LLM calls with isolated context).
- Debater A produces a position. Debater B sees Debater A's position and must explicitly
  rebut it before stating its own. Debater A sees the rebuttal and may revise.
- A Judge agent (third call) sees both positions plus the rebuttal exchange and adjudicates.
- The Judge's verdict is the final `Critique`. The Judge may also disagree with both debaters.
- A Dissent agent may be added to avoid confident-wrong convergence: if both debaters agree
  but the confidence is high, the Dissent agent is given the task of finding flaws in the
  consensus.

**Cost constraint**: debate triples inference cost on the A6000. It must be gated strictly to
`severity=critical` or `category=security` issues. Routine quality issues (low/medium severity)
never escalate.

---

## 6. CritiqueSkill Implementation

### 6.1 Pydantic Issue + CritiqueResult Schema

```python
from __future__ import annotations

from typing import Literal, Protocol
import subprocess

import instructor
from pydantic import BaseModel, model_validator


class Issue(BaseModel):
    location: str
    category: Literal["bug", "style", "factual", "security", "logic", "clarity"]
    explanation: str
    grounded_by: str | None = None


class Critique(BaseModel):
    verdict: Literal["pass", "revise", "fail"]
    severity: Literal["low", "medium", "high", "critical"]
    score: float                              # 0.0 to 1.0
    issues_found: list[Issue]
    constitutional_violations: list[str]
    suggested_revision: str
    evidence: list[str]                       # external signal quotes
    confidence: float
    no_issues_justification: str | None = None

    @model_validator(mode="after")
    def require_justification_for_empty_issues(self) -> "Critique":
        if not self.issues_found and not self.no_issues_justification:
            raise ValueError(
                "issues_found is empty but no_issues_justification was not provided. "
                "A critique must explain why no issues were found."
            )
        return self


class Reflection(BaseModel):
    lessons: list[str]   # bounded; concrete changes to make on next attempt


class ExternalSignals(BaseModel):
    test_output: str | None = None
    lint_output: str | None = None
    execution_result: str | None = None
    retrieval_snippets: list[str] = []
    cove_verification: list[dict] | None = None   # [{question, answer, contradicts_draft}]
```

### 6.2 `critique()` Method

```python
class CritiqueSkill:
    """
    Grounded critique-reflexion loop for code and writing output.

    Design rules:
    - External signals are gathered BEFORE the LLM evaluator is invoked.
    - DoT is mitigated by feeding fresh signals every round and enforcing a
      convergence check on token-level diff.
    - Best-so-far is always retained; a lower-scoring revision is discarded.
    - Escalation to multi-agent debate is gated to severity=critical/security only.
    """

    MAX_ITERS: int = 3
    STOP_THRESHOLD: float = 0.90
    MIN_CONFIDENCE: float = 0.50
    MEMORY_WINDOW: int = 3

    def __init__(self, lm_client, constitution: list[str] | None = None):
        self._lm = lm_client           # instructor-wrapped LLM client
        self._constitution = constitution or _default_constitution()

    def critique(
        self,
        output: str,
        task: str,
        critique_type: Literal["code", "writing"] = "code",
        test_suite_path: str | None = None,
        lint_target: str | None = None,
    ) -> tuple[Critique, str]:
        """
        Run the grounded reflexion loop.

        Returns (final_critique, best_output).
        Raises EscalationRequired if severity=critical and debate is needed.
        """
        best_output = output
        best_score = 0.0
        memory: list[Reflection] = []
        trace: list[dict] = []

        for i in range(self.MAX_ITERS):
            signals = self.ground_with_signals(
                output=best_output,
                critique_type=critique_type,
                test_suite_path=test_suite_path,
                lint_target=lint_target,
            )
            crit = self._invoke_evaluator(task, best_output, signals, memory)
            trace.append({"round": i, "output": best_output, "critique": crit.model_dump()})

            if crit.score > best_score:
                best_score = crit.score
                best_output_this_round = best_output
            else:
                best_output_this_round = best_output  # keep current best; do not regress

            if self.escalation_router(crit):
                return self.run_debate(task, best_output, crit)

            if crit.verdict == "pass" or crit.score >= self.STOP_THRESHOLD:
                return crit, best_output

            if crit.confidence >= self.MIN_CONFIDENCE:
                reflection = self._reflect(task, best_output, crit)
                memory = (memory + [reflection])[-self.MEMORY_WINDOW:]

            revised = self._refine(task, best_output, crit, memory)

            # Convergence check: if revision is nearly identical, DoT has set in — exit
            if _token_diff(revised, best_output) < 5:
                break

            best_output = revised

        return crit, best_output
```

### 6.3 `ground_with_signals()`

```python
    def ground_with_signals(
        self,
        output: str,
        critique_type: Literal["code", "writing"],
        test_suite_path: str | None = None,
        lint_target: str | None = None,
    ) -> ExternalSignals:
        """
        Gather all available external signals before the LLM evaluator is called.

        For code critique: ALWAYS runs the test suite and linter if paths are provided.
        Critiquing code that was never executed is prohibited — see Section 5.3.

        For writing critique: runs citation validation, word-count check, and IMRaD
        structure validation. Optionally runs retrieval grounding if a retrieval
        client is configured.

        Returns ExternalSignals. The LLM evaluator receives these as quoted evidence
        in its prompt; it may not contradict them.
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
            # Citation grounding: check that cited keys appear in validated bibliography
            signals.retrieval_snippets = _check_imrad_structure(output)
            # CoVe factored verification
            questions = self._plan_verification_questions(output)
            signals.cove_verification = [
                {"question": q, "answer": self._answer_in_isolation(q), "contradicts_draft": None}
                for q in questions
            ]
            for item in signals.cove_verification:
                item["contradicts_draft"] = _check_contradiction(item["answer"], output)

        return signals
```

### 6.4 `reflexion_loop()`

```python
    def _reflect(self, task: str, output: str, crit: Critique) -> Reflection:
        """
        Convert the evaluator's critique into concise verbal lessons for episodic memory.
        The reflection prompt sees the task, the output, and the critique — not the
        generator's chain-of-thought, to prevent rationalization.
        """
        prompt = (
            f"Task: {task}\n\n"
            f"Output produced:\n{output}\n\n"
            f"Critique:\n{crit.model_dump_json(indent=2)}\n\n"
            "In 2-5 bullet points, write concise lessons about what to change on the next attempt. "
            "Be concrete and actionable. Reference specific issues from the critique."
        )
        raw = self._lm.complete(prompt)
        lessons = [line.strip("- ").strip() for line in raw.strip().split("\n") if line.strip()]
        return Reflection(lessons=lessons[:5])  # cap at 5 lessons

    def _refine(self, task: str, output: str, crit: Critique,
                memory: list[Reflection]) -> str:
        """
        Produce a revised output conditioned on the prior output and episodic memory.
        The refiner sees all lessons from the sliding window.
        """
        lessons_text = "\n".join(
            f"- {lesson}" for r in memory for lesson in r.lessons
        )
        prompt = (
            f"Task: {task}\n\n"
            f"Previous output:\n{output}\n\n"
            f"Critique summary:\n{crit.suggested_revision}\n\n"
            f"Lessons from prior rounds:\n{lessons_text}\n\n"
            "Produce a revised output that addresses the critique and applies the lessons. "
            "Do not introduce new issues. Preserve all valid content from the previous output."
        )
        return self._lm.complete(prompt)
```

### 6.5 `escalation_router()`

```python
    def escalation_router(self, crit: Critique) -> bool:
        """
        Returns True iff the critique should escalate to multi-agent debate.
        Multi-agent debate is reserved for severity=critical or security-category issues.
        All other issues use single-agent revision to bound inference cost.
        """
        if crit.severity == "critical":
            return True
        if any(issue.category == "security" for issue in crit.issues_found):
            return True
        return False

    def run_debate(
        self, task: str, output: str, trigger_crit: Critique
    ) -> tuple[Critique, str]:
        """
        Two-debater + judge multi-agent debate for severity=critical findings.
        Each debater must explicitly rebut the other before the judge adjudicates.
        A Dissent agent may be invoked if both debaters converge at high confidence
        to guard against confident-wrong consensus (Du et al.).
        """
        position_a = self._debate_position(task, output, trigger_crit, role="debater_a")
        position_b = self._debate_position(task, output, trigger_crit, role="debater_b",
                                            opposing_position=position_a)
        rebuttal_a = self._rebuttal(position_a, position_b)

        # Dissent guard: if both positions agree, invoke a dedicated dissent agent
        if _positions_converge(position_a, position_b):
            dissent = self._dissent(task, output, position_a)
            judgment = self._judge(task, output, position_a, position_b, rebuttal_a,
                                   dissent=dissent)
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

## 7. BDD Test Scenarios

```gherkin
Feature: IMRaD outline generation
  Scenario: Outline skill produces a valid IMRaD scaffold
    Given a research topic and a set of gathered references
    When outline_skill() is invoked
    Then the outline contains Introduction, Methods, Results, and Discussion/Conclusion sections
    And the sections are emitted in canonical IMRaD order
    And each section node carries the reference ids assigned to it
    And a ValueError is raised if any mandatory section is absent

Feature: Citation validation rejects hallucinated references
  Scenario: A fabricated reference is caught before acceptance
    Given a BibTeX entry whose DOI does not resolve
    And whose title is not found in Crossref, Semantic Scholar, or arXiv
    When the deterministic validation cascade runs
    Then the entry is marked valid=False
    And flagged_for_review=True
    And it is written to citations_flagged.json rather than references.bib

  Scenario: A real reference passes validation
    Given a BibTeX entry with a DOI that resolves to a matching title and author set
    When the validation cascade runs
    Then the entry is marked valid=True
    And normalized_bibtex is populated from the Crossref API response
    And the entry is added to references.bib with a unique deduplicated citation key

  Scenario: Duplicate citation keys are disambiguated
    Given two validated BibTeX entries both generating the key "smith2024"
    When both entries are written to references.bib
    Then the keys are emitted as "smith2024a" and "smith2024b"

Feature: Chain-of-Density abstract compression
  Scenario: Long abstract is compressed at fixed length with higher entity density
    Given an abstract text longer than 300 words
    When chain_of_density() is called with target_words=150 and iterations=3
    Then the output is 150 words or fewer
    And the entity density of the output is greater than the entity density of the input
    And the word count does not increase across iterations
    And the first iteration output is notably sparse compared to the final output

Feature: Section-by-section drafting
  Scenario: Each section is drafted in order with grounded citations
    Given a validated IMRaD outline with per-section references
    When draft_section() is invoked for each section in canonical order
    Then each section contains only inline citations resolving to validated references
    And no section cites a key that failed validation
    And a ValueError is raised immediately if an unvalidated key appears in the output

  Scenario: Results section stays grounded in supplied notes
    Given a Results section with supplied figures and tables in notes
    And no fabricated numbers in the notes
    When draft_section("Results", refs, notes) is invoked
    Then the output contains only numbers present in the supplied notes
    And no numbers appear that are absent from the notes

Feature: External-feedback reflexion loop for code
  Scenario: Agent fixes a failing solution using test results as the oracle
    Given a coding task with a unit test suite
    And the actor produces an initial solution that fails 2 of 5 tests
    When reflexion_loop() runs with max_iterations=3
    Then the evaluator returns failing test names and error messages as external feedback
    And the self-reflection module writes a concise lesson to episodic memory
    And the refiner produces a new solution conditioned on prior output and reflection
    And the loop stops when all 5 tests pass or 3 iterations are reached
    And every intermediate state is emitted as schema-valid JSON

  Scenario: Loop refuses to overwrite a correct answer on weak self-critique
    Given the actor's initial answer is already correct per the test suite
    And the LLM critic proposes a revision with confidence below 0.50
    When the reflexion loop evaluates the critique
    Then the agent keeps the original best-so-far answer
    And records a suppression warning in the trace
    And the output score is not degraded

Feature: DoT convergence detection
  Scenario: Loop exits early when revisions stop being material
    Given the actor has produced 2 rounds of revisions
    And each revision changes fewer than 5 tokens from the previous
    When the convergence check runs
    Then the loop exits before consuming the remaining iteration budget
    And the best-so-far output is returned

Feature: Escalation from single-agent critique to multi-agent debate
  Scenario: A critical security finding triggers debate
    Given the single-agent Generator/Critic loop reviews a code diff
    When the critic emits a Critique with severity="critical" and category="security"
    Then escalation_router() returns True
    And run_debate() is invoked with 2 independent debaters and a judge
    And each debater must quote and rebut the other before the judge adjudicates
    And the final decision is taken from the judge's verdict

  Scenario: A routine low-severity finding bypasses debate
    Given the critic emits a Critique with severity="low"
    When escalation_router() is called
    Then it returns False
    And single-agent suggested_revision is applied directly

Feature: Constitutional violation detection in a writing draft
  Scenario: Unsupported claims populate constitutional_violations
    Given a writing draft containing factual claims with no cited evidence
    And a written constitution loaded with writing-quality principles
    When critique() is invoked with critique_type="writing"
    Then Critique.constitutional_violations is non-empty
    And each violation references a specific constitutional principle id
    And suggested_revision either adds citation support for or removes each unsupported claim

Feature: Sycophantic critic guard
  Scenario: Unjustified empty critique is rejected and retried
    Given the LLM critic returns issues_found=[] with no no_issues_justification
    When Pydantic validation runs on the Critique model
    Then a ValidationError is raised
    And Instructor triggers an automatic re-ask
    And after 2 failed re-asks the critique is treated as a failed generation
    And the reflexion round is not consumed
```

---

## 8. Common Pitfalls

### Hallucinated Citations (11-57% rate — non-optional cascade)

LLMs generate plausible-sounding BibTeX entries with fabricated DOIs, wrong author lists, or
titles that don't exist. Measured rates range from 11% to 57% depending on model and domain.
This is not an edge case; it is the normal behavior of LLMs when asked to produce citations
from memory.

**Mitigation**: the `validate_citations()` cascade is non-optional. It runs before any reference
enters the bibliography. Every citation emitted by the LLM is treated as untrusted until it
passes DOI/arXiv/Semantic Scholar confirmation with ≥0.60 author overlap. LLM-generated BibTeX
text is never kept verbatim; the normalized API response replaces it entirely. Citations that
fail validation are hard-blocked and written to `citations_flagged.json` for human review.

### Degeneration-of-Thought (DoT) — Primary Motivation for External Grounding

DoT (Liang et al. arXiv:2305.19118, EMNLP 2024) is the failure mode where, after 3-4 rounds of
self-critique without fresh external input, the model becomes overconfident in its first answer
and stops generating novel corrective thoughts. The critique loop appears to continue but produces
only superficial paraphrasing that changes nothing of substance.

**This is the primary architectural reason external grounding signals are mandatory** in Labmate's
`CritiqueSkill`. A pure intrinsic self-critique loop will reliably fail on exactly the cases where
critique is most needed: when the first answer is confidently wrong. (Huang et al. ICLR 2024 confirm
that intrinsic self-correction without external feedback often degrades rather than improves accuracy.)

**Mitigation**: fresh external signals (test results, lint output, retrieval snippets, CoVe
verification) are gathered before each evaluator call. The token-level diff convergence check
detects when the loop has entered a DoT plateau and exits rather than continuing to waste budget.

### Sycophantic Critic (Single-Model Two-Role)

When the same model acts as both generator and critic, there is a documented tendency for the
critic to rubber-stamp the generator's output (SycEval; "Challenging the Evaluator" arXiv:2509.16533).
This is especially pronounced when the critic is told it is judging its own output.

**Mitigation**: adversarial critic system prompt, lower critic temperature (0.1-0.2), external
signal grounding, non-empty critique contract via Pydantic + Instructor re-ask, and never
revealing to the critic that it is evaluating the same model's output.

### Static Critique Without Execution

Critiquing a code diff without running the test suite misses runtime bugs (wrong type at runtime,
off-by-one, exception on edge input) and invites hallucinated issues (the model asserts a bug
that does not exist). Static critique alone is insufficient for code review.

**Mitigation**: `ground_with_signals()` is called before the LLM evaluator for every code critique.
The test suite is always run first. The evaluator prompt receives the actual test output as
quoted evidence and must not contradict it.

### IMRaD Section-Ordering Drift

Without explicit enforcement, LLMs muddle Methods content into Results, or present Discussion
as part of Results. This is common in long multi-section generation where the model loses track
of section boundaries.

**Mitigation**: `outline_skill()` enforces canonical ordering; `draft_section()` prompt embeds
explicit IMRaD role constraints; a post-assembly structural validator checks the rendered
document against the IMRAD_ORDER schema before the draft proceeds to the critique phase.

### Single-Pass Long-Form Degradation

Generating an entire academic paper in one LLM call causes coherence and citation grounding to
collapse after roughly 3,000 generated tokens. The model begins to drift from the outline,
repeat prior content, and introduce citations from memory.

**Mitigation**: per-section drafting (`draft_section()`) keeps each call well under the
degradation threshold. The whole paper is never drafted in one shot.

### CoVe Factored Variant Failure

If verification questions are answered WITH the original output still in context, the model
parrots its original (possibly wrong) reasoning rather than independently checking the claim.

**Mitigation**: `ground_with_signals()` answers verification questions in isolation — the
original output is not in context when the verification question is answered (Chain-of-Verification
Factored variant, Dhuliawala et al. ACL 2024 Findings).

### Confident-Wrong Convergence in Debate

In multi-agent debate (Du et al.), agents can converge on an incorrect answer and assert high
confidence. Consensus is not ground truth.

**Mitigation**: the Dissent agent is invoked when both debaters agree at high confidence. The
judge receives the dissent position and must address it before adjudicating.

### Reflection Poisoning

A wrong or biased critique steers the actor away from a correct answer it already had. If the
critique score is low-confidence and the revised output scores lower than the previous best,
the revision is harmful.

**Mitigation**: best-so-far retention across all iterations. Reflections are only appended to
memory when `crit.confidence >= MIN_CONFIDENCE`. Revised outputs that score lower than the
best-so-far are discarded.

### Unbounded Reflection Memory / Context Overflow

Appending all reflection objects to the context overflows the model's context window and dilutes
the most recent, most relevant lessons with stale earlier ones.

**Mitigation**: `MEMORY_WINDOW = 3` keeps only the 3 most recent `Reflection` objects. Older
reflections are dropped when the window is full.

### Entity-Sparse Summaries from Misapplied Chain-of-Density

Prompting CoD to "start dense" produces unreadable, entity-jammed output from the first
iteration. The method requires starting sparse and iteratively densifying.

**Mitigation**: `chain_of_density()` explicitly begins with a sparse summary and the iteration
loop adds 1-3 missing entities per pass. The prompt for iteration 0 explicitly says "use few
named entities; prioritize readability."

### Abstract / Word-Count Overrun

Without a fixed-length constraint per iteration, the CoD summary balloons in word count across
iterations as the model adds entities without removing anything.

**Mitigation**: `_enforce_word_count()` is called after each CoD iteration. If the output
exceeds `target_words`, the model is prompted to truncate-and-retry with the same entity set.

### BibTeX Key Collision

Two papers by the same first author in the same year produce identical `firstauthorYEAR` keys,
causing bibliography corruption.

**Mitigation**: `validate_citations()` deduplicates all keys post-validation, appending `a`,
`b`, `c` disambiguators when collisions occur (`smith2024a`, `smith2024b`).

---

## 9. Dependencies

### Academic Writing Skills

| Package | Version | Purpose |
|---|---|---|
| `bibtexparser` | v1.x stable or v2.x | Parse and write `.bib` files; citation key validation and deduplication |
| `pybtex` | latest | BibTeX formatting, bibliography generation, DOI/eprint field support |
| `habanero` | v2.x | Crossref API client for DOI lookup and metadata validation; polite pool via mailto |
| `semanticscholar` | latest | Semantic Scholar API for title search and per-section RAG reference gathering |
| `scholarly` | latest | Google Scholar scrape; last-resort fallback (rate-limit risk; may need proxies) |
| `dspy-ai` | >=2.0 | Declarative LLM program framework for outline/section/edit modules (STORM architecture) |
| `pylatexenc` | v2.10 | LaTeX ↔ Unicode conversion; clean Crossref/BibTeX special characters |
| `requests` | latest | arXiv API calls, GROBID REST API client |
| GROBID | via Docker | ML PDF → structured TEI/XML; extracts metadata and reference lists from PDFs |
| `doi2bib` | CLI | DOI/arXiv/PubMed ID → BibTeX; fallback for identifier conversion |

### Critique & Reflexion Skills

| Package | Version | Purpose |
|---|---|---|
| `pydantic` | v2 | `Issue`, `Critique`, `Reflection` schema definitions and validation |
| `instructor` | latest | Wraps LLM client to coerce output into Pydantic models; automatic re-ask on validation failure |
| `outlines` or `llguidance` | latest | Constrained/grammar-guided decoding; guarantees JSON-schema-valid critique output from Gemma |
| `langgraph` | >=0.2 | `StateGraph` orchestration of cyclic actor→evaluator→reflect→refine loops; checkpointing |
| `langchain-core` | latest | LLM/tool abstraction, prompt templates, `with_structured_output()` |
| `dspy-ai` | >=2.0 | Shared with writing skills; `Assert`/`Suggest` for verify-correct pipelines |
| `autogen` / `ag2` | latest | Multi-agent debate (Du-style) for the escalation path |
| `deepeval` or `ragas` | latest | LLM-as-judge evaluation metrics, rubric scoring, calibration |
| `langsmith` or `langfuse` | latest | Trace and log every actor/evaluator/reflection step; detect DoT convergence |
| `pytest` | latest | Test suite runner for code critique oracle |
| `ruff` | latest | Linter for code critique oracle |
| `mypy` | latest | Type checker for code critique oracle |

---

## 10. Reference Papers & Repos

### Academic Writing

| Paper | arXiv | Venue | Relevance |
|---|---|---|---|
| Adams et al. — From Sparse to Dense: GPT-4 Summarization with Chain of Density Prompting | 2309.04269 | EMNLP 2023 Workshop | Foundational CoD method |
| Shao et al. — STORM: Wikipedia-like Articles from Scratch | 2402.14207 | NAACL 2024 | Two-stage outline-then-write architecture |
| Jiang et al. — Co-STORM: Collaborative Discourse Protocol | 2408.15232 | EMNLP 2024 | Collaborative variant with Moderator agent and mind-map |
| Lu et al. — The AI Scientist | 2408.06292 | Nature 2024 | Per-section LaTeX writing, Semantic Scholar RAG, automated reviewer |
| 2025 LiRA | 2510.05138 | 2025 | Outline-drafting agent + specialized writing/consistency/factual agents |
| Baek et al. — ResearchAgent | 2404.07738 | 2024 | Iterative draft→ReviewingAgents→revise |
| Kaesberg et al. — CiteAssist | 2407.03192 | 2024 | GROBID + PDF-LIB citation extraction pipeline |
| 2024 TST Survey | 2407.16737 | 2024 | Text Style Transfer survey, formality transfer |
| Ostheimer et al. — TST Evaluation with LLMs | 2308.13577 | 2023 | Zero-shot LLM TST evaluation; prompt ensembling |

### Critique & Reflexion

| Paper | arXiv | Venue | Relevance |
|---|---|---|---|
| Shinn et al. — Reflexion | 2303.11366 | NeurIPS 2023 | Actor/Evaluator/Self-Reflection with episodic memory |
| Bai et al. — Constitutional AI | 2212.08073 | 2022 | Critique-then-revise against explicit principle set |
| Dhuliawala et al. — Chain-of-Verification | 2309.11495 | ACL 2024 Findings | Factored decoupled verification; prevents error propagation |
| Du et al. — Multiagent Debate | 2305.14325 | ICML 2024 | Multi-agent debate for factuality and reasoning |
| Huang et al. — LLMs Cannot Self-Correct Yet | 2310.01798 | ICLR 2024 | Landmark: intrinsic self-correction fails; mandates external feedback |
| Gou et al. — CRITIC | 2305.11738 | ICLR 2024 | Tool-interactive critiquing; external tools as primary evaluator |
| Madaan et al. — Self-Refine | 2303.17651 | NeurIPS 2023 | Single-LLM generate→feedback→refine loop |
| Kamoi et al. — When Can LLMs Self-Correct? | 2406.01297 | TACL 2024 | Taxonomy of when self-correction works and fails |
| Liang et al. — Degeneration-of-Thought | 2305.19118 | EMNLP 2024 | Names and formalizes DoT; motivates debate + dissent agent |
| Zhuge et al. — Agent-as-a-Judge | 2410.10934 | 2024 | Agentic evaluator judges full reasoning trajectory |

### Reference Repositories

| Repo | URL | Relevance |
|---|---|---|
| `stanford-oval/storm` | https://github.com/stanford-oval/storm | STORM + Co-STORM; DSPy-based outline-then-write |
| `SakanaAI/AI-Scientist` | https://github.com/SakanaAI/AI-Scientist | Per-section writing, Semantic Scholar RAG, automated reviewer |
| `SakanaAI/AI-Scientist-v2` | https://github.com/SakanaAI/AI-Scientist-v2 | Template-free agentic tree-search drafting |
| `markrussinovich/refchecker` | https://github.com/markrussinovich/refchecker | Citation validation via deterministic filters + LLM fallback |
| `grobidOrg/grobid` | https://github.com/grobidOrg/grobid | ML PDF → TEI/XML structured reference extraction |
| `sckott/habanero` | https://github.com/sckott/habanero | Python Crossref API client |
| `mseri/doi2bib` | https://github.com/mseri/doi2bib | DOI/arXiv/PubMed → BibTeX CLI |
| `noahshinn/reflexion` | https://github.com/noahshinn/reflexion | Official Reflexion implementation |
| `madaan/self-refine` | https://github.com/madaan/self-refine | Official Self-Refine implementation |
| `composable-models/llm_multiagent_debate` | https://github.com/composable-models/llm_multiagent_debate | Official multi-agent debate |
| `langchain-ai/langgraph-reflection` | https://github.com/langchain-ai/langgraph-reflection | Official LangGraph reflection/judge graph |
| `teacherpeterpan/self-correction-llm-papers` | https://github.com/teacherpeterpan/self-correction-llm-papers | Curated self-correction paper reading list |

---

## 11. SOTA Improvements

The following improvements go beyond the base architecture and are recommended for phased
adoption as Labmate matures.

### Writing

**W1 — Co-STORM Collaborative Outlining** (EMNLP 2024)
Replace single-agent `outline_skill()` with a multi-perspective protocol: multiple LLM expert
personas + a Moderator agent collaboratively build the outline and maintain a dynamic mind-map
knowledge base. The mind-map then becomes the shared fact store that prevents cross-section
contradiction (pitfall (i) in the research notes). Cost: 3-5× outline inference.

**W2 — AI Scientist v2 Template-Free Agentic Tree-Search**
Replace the fixed IMRaD scaffold with AI Scientist v2's template-free agentic tree search,
where the structure adapts to content rather than forcing every paper into one skeleton.
Add a vision-model feedback pass for figures. Appropriate once the fixed-scaffold pipeline
is validated.

**W3 — LLM-as-Reviewer Feedback Loop**
After the first full draft, invoke an automated peer-review prompt (AI Scientist's reviewer
or ResearchAgent's ReviewingAgents) and feed structured critiques back into a targeted
revision pass, rather than accepting the first draft. This is distinct from CritiqueSkill,
which reviews for quality; the automated reviewer reviews for scientific validity.

**W4 — Knowledge Graph / Shared Fact Store**
Track every cited fact across sections in a lightweight graph to prevent re-citation and
contradiction of the same fact across independently drafted sections. Implemented as a
dictionary keyed by fact + source, checked before each `draft_section()` call.

**W5 — CiteCheck Three-Class Severity**
Upgrade `validate_citations()` from binary valid/invalid to a three-class scheme:
`exact_match` | `minor_metadata_corruption` | `full_fabrication`. Minor metadata drift
(year off by one, venue abbreviation variant) is auto-corrected from the API response;
full fabrications are hard-blocked. This reduces the false-positive rate on legitimate
entries with minor metadata errors.

### Critique & Reflexion

**C1 — Mandatory External-Feedback Gating**
Make programmatic oracles the PRIMARY evaluator and LLM-as-judge a labeled fallback in all
paths, not just code critique. Already implemented in this spec; formalize as a policy that
every new task type must register an oracle or declare itself "oracle-free" before enabling
the critique loop.

**C2 — Agent-as-a-Judge Trajectory Evaluation** (Zhuge et al. 2024)
Have the evaluator judge the full reasoning trajectory and intermediate artifacts, not just
the final output. Aligns better with human review on code-gen and catches cascade failures
that output-only judges miss. Implement as an optional `trajectory_mode=True` flag on
`critique()`.

**C3 — RLAIF Distillation**
Distill the critique-revise traces back into the local Gemma model (Constitutional AI RL
phase) so it internalizes critique quality and needs fewer runtime iterations, cutting
inference cost on the A6000. Long-term improvement; requires accumulation of critique traces
as a training corpus.

**C4 — Tool-Using Structured Verification**
For code critique: actually EXECUTE the `suggested_revision` (apply patch + rerun test suite)
before returning it to the caller. Return only a revision that passes the test suite. Untested
fixes are advisory only and flagged as such.

**C5 — Explicit Written Constitution per Task Type**
Maintain a separate written constitution for each task type (code, academic writing, data
analysis). Each principle in the constitution is enumerated and assigned an id. Critiques
reference principle ids in `constitutional_violations`, making audits machine-readable.

**C6 — Adversarial Audit Pass**
Add a dedicated red-team auditor prompt that probes for jailbreaks, spec-gaming, and harmful
outputs, distinct from quality scoring. Run on every output before it leaves the critique loop.
The adversarial auditor is never the same call as the quality evaluator.

**C7 — Full Trace Logging with Drift Detection**
Log every actor/evaluator/reflection state to LangSmith or Langfuse. Run periodic calibration
of the LLM-as-judge against human labels to detect evaluator drift. Alert when judge agreement
with human labels drops below a configured threshold.
