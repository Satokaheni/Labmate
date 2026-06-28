---
name: academic-writing
description: >
  Produces IMRaD-structured academic papers through a hierarchical pipeline:
  outline → per-section draft → citation validation → style transfer → abstract (Chain-of-Density).
  Use when the agent needs to write a research paper, literature review, or structured report
  with validated citations. Each stage is independently invokable.
trigger: "Use when writing an academic paper or structured research document"
tools:
  - research_topic
  - outline_skill
  - draft_section
  - validate_citations
  - chain_of_density
  - style_transfer
version: "0.1.0"
license: MIT
requires: []
---

# Academic Writing Skill

A composable Python class implementing the STORM / AI-Scientist IMRaD writing
pipeline. Each stage is an independently invokable method.

## Pipeline

0. `research_topic(topic, refs, n_perspectives=3)` — STORM pre-writing phase.
   Generates diverse expert perspectives, interviews each against the supplied
   references (grounded answers only), and synthesizes structured `ResearchNotes`.
   `ResearchNotes.key_findings` feed directly into `outline_skill` as section key_points.
1. `outline_skill(topic, refs)` — STORM two-stage outline: cluster references by
   IMRaD section, emit a fixed IMRaD scaffold in canonical order. Raises `ValueError`
   if any mandatory section is missing.
2. `validate_citations(bibtex_entries)` — deterministic-first cascade
   (DOI → Crossref → arXiv → Semantic Scholar → LLM fallback). Non-optional:
   citation hallucination rates of 11-57% make this a hard architectural requirement.
   LLM-generated BibTeX text is never trusted verbatim; the normalized API response
   replaces it entirely.
3. `draft_section(section_name, refs, notes)` — per-section LLM call. Never drafts
   the whole paper in one shot. Raises `ValueError` if the output cites a key not
   in the supplied validated refs.
4. `style_transfer(text)` — single prompt-based Text Style Transfer pass over the
   assembled draft. Rejects the output if any `\cite{}`, `\ref{}`, or numeric token
   was altered.
5. `chain_of_density(text, target_words, iterations=3)` — abstract compression.
   Starts SPARSE, adds 1-3 missing salient entities per iteration while holding the
   word count fixed.

## Usage

```python
# Pre-writing STORM research phase, then outline seeded by its findings.
notes = skill.research_topic(topic, refs, n_perspectives=3)
outline = skill.outline_skill(topic, refs)
for section in outline.sections:
    section.key_points.extend(notes.key_findings)
```

```python
import dspy
from academic_writing_skill import AcademicWritingSkill, IMRAD_ORDER

skill = AcademicWritingSkill(lm=dspy.LM("openai/gemma-4-31B-it"))
outline = skill.outline_skill(topic, refs)
validated = skill.validate_citations([r.bibtex for r in refs])
good_refs = [r for r, v in zip(refs, validated) if v.valid]
sections = {s.name: skill.draft_section(s.name, good_refs, notes) for s in outline.sections}
draft = "\n\n".join(sections[n] for n in IMRAD_ORDER if n in sections)
draft = skill.style_transfer(draft)
abstract = skill.chain_of_density(draft, target_words=200)
```

## Critical rules

- Token counting uses `AutoTokenizer.from_pretrained("google/gemma-4-9b-it")` — never tiktoken.
- Citation validation is non-optional and runs before any reference enters the bibliography.
- Chain-of-Density starts sparse; starting dense is the most common misapplication.
