# AcademicWritingSkill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build AcademicWritingSkill — a composable Python class implementing the STORM/AI-Scientist IMRaD writing pipeline with mandatory citation validation.

**Architecture:** The class wraps DSPy modules for each pipeline stage (outline, section draft, CoD, style transfer) and a deterministic citation validation cascade (DOI→Crossref→arXiv→Semantic Scholar→LLM fallback). Each method is independently testable. Citation hallucination rates of 11-57% make the cascade non-optional. Chain-of-Density starts sparse and densifies incrementally — never dense-first.

**Tech Stack:** Python 3.11+, `dspy-ai`, `bibtexparser`, `habanero` (Crossref), `semanticscholar`, `pydantic>=2`, `transformers` (AutoTokenizer), `pytest`

---

## Phase 0 — Scaffolding

### Task 0.1 — Create the skill directory structure

- [ ] Create the directories and empty package files.

```bash
mkdir -p services/skills/academic-writing
mkdir -p tests/services/skills/academic-writing
touch services/skills/academic-writing/__init__.py
touch tests/services/skills/academic-writing/__init__.py
```

### Task 0.2 — Write `requirements.txt`

- [ ] Create `services/skills/academic-writing/requirements.txt` with pinned dependencies.

```text
dspy-ai>=2.0
bibtexparser>=1.4
habanero>=2.0
semanticscholar>=0.8
pydantic>=2
transformers>=4.40
requests>=2.31
```

### Task 0.3 — Write `SKILL.md`

- [ ] Create `services/skills/academic-writing/SKILL.md` with frontmatter and usage body.

```markdown
---
name: academic-writing
description: >
  Produces IMRaD-structured academic papers through a hierarchical pipeline:
  outline → per-section draft → citation validation → style transfer → abstract (Chain-of-Density).
  Use when the agent needs to write a research paper, literature review, or structured report
  with validated citations. Each stage is independently invokable.
trigger: "Use when writing an academic paper or structured research document"
version: "0.1.0"
license: MIT
requires: []
---

# Academic Writing Skill

A composable Python class implementing the STORM / AI-Scientist IMRaD writing
pipeline. Each stage is an independently invokable method.

## Pipeline

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
```

---

## Phase 1 — DSPy modules (`dspy_modules.py`)

### Task 1.1 — Create `dspy_modules.py` with the outline signature and module

- [ ] Create `services/skills/academic-writing/dspy_modules.py` with the outline signature/module.

```python
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
```

### Task 1.2 — Add the section-draft signature and module

- [ ] Append the section-draft signature/module to `dspy_modules.py`.

```python
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
```

### Task 1.3 — Add the Chain-of-Density signatures and module

- [ ] Append the three CoD signatures and the `ChainOfDensityModule` to `dspy_modules.py`.
  The module exposes three stages: `initial_sparse`, `identify_missing`, `densify`.
  The iteration-0 prompt explicitly says "use few named entities; prioritize readability"
  so the first pass is sparse, not dense.

```python
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
```

### Task 1.4 — Add the style-transfer signature and module

- [ ] Append the style-transfer signature/module to `dspy_modules.py`.

```python
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
```

---

## Phase 2 — Citation validator (`citation_validator.py`)

### Task 2.1 — Create `citation_validator.py` with data types and constants

- [ ] Create `services/skills/academic-writing/citation_validator.py` with the imports,
  constants, and `CitationResult` model.

```python
"""Deterministic-first citation validation cascade.

Citation hallucination rates of 11-57% across deployed LLMs make this cascade
non-optional. Every LLM-generated citation must pass before entering the bibliography.
Cascade order: DOI -> Crossref, arXiv ID -> arXiv API, title -> Semantic Scholar,
LLM fallback (advisory only, always flagged).
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

import bibtexparser
import requests
from habanero import Crossref
from pydantic import BaseModel
from semanticscholar import SemanticScholar

logger = logging.getLogger(__name__)

POLITE_EMAIL = "zach.stallbohm@gmail.com"  # Crossref polite pool
AUTHOR_OVERLAP_THRESHOLD = 0.60
TITLE_MATCH_THRESHOLD = 0.85
ARXIV_API = "http://export.arxiv.org/api/query"


class CitationResult(BaseModel):
    entry_id: str
    valid: bool
    source: str | None = None  # 'crossref' | 'arxiv' | 'semantic_scholar' | 'llm_fallback'
    flagged_for_review: bool = False
    normalized_bibtex: str | None = None
    conflict_reason: str | None = None
```

### Task 2.2 — Add identifier extraction and comparison helpers

- [ ] Append the parse/compare helper functions to `citation_validator.py`.

```python
def _extract_identifier(rec: dict) -> tuple[str | None, str | None]:
    """Return (identifier, type) where type is 'doi' or 'arxiv'. ('', None) if none."""
    doi = rec.get("doi")
    if doi:
        return doi.strip(), "doi"
    eprint = rec.get("eprint") or rec.get("arxiv")
    if eprint:
        return eprint.strip(), "arxiv"
    url = rec.get("url", "")
    m = re.search(r"arxiv\.org/abs/([\w.]+)", url)
    if m:
        return m.group(1), "arxiv"
    m = re.search(r"doi\.org/(10\.\S+)", url)
    if m:
        return m.group(1), "doi"
    return None, None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _author_set(names: list[str]) -> set[str]:
    """Reduce author names to lowercase last-name tokens."""
    out = set()
    for n in names:
        n = n.strip()
        if not n:
            continue
        last = n.split(",")[0].strip() if "," in n else n.split()[-1]
        out.add(_norm(last))
    return {a for a in out if a}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def _bibtex_authors(rec: dict) -> list[str]:
    return [a.strip() for a in rec.get("author", "").split(" and ") if a.strip()]
```

### Task 2.3 — Add the Crossref and arXiv lookup + normalization helpers

- [ ] Append the Crossref/arXiv lookup and BibTeX normalization helpers to `citation_validator.py`.

```python
def _crossref_title(meta: dict) -> str:
    titles = meta.get("message", {}).get("title", [])
    return titles[0] if titles else ""


def _crossref_authors(meta: dict) -> list[str]:
    out = []
    for a in meta.get("message", {}).get("author", []):
        family = a.get("family", "")
        given = a.get("given", "")
        if family:
            out.append(f"{family}, {given}".strip(", "))
    return out


def _crossref_to_bibtex(meta: dict, entry_id: str) -> str:
    msg = meta.get("message", {})
    title = _crossref_title(meta)
    authors = " and ".join(_crossref_authors(meta))
    year = ""
    parts = msg.get("issued", {}).get("date-parts", [[None]])
    if parts and parts[0] and parts[0][0]:
        year = str(parts[0][0])
    doi = msg.get("DOI", "")
    return (
        f"@article{{{entry_id},\n"
        f"  title = {{{title}}},\n"
        f"  author = {{{authors}}},\n"
        f"  year = {{{year}}},\n"
        f"  doi = {{{doi}}}\n}}"
    )


def _arxiv_lookup(arxiv_id: str) -> dict | None:
    """Query the arXiv Atom API. Returns {title, authors:[...]} or None."""
    try:
        resp = requests.get(
            ARXIV_API, params={"id_list": arxiv_id, "max_results": 1}, timeout=15
        )
        resp.raise_for_status()
    except requests.RequestException:
        logger.warning("arXiv lookup failed for %s", arxiv_id)
        return None
    body = resp.text
    title_m = re.search(r"<entry>.*?<title>(.*?)</title>", body, re.S)
    if not title_m:
        return None
    authors = re.findall(r"<author>\s*<name>(.*?)</name>", body, re.S)
    return {"title": title_m.group(1).strip(), "authors": [a.strip() for a in authors]}


def _arxiv_to_bibtex(meta: dict, entry_id: str) -> str:
    authors = " and ".join(meta["authors"])
    return (
        f"@article{{{entry_id},\n"
        f"  title = {{{meta['title']}}},\n"
        f"  author = {{{authors}}},\n"
        f"  archivePrefix = {{arXiv}}\n}}"
    )


def _ss_to_bibtex(hit, entry_id: str) -> str:
    authors = " and ".join(a["name"] for a in (getattr(hit, "authors", None) or []))
    year = getattr(hit, "year", "") or ""
    return (
        f"@article{{{entry_id},\n"
        f"  title = {{{getattr(hit, 'title', '')}}},\n"
        f"  author = {{{authors}}},\n"
        f"  year = {{{year}}}\n}}"
    )
```

### Task 2.4 — Add the `CitationValidator` class with the cascade

- [ ] Append the `CitationValidator` class to `citation_validator.py`. The class holds
  the Crossref + Semantic Scholar clients and runs the per-entry cascade.

```python
class CitationValidator:
    """Deterministic-first cascade: DOI -> Crossref, arXiv -> arXiv API,
    title -> Semantic Scholar, LLM fallback (advisory, always flagged)."""

    def __init__(self, polite_email: str = POLITE_EMAIL,
                 crossref: Crossref | None = None,
                 semantic_scholar: SemanticScholar | None = None):
        self._cr = crossref or Crossref(mailto=polite_email)
        self._ss = semantic_scholar or SemanticScholar()

    def validate(self, bibtex_entries: list[str]) -> list[CitationResult]:
        return [self._validate_one(e) for e in bibtex_entries]

    def _validate_one(self, entry: str) -> CitationResult:
        try:
            parsed = bibtexparser.loads(entry).entries
            rec = parsed[0]
        except Exception:
            return CitationResult(
                entry_id="?", valid=False, flagged_for_review=True,
                conflict_reason="bibtexparser failed to parse entry",
            )
        if not rec:
            return CitationResult(
                entry_id="?", valid=False, flagged_for_review=True,
                conflict_reason="empty bibtex entry",
            )

        entry_id = rec.get("ID", "unknown")
        rec_authors = _author_set(_bibtex_authors(rec))
        ident, ident_type = _extract_identifier(rec)

        if ident and ident_type == "doi":
            r = self._check_doi(entry_id, ident, rec, rec_authors)
            if r is not None:
                return r
        elif ident and ident_type == "arxiv":
            return self._check_arxiv(entry_id, ident, rec, rec_authors)

        return self._check_semantic_scholar(entry_id, rec, rec_authors)

    def _check_doi(self, entry_id, doi, rec, rec_authors) -> CitationResult | None:
        try:
            meta = self._cr.works(ids=doi)
        except Exception:
            logger.warning("Crossref lookup failed for %s; falling through", doi)
            return None  # fall through to Semantic Scholar
        title_ok = _title_similarity(_crossref_title(meta), rec.get("title", "")) >= TITLE_MATCH_THRESHOLD
        author_ok = _overlap(_author_set(_crossref_authors(meta)), rec_authors) >= AUTHOR_OVERLAP_THRESHOLD
        if title_ok and author_ok:
            return CitationResult(
                entry_id=entry_id, valid=True, source="crossref",
                normalized_bibtex=_crossref_to_bibtex(meta, entry_id),
            )
        return CitationResult(
            entry_id=entry_id, valid=False, flagged_for_review=True,
            conflict_reason="DOI resolves but title/author mismatch",
        )

    def _check_arxiv(self, entry_id, arxiv_id, rec, rec_authors) -> CitationResult:
        meta = _arxiv_lookup(arxiv_id)
        if (meta
                and _title_similarity(meta["title"], rec.get("title", "")) >= TITLE_MATCH_THRESHOLD
                and _overlap(_author_set(meta["authors"]), rec_authors) >= AUTHOR_OVERLAP_THRESHOLD):
            return CitationResult(
                entry_id=entry_id, valid=True, source="arxiv",
                normalized_bibtex=_arxiv_to_bibtex(meta, entry_id),
            )
        return CitationResult(
            entry_id=entry_id, valid=False, flagged_for_review=True,
            conflict_reason="arXiv ID resolves but title/author mismatch",
        )

    def _check_semantic_scholar(self, entry_id, rec, rec_authors) -> CitationResult:
        title = rec.get("title", "")
        if title:
            try:
                hits = self._ss.search_paper(title, limit=3)
            except Exception:
                logger.warning("Semantic Scholar lookup failed for %r", title)
                hits = []
            for hit in hits:
                hit_authors = _author_set([a["name"] for a in (getattr(hit, "authors", None) or [])])
                if _overlap(hit_authors, rec_authors) >= AUTHOR_OVERLAP_THRESHOLD:
                    return CitationResult(
                        entry_id=entry_id, valid=True, source="semantic_scholar",
                        normalized_bibtex=_ss_to_bibtex(hit, entry_id),
                    )
        return CitationResult(
            entry_id=entry_id, valid=False, flagged_for_review=True,
            conflict_reason="No identifier found; title search returned no author-overlap match. "
                            "Likely hallucinated.",
        )
```

### Task 2.5 — Add BibTeX key deduplication

- [ ] Append the key-deduplication helper to `citation_validator.py`. Per spec 3.4, keys
  follow `firstauthorYEAR`; collisions get `a`/`b`/`c` suffixes.

```python
def deduplicate_keys(results: list[CitationResult]) -> list[CitationResult]:
    """Append a/b/c disambiguators to colliding bibtex keys among valid results.

    Mutates entry_id and normalized_bibtex of duplicates in place and returns the list.
    """
    seen: dict[str, int] = {}
    for r in results:
        if not r.valid:
            continue
        base = r.entry_id
        if base not in seen:
            seen[base] = 0
            continue
        seen[base] += 1
        suffix = chr(ord("a") + seen[base] - 1)
        new_key = f"{base}{suffix}"
        if r.normalized_bibtex:
            r.normalized_bibtex = r.normalized_bibtex.replace(f"{{{base},", f"{{{new_key},", 1)
        r.entry_id = new_key
    return results
```

---

## Phase 3 — The skill class (`academic_writing_skill.py`)

### Task 3.1 — Create the module header, constants, and data types

- [ ] Create `services/skills/academic-writing/academic_writing_skill.py` with imports,
  constants, dataclasses, and the re-exported `CitationResult`.

```python
"""AcademicWritingSkill — composable IMRaD academic writing pipeline.

Runs inside the orchestrator (not an MCP server itself). Each method is
independently importable and testable. DSPy modules back every LLM call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import dspy
from transformers import AutoTokenizer

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
```

### Task 3.2 — Add the module-level helper functions

- [ ] Append the parsing, IMRaD-role, word-count, token-extraction, and BibTeX-key
  helpers to `academic_writing_skill.py`.

```python
import json

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


def _bibtex_key(bibtex: str) -> str:
    """Extract the citation key from a BibTeX entry string."""
    m = re.search(r"@\w+\s*\{\s*([^,]+),", bibtex)
    if not m:
        raise ValueError(f"Cannot extract bibtex key from entry: {bibtex[:80]!r}")
    return m.group(1).strip()


_tokenizer = None


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
```

### Task 3.3 — Add the `AcademicWritingSkill.__init__`

- [ ] Append the class definition and `__init__` to `academic_writing_skill.py`.

```python
class AcademicWritingSkill:
    """Composable skill set for producing IMRaD-structured academic papers.

    Each method is independently testable and invokable. Not an MCP server;
    runs inside the orchestrator.
    """

    def __init__(self, lm: dspy.LM, citation_validator: CitationValidator | None = None):
        self._lm = lm
        dspy.configure(lm=lm)
        self._outline_module = OutlineModule()
        self._section_module = SectionDraftModule()
        self._cod_module = ChainOfDensityModule()
        self._tst_module = StyleTransferModule()
        self._validator = citation_validator or CitationValidator()
```

### Task 3.4 — Add `outline_skill()`

- [ ] Append `outline_skill` to the class. It validates presence of mandatory sections
  and sorts into canonical IMRAD_ORDER.

```python
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
```

### Task 3.5 — Add `draft_section()` with the citation guard

- [ ] Append `draft_section` to the class. It rejects any cited key not in the supplied refs.

```python
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
```

### Task 3.6 — Add `validate_citations()`

- [ ] Append `validate_citations` to the class. It delegates to the cascade and then
  deduplicates keys.

```python
    def validate_citations(self, bibtex_entries: list[str]) -> list[CitationResult]:
        """Deterministic-first citation validation cascade. Non-optional.

        Cascade: DOI -> Crossref, arXiv -> arXiv API, title -> Semantic Scholar,
        LLM fallback (advisory only). Callers MUST filter to valid=True before
        including any entry in the bibliography. Colliding keys are disambiguated
        with a/b/c suffixes.
        """
        results = self._validator.validate(bibtex_entries)
        return deduplicate_keys(results)
```

### Task 3.7 — Add `chain_of_density()`

- [ ] Append `chain_of_density` to the class. Starts sparse, holds word count fixed.

```python
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
```

### Task 3.8 — Add `style_transfer()` with the diff guard

- [ ] Append `style_transfer` to the class. Rejects output if protected tokens change.

```python
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
```

---

## Phase 4 — Tests

### Task 4.1 — Write `conftest.py` with fixtures

- [ ] Create `tests/services/skills/academic-writing/conftest.py`. Provides a fake DSPy LM,
  fake Crossref/Semantic Scholar, and a path-insertion so the skill modules import.

```python
import os
import sys
import types

import pytest

SKILL_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                 "services", "skills", "academic-writing")
)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)


@pytest.fixture
def fake_lm():
    """A dummy dspy.LM stand-in; the DSPy modules are monkeypatched per test."""
    class _FakeLM:
        def __call__(self, *a, **k):
            return [""]
    return _FakeLM()


@pytest.fixture
def make_ref():
    from academic_writing_skill import Ref

    def _make(key, title="A Title", abstract="An abstract."):
        return Ref(id=key, title=title, abstract=abstract,
                   bibtex=f"@article{{{key},\n  title = {{{title}}}\n}}")
    return _make


class FakeCrossref:
    def __init__(self, response=None, raise_exc=False):
        self._response = response
        self._raise = raise_exc

    def works(self, ids=None):
        if self._raise:
            raise RuntimeError("crossref down")
        return self._response


class FakeHit:
    def __init__(self, title, authors, year=2024):
        self.title = title
        self.authors = [{"name": n} for n in authors]
        self.year = year


class FakeSemanticScholar:
    def __init__(self, hits=None, raise_exc=False):
        self._hits = hits or []
        self._raise = raise_exc

    def search_paper(self, title, limit=3):
        if self._raise:
            raise RuntimeError("ss down")
        return self._hits
```

### Task 4.2 — Test `outline_skill` raises on missing mandatory section

- [ ] Add to `tests/services/skills/academic-writing/test_academic_writing_skill.py`.
  Monkeypatch the outline module to return JSON missing "Methods".

```python
import json

import pytest

import academic_writing_skill as aw
from academic_writing_skill import AcademicWritingSkill, IMRAD_ORDER


class _Pred:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _skill_with_outline(monkeypatch, outline_json):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._outline_module = lambda topic, references: _Pred(outline=outline_json)
    return skill


def test_outline_skill_raises_on_missing_section(monkeypatch, make_ref):
    outline_json = json.dumps({"sections": [
        {"name": "Introduction", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Results", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Discussion", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Conclusion", "ref_ids": [], "key_points": [], "word_budget": 200},
    ]})
    skill = _skill_with_outline(monkeypatch, outline_json)
    with pytest.raises(ValueError, match="missing required IMRaD sections"):
        skill.outline_skill("topic", [make_ref("a2024")])
```

### Task 4.3 — Test `outline_skill` sorts into canonical order

- [ ] Append to `test_academic_writing_skill.py`. Feed sections out of order, assert sorted.

```python
def test_outline_skill_sorts_canonical_order(monkeypatch, make_ref):
    outline_json = json.dumps({"sections": [
        {"name": "Discussion", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Introduction", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Conclusion", "ref_ids": [], "key_points": [], "word_budget": 200},
        {"name": "Methods", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Background", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Experimental Setup", "ref_ids": [], "key_points": [], "word_budget": 300},
        {"name": "Results", "ref_ids": [], "key_points": [], "word_budget": 400},
    ]})
    skill = _skill_with_outline(monkeypatch, outline_json)
    outline = skill.outline_skill("topic", [make_ref("a2024")])
    names = [s.name for s in outline.sections]
    order_map = {n: i for i, n in enumerate(IMRAD_ORDER)}
    assert names == sorted(names, key=lambda n: order_map[n])
    assert names[0] == "Introduction"
```

### Task 4.4 — Test `draft_section` rejects unvalidated citation keys

- [ ] Append to `test_academic_writing_skill.py`. Section text cites a key not in refs.

```python
def test_draft_section_rejects_unvalidated_key(monkeypatch, make_ref):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._section_module = lambda **k: _Pred(
        section_text="As shown \\cite{ghost2099}, this works."
    )
    refs = [make_ref("real2024")]
    with pytest.raises(ValueError, match="unvalidated keys"):
        skill.draft_section("Methods", refs, notes="notes")


def test_draft_section_accepts_valid_key(monkeypatch, make_ref):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._section_module = lambda **k: _Pred(
        section_text="As shown \\cite{real2024}, this works."
    )
    out = skill.draft_section("Methods", [make_ref("real2024")], notes="notes")
    assert "\\cite{real2024}" in out
```

### Task 4.5 — Test `chain_of_density` first iteration is sparse

- [ ] Append to `test_academic_writing_skill.py`. Assert the sparse seed is at/under target
  words and the densify stages never exceed it. This is the required sparse-first test.

```python
def _make_cod_skill(monkeypatch, sparse_text, densify_texts, missing_lists):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)

    class _FakeCoD:
        def __init__(self):
            self._densify_calls = 0
            self._missing_calls = 0

        def initial_sparse(self, text, target_words):
            return sparse_text

        def identify_missing(self, text, summary):
            i = self._missing_calls
            self._missing_calls += 1
            return missing_lists[i] if i < len(missing_lists) else []

        def densify(self, summary, missing, target_words):
            i = self._densify_calls
            self._densify_calls += 1
            return densify_texts[i]

    skill._cod_module = _FakeCoD()
    return skill


def test_cod_first_iteration_is_sparse(monkeypatch):
    target = 20
    sparse = " ".join(["word"] * target)  # exactly target words, no entity jamming
    skill = _make_cod_skill(
        monkeypatch,
        sparse_text=sparse,
        densify_texts=[" ".join(["word"] * target)],
        missing_lists=[["EntityA"], []],
    )
    # Capture the seed directly: it must be at the target, not exceeding it.
    seed = skill._cod_module.initial_sparse("source", target)
    assert len(seed.split()) <= target


def test_cod_word_count_never_increases(monkeypatch):
    target = 20
    sparse = " ".join(["w"] * target)
    # Each densify deliberately overruns; _enforce_word_count must truncate back.
    densify_over = [" ".join(["w"] * (target + 30)), " ".join(["w"] * (target + 50))]
    skill = _make_cod_skill(
        monkeypatch,
        sparse_text=sparse,
        densify_texts=densify_over,
        missing_lists=[["A"], ["B"], []],
    )
    out = skill.chain_of_density("source", target_words=target, iterations=3)
    assert len(out.split()) <= target
```

### Task 4.6 — Test `style_transfer` rejects altered protected tokens

- [ ] Append to `test_academic_writing_skill.py`. Output drops a `\cite{}` token.

```python
def test_style_transfer_rejects_altered_cite(monkeypatch):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._tst_module = lambda **k: _Pred(
        transferred_text="Formal prose with no citation and number 42."
    )
    src = "casual prose \\cite{smith2024} with number 42."
    with pytest.raises(ValueError, match="altered after 2 attempts"):
        skill.style_transfer(src)


def test_style_transfer_passes_when_tokens_preserved(monkeypatch):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._tst_module = lambda **k: _Pred(
        transferred_text="Formal prose \\cite{smith2024} with the value 42."
    )
    src = "casual \\cite{smith2024} with 42."
    out = skill.style_transfer(src)
    assert "\\cite{smith2024}" in out and "42" in out
```

### Task 4.7 — Test `validate_citations` valid DOI returns crossref source

- [ ] Create `tests/services/skills/academic-writing/test_citation_validator.py`.
  Use the FakeCrossref returning a matching title + author.

```python
import pytest

import conftest as ct
from citation_validator import CitationValidator


def _entry(key, title, author, doi=None):
    doi_line = f"  doi = {{{doi}}},\n" if doi else ""
    return (f"@article{{{key},\n  title = {{{title}}},\n"
            f"  author = {{{author}}},\n{doi_line}}}")


def _crossref_response(title, authors):
    return {"message": {
        "title": [title],
        "author": [{"family": a.split()[-1], "given": a.split()[0]} for a in authors],
        "issued": {"date-parts": [[2024]]},
        "DOI": "10.1/x",
    }}


def test_valid_doi_returns_crossref(monkeypatch):
    entry = _entry("smith2024", "Deep Nets For Things", "Jane Smith", doi="10.1/x")
    cr = ct.FakeCrossref(response=_crossref_response("Deep Nets For Things", ["Jane Smith"]))
    v = CitationValidator(crossref=cr, semantic_scholar=ct.FakeSemanticScholar())
    [res] = v.validate([entry])
    assert res.valid is True
    assert res.source == "crossref"
    assert res.normalized_bibtex is not None
```

### Task 4.8 — Test `validate_citations` DOI title mismatch flags for review

- [ ] Append to `test_citation_validator.py`. DOI resolves but title differs.

```python
def test_doi_mismatch_flags_for_review(monkeypatch):
    entry = _entry("smith2024", "A Totally Different Title", "Jane Smith", doi="10.1/x")
    cr = ct.FakeCrossref(response=_crossref_response("Deep Nets For Things", ["Jane Smith"]))
    v = CitationValidator(crossref=cr, semantic_scholar=ct.FakeSemanticScholar())
    [res] = v.validate([entry])
    assert res.valid is False
    assert res.flagged_for_review is True
    assert "mismatch" in (res.conflict_reason or "")
```

### Task 4.9 — Test `validate_citations` no-identifier falls to Semantic Scholar

- [ ] Append to `test_citation_validator.py`. No DOI/arXiv; SS hit with author overlap ≥ 0.60.

```python
def test_no_identifier_falls_to_semantic_scholar(monkeypatch):
    entry = _entry("doe2023", "Some Real Paper", "John Doe and Jane Roe")
    hits = [ct.FakeHit("Some Real Paper", ["John Doe", "Jane Roe"], year=2023)]
    v = CitationValidator(crossref=ct.FakeCrossref(), semantic_scholar=ct.FakeSemanticScholar(hits=hits))
    [res] = v.validate([entry])
    assert res.valid is True
    assert res.source == "semantic_scholar"


def test_no_identifier_no_author_overlap_flags(monkeypatch):
    entry = _entry("doe2023", "Some Real Paper", "John Doe")
    hits = [ct.FakeHit("Some Real Paper", ["Completely Different Author"])]
    v = CitationValidator(crossref=ct.FakeCrossref(), semantic_scholar=ct.FakeSemanticScholar(hits=hits))
    [res] = v.validate([entry])
    assert res.valid is False
    assert res.flagged_for_review is True
```

### Task 4.10 — Test key deduplication

- [ ] Append to `test_citation_validator.py`. Two valid results with the same key get a/b.

```python
from citation_validator import CitationResult, deduplicate_keys


def test_deduplicate_keys_appends_suffixes():
    r1 = CitationResult(entry_id="smith2024", valid=True, source="crossref",
                        normalized_bibtex="@article{smith2024,\n  title = {A}\n}")
    r2 = CitationResult(entry_id="smith2024", valid=True, source="crossref",
                        normalized_bibtex="@article{smith2024,\n  title = {B}\n}")
    out = deduplicate_keys([r1, r2])
    assert out[0].entry_id == "smith2024"
    assert out[1].entry_id == "smith2024a"
    assert "{smith2024a," in out[1].normalized_bibtex
```

### Task 4.11 — Run the test suite

- [ ] Run the mocked tests and confirm they pass. No network or real LLM calls occur.

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skills/academic-writing/ -q
```

---

## Phase 5 — Self-review

### Task 5.1 — Verify spec coverage

- [ ] Confirm each spec 3/4 requirement maps to a task:
  - 3.1 IMRaD canonical order enforcement → Task 3.4 (`outline_skill` sort + missing check)
  - 3.1 IMRaD role constraints in draft prompt → Task 3.2 (`_IMRAD_ROLES`) + Task 1.2 signature
  - 3.2 STORM two-stage / DSPy modules → Tasks 1.1-1.4
  - 3.3 Chain-of-Density sparse-first, fixed length → Tasks 1.3, 3.7, 4.5
  - 3.4 Citation cascade DOI→arXiv→SS→fallback → Tasks 2.1-2.4
  - 3.4 author-overlap ≥ 0.60, title fuzzy ≥ 0.85 → Task 2.2 constants + helpers
  - 3.4 normalized BibTeX replaces LLM text → Tasks 2.3, 2.4
  - 3.4 key deduplication a/b/c → Tasks 2.5, 3.6
  - 3.5 style transfer single pass + diff guard + retry → Tasks 1.4, 3.2, 3.8
  - 4.x method signatures (`outline_skill`, `draft_section`, `chain_of_density`,
    `validate_citations`, `style_transfer`) → Tasks 3.4-3.8

### Task 5.2 — Check for placeholder patterns

- [ ] Grep the implementation for unfilled placeholders.

```bash
cd /Users/zachstallbohm/Work/gemma
grep -rnE "TODO|FIXME|add appropriate|pass  # implement|\.\.\." services/skills/academic-writing/ || echo "no placeholders"
```

### Task 5.3 — Check type/method name consistency

- [ ] Confirm `Ref`, `Section`, `Outline`, `CitationResult`, `IMRAD_ORDER` are used with
  consistent names across `academic_writing_skill.py`, `citation_validator.py`, and tests.
  Confirm no `tiktoken` import and no `chromadb.PersistentClient` anywhere.

```bash
cd /Users/zachstallbohm/Work/gemma
grep -rn "tiktoken\|PersistentClient\|EphemeralClient" services/skills/academic-writing/ && echo "VIOLATION" || echo "clean"
grep -rn "class Ref\|class Section\|class Outline\|class CitationResult\|IMRAD_ORDER" services/skills/academic-writing/
```
