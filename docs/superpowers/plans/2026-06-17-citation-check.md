# citation-check MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the citation-check Python MCP server implementing RefChecker claim-triplet verification and CiteCheck bibliography hallucination detection.

**Architecture:** Two verifiers: ClaimVerifier extracts claim-triplets from LLM text using a local Gemma call then classifies each triplet against supplied reference passages (entailed/contradicted/unverifiable). CitationVerifier runs the deterministic cascade (DOI→Crossref→arXiv→Semantic Scholar) and classifies citation accuracy as exact/minor/major hallucination. Both return structured Pydantic models serialized to JSON. All logging to stderr.

**Tech Stack:** Python 3.11+, `mcp` SDK, `pydantic>=2`, `habanero` (Crossref), `semanticscholar`, `bibtexparser`, `litellm` (for local Gemma calls), `pytest`

---

## Phase 0 — Scaffolding

### Task 0.1 — Create the skill directory structure

- [ ] Create the directories and empty package files.

```bash
mkdir -p services/skills/citation-check
mkdir -p tests/services/skills/citation-check
touch services/skills/citation-check/__init__.py
touch tests/services/skills/citation-check/__init__.py
```

### Task 0.2 — Write `requirements.txt`

- [ ] Create `services/skills/citation-check/requirements.txt` with pinned dependencies.

```text
mcp>=1.0
pydantic>=2
habanero>=2.0
semanticscholar>=0.8
bibtexparser>=1.4
litellm>=1.40
transformers>=4.40
requests>=2.31
```

### Task 0.3 — Write `SKILL.md`

- [ ] Create `services/skills/citation-check/SKILL.md` with frontmatter and usage body.

```markdown
---
name: citation-check
description: >
  Verifies claims and citations against external evidence. verify_claims decomposes
  LLM text into claim-triplets and checks each against supplied references (entailed/
  contradicted/unverifiable). verify_citations checks BibTeX entries against
  Semantic Scholar/Crossref (exact/minor/major hallucination). Use as the grounding
  layer in any critique or academic writing workflow.
trigger: "Use when verifying factual claims or bibliography entries in generated text"
tools:
  - citation_check.verify_claims
  - citation_check.verify_citations
version: "0.1.0"
license: MIT
requires: []
---

# Citation Check Skill

External grounding layer for the `critique` Reflexion loop and citation-validation
supplement for `academic-writing`. Two complementary verification tools:

- **`citation_check.verify_claims(text, references)`** — RefChecker (arXiv:2405.14486).
  Decomposes `text` into (subject, predicate, object) claim-triplets via a local Gemma
  call, then classifies each triplet against the supplied `references` as
  **entailed**, **contradicted**, or **unverifiable**. Returns JSON with per-claim
  verdicts and aggregate counts.

- **`citation_check.verify_citations(bibliography)`** — CiteCheck (arXiv:2605.27700).
  Verifies each BibTeX entry against the deterministic cascade
  (DOI→Crossref→arXiv→Semantic Scholar) and classifies as **exact_match**,
  **minor_hallucination** (correct paper, corrupted field), or **major_hallucination**
  (fabricated / not found). Returns JSON with per-entry classification and the
  normalized BibTeX where a match was found.

## Invocation

Both tools are exposed over MCP stdio JSON-RPC. Inputs and outputs are JSON strings.

## Notes

- Claim extraction uses the local Gemma 4 server via `GEMMA_BASE`
  (default `http://localhost:8000/v1`), not OpenAI.
- The citation cascade is deterministic-first and reuses the logic from the
  `academic-writing` skill.
- stdout carries JSON-RPC only — all diagnostics go to stderr.
```

---

## Phase 1 — Shared types

### Task 1.1 — Write `models.py` with the Pydantic types

- [ ] Create `services/skills/citation-check/models.py`.

```python
"""Shared Pydantic models for the citation-check skill."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ClaimTriplet(BaseModel):
    subject: str
    predicate: str
    object: str
    verdict: Literal["entailed", "contradicted", "unverifiable"]
    evidence: str | None = None  # quoted passage from reference that entails/contradicts


class ClaimVerificationResult(BaseModel):
    text: str
    triplets: list[ClaimTriplet]
    entailed_count: int
    contradicted_count: int
    unverifiable_count: int

    @classmethod
    def from_triplets(cls, text: str, triplets: list[ClaimTriplet]) -> "ClaimVerificationResult":
        return cls(
            text=text,
            triplets=triplets,
            entailed_count=sum(1 for t in triplets if t.verdict == "entailed"),
            contradicted_count=sum(1 for t in triplets if t.verdict == "contradicted"),
            unverifiable_count=sum(1 for t in triplets if t.verdict == "unverifiable"),
        )


class CitationCheckResult(BaseModel):
    entry_id: str
    verdict: Literal["exact_match", "minor_hallucination", "major_hallucination"]
    field_errors: list[str] = []  # specific corrupted fields for minor_hallucination
    source: str | None = None  # 'crossref' | 'semantic_scholar' | 'arxiv'
    normalized_bibtex: str | None = None
```

---

## Phase 2 — ClaimVerifier (RefChecker)

### Task 2.1 — Scaffold `claim_verifier.py` with the Gemma client config

- [ ] Create `services/skills/citation-check/claim_verifier.py` with stderr logging and the `GEMMA_BASE` config. NEVER `print()`.

```python
"""ClaimVerifier — RefChecker (arXiv:2405.14486) claim-triplet extraction
and 3-way verification against reference passages."""
from __future__ import annotations

import json
import logging
import os
import sys

import litellm

from models import ClaimTriplet, ClaimVerificationResult

# All diagnostics to stderr — stdout is reserved for JSON-RPC.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("citation-check.claim")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")
```

### Task 2.2 — Implement claim-triplet extraction prompt + call

- [ ] Add `_extract_triplets` to `claim_verifier.py`. It calls the local Gemma via litellm and parses a JSON array of triplets.

```python
EXTRACTION_PROMPT = """You are a claim extractor. Decompose the TEXT below into atomic \
knowledge claims. Each claim is a (subject, predicate, object) triplet capturing one \
verifiable fact. Do not infer beyond the text. Return ONLY a JSON array, e.g.:
[{"subject": "BERT", "predicate": "was introduced by", "object": "Devlin et al. 2018"}]

TEXT:
{text}
"""


def _call_gemma(prompt: str) -> str:
    resp = litellm.completion(
        model=f"openai/{GEMMA_MODEL}",
        api_base=GEMMA_BASE,
        api_key="not-needed",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
    )
    return resp["choices"][0]["message"]["content"]


def _parse_json_array(raw: str) -> list[dict]:
    """Tolerant parse: strip code fences, locate the first JSON array."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1:
        log.warning("no JSON array found in extraction output")
        return []
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        log.warning("failed to parse extraction JSON")
        return []


def _extract_triplets(text: str) -> list[dict]:
    raw = _call_gemma(EXTRACTION_PROMPT.format(text=text))
    return _parse_json_array(raw)
```

### Task 2.3 — Implement 3-way classification against references

- [ ] Add `_classify_triplet` to `claim_verifier.py`. It prompts Gemma to judge one triplet against the joined references and returns a verdict + evidence.

```python
CLASSIFY_PROMPT = """Given the REFERENCES and a single CLAIM triplet, decide whether the \
references support the claim. Answer with strict JSON:
{{"verdict": "entailed|contradicted|unverifiable", "evidence": "<quoted passage or null>"}}

- "entailed": the references clearly support the claim.
- "contradicted": the references clearly state the opposite.
- "unverifiable": the references neither support nor contradict it.

REFERENCES:
{references}

CLAIM: subject="{subject}" predicate="{predicate}" object="{object}"
"""


def _classify_triplet(triplet: dict, references_blob: str) -> ClaimTriplet:
    prompt = CLASSIFY_PROMPT.format(
        references=references_blob,
        subject=triplet.get("subject", ""),
        predicate=triplet.get("predicate", ""),
        object=triplet.get("object", ""),
    )
    raw = _call_gemma(prompt)
    verdict, evidence = "unverifiable", None
    try:
        s = raw[raw.find("{") : raw.rfind("}") + 1]
        parsed = json.loads(s)
        v = parsed.get("verdict")
        if v in ("entailed", "contradicted", "unverifiable"):
            verdict = v
        evidence = parsed.get("evidence") or None
    except (json.JSONDecodeError, ValueError):
        log.warning("failed to parse classification JSON; defaulting unverifiable")
    return ClaimTriplet(
        subject=triplet.get("subject", ""),
        predicate=triplet.get("predicate", ""),
        object=triplet.get("object", ""),
        verdict=verdict,
        evidence=evidence,
    )
```

### Task 2.4 — Implement the public `verify_claims` entry point

- [ ] Add `verify_claims` to `claim_verifier.py`. It extracts triplets, classifies each, and assembles a `ClaimVerificationResult`.

```python
def verify_claims(text: str, references: list[str]) -> ClaimVerificationResult:
    references_blob = "\n\n".join(references) if references else ""
    raw_triplets = _extract_triplets(text)
    triplets: list[ClaimTriplet] = []
    for rt in raw_triplets:
        if not references_blob:
            triplets.append(
                ClaimTriplet(
                    subject=rt.get("subject", ""),
                    predicate=rt.get("predicate", ""),
                    object=rt.get("object", ""),
                    verdict="unverifiable",
                )
            )
            continue
        triplets.append(_classify_triplet(rt, references_blob))
    return ClaimVerificationResult.from_triplets(text, triplets)
```

---

## Phase 3 — CitationVerifier (CiteCheck)

### Task 3.1 — Scaffold `citation_verifier.py` and parse BibTeX

- [ ] Create `services/skills/citation-check/citation_verifier.py` with stderr logging and a BibTeX parser that yields normalized entry dicts.

```python
"""CitationVerifier — CiteCheck (arXiv:2605.27700) bibliography hallucination
detection via the deterministic cascade (DOI→Crossref→arXiv→Semantic Scholar)."""
from __future__ import annotations

import logging
import sys

import bibtexparser

from models import CitationCheckResult

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("citation-check.citation")


def _parse_entries(bib_str: str) -> list[dict]:
    """Parse a single BibTeX string into a list of normalized field dicts."""
    db = bibtexparser.loads(bib_str)
    entries = []
    for e in db.entries:
        entries.append(
            {
                "id": e.get("ID", "unknown"),
                "title": e.get("title", "").strip("{} ").strip(),
                "author": e.get("author", "").strip(),
                "year": e.get("year", "").strip(),
                "doi": e.get("doi", "").strip(),
                "raw": e,
            }
        )
    return entries
```

### Task 3.2 — Port the deterministic resolution cascade from academic-writing

- [ ] Add `_resolve` to `citation_verifier.py`. Reuse the `academic-writing` cascade logic (DOI→Crossref→arXiv→Semantic Scholar). Each stage returns a normalized record `{title, author, year, doi, source, bibtex}` or `None`.

```python
from habanero import Crossref
from semanticscholar import SemanticScholar

_cr = Crossref()
_s2 = SemanticScholar()


def _from_crossref_item(item: dict, source: str) -> dict:
    authors = item.get("author", []) or []
    author = " and ".join(
        f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in authors
    )
    title = (item.get("title") or [""])[0]
    issued = item.get("issued", {}).get("date-parts", [[None]])
    year = str(issued[0][0]) if issued and issued[0] and issued[0][0] else ""
    doi = item.get("DOI", "")
    return {"title": title, "author": author, "year": year, "doi": doi, "source": source}


def _resolve(entry: dict) -> dict | None:
    # 1. DOI → Crossref (authoritative)
    if entry["doi"]:
        try:
            item = _cr.works(ids=entry["doi"]).get("message")
            if item:
                return _from_crossref_item(item, "crossref")
        except Exception as exc:  # noqa: BLE001
            log.warning("crossref DOI lookup failed: %s", exc)
    # 2. Title → Crossref
    if entry["title"]:
        try:
            res = _cr.works(query_bibliographic=entry["title"], limit=1)
            items = res.get("message", {}).get("items", [])
            if items:
                return _from_crossref_item(items[0], "crossref")
        except Exception as exc:  # noqa: BLE001
            log.warning("crossref title lookup failed: %s", exc)
    # 3. arXiv id embedded in DOI/title (deterministic prefix check) — optional stage
    # 4. Title → Semantic Scholar
    if entry["title"]:
        try:
            results = _s2.search_paper(entry["title"], limit=1)
            if results:
                p = results[0]
                author = " and ".join(a.name for a in (p.authors or []))
                return {
                    "title": p.title or "",
                    "author": author,
                    "year": str(p.year or ""),
                    "doi": (p.externalIds or {}).get("DOI", "") or "",
                    "source": "semantic_scholar",
                }
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic scholar lookup failed: %s", exc)
    return None
```

> **Note for the implementing agent:** the `academic-writing` skill already contains
> this cascade. Prefer importing/sharing that module over copy-paste if it is exposed
> as an importable function; the code above is the fallback shape if it is not.

### Task 3.3 — Implement field-level comparison and classification

- [ ] Add `_classify` to `citation_verifier.py`. Compare the entry's fields against the resolved record and decide exact/minor/major.

```python
def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _last_names(author: str) -> set[str]:
    names = set()
    for part in author.replace(" and ", ";").replace(",", ";").split(";"):
        tok = part.strip().split()
        if tok:
            names.add(tok[-1].lower() if "," not in part else tok[0].lower())
    return {n for n in names if n}


def _classify(entry: dict, resolved: dict | None) -> CitationCheckResult:
    if resolved is None:
        return CitationCheckResult(
            entry_id=entry["id"], verdict="major_hallucination",
            field_errors=["entry not found in any source"], source=None,
        )

    field_errors: list[str] = []
    if entry["title"] and _norm(entry["title"]) != _norm(resolved["title"]):
        # title mismatch is severe — likely a different paper
        from difflib import SequenceMatcher
        ratio = SequenceMatcher(None, _norm(entry["title"]), _norm(resolved["title"])).ratio()
        if ratio < 0.6:
            return CitationCheckResult(
                entry_id=entry["id"], verdict="major_hallucination",
                field_errors=["title"], source=resolved["source"],
            )
        field_errors.append("title")

    if entry["year"] and resolved["year"] and entry["year"] != resolved["year"]:
        field_errors.append("year")
    if entry["author"] and resolved["author"]:
        if not (_last_names(entry["author"]) & _last_names(resolved["author"])):
            field_errors.append("author")
    if entry["doi"] and resolved["doi"] and _norm(entry["doi"]) != _norm(resolved["doi"]):
        field_errors.append("doi")

    verdict = "exact_match" if not field_errors else "minor_hallucination"
    return CitationCheckResult(
        entry_id=entry["id"], verdict=verdict, field_errors=field_errors,
        source=resolved["source"], normalized_bibtex=_to_bibtex(entry["id"], resolved),
    )


def _to_bibtex(entry_id: str, r: dict) -> str:
    return (
        f"@article{{{entry_id},\n"
        f"  title = {{{r['title']}}},\n"
        f"  author = {{{r['author']}}},\n"
        f"  year = {{{r['year']}}},\n"
        f"  doi = {{{r['doi']}}}\n}}"
    )
```

### Task 3.4 — Implement the public `verify_citations` entry point

- [ ] Add `verify_citations` to `citation_verifier.py`. Parse each bibliography string, resolve, classify.

```python
def verify_citations(bibliography: list[str]) -> list[CitationCheckResult]:
    results: list[CitationCheckResult] = []
    for bib_str in bibliography:
        for entry in _parse_entries(bib_str):
            resolved = _resolve(entry)
            results.append(_classify(entry, resolved))
    return results
```

---

## Phase 4 — MCP server

### Task 4.1 — Write `server.py` exposing the two tools over stdio

- [ ] Create `services/skills/citation-check/server.py`. Register both tools, serialize Pydantic results to JSON strings, run over stdio. stdout is JSON-RPC only.

```python
"""citation-check MCP server — exposes verify_claims and verify_citations."""
from __future__ import annotations

import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

import claim_verifier
import citation_verifier
from models import ClaimVerificationResult

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("citation-check.server")

app = Server("citation-check")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="citation_check.verify_claims",
            description="Decompose text into claim-triplets and verify each against references.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "references": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "references"],
            },
        ),
        Tool(
            name="citation_check.verify_citations",
            description="Verify BibTeX entries against Crossref/Semantic Scholar.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bibliography": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["bibliography"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "citation_check.verify_claims":
        result = claim_verifier.verify_claims(
            arguments["text"], arguments.get("references", [])
        )
        return [TextContent(type="text", text=result.model_dump_json())]
    if name == "citation_check.verify_citations":
        results = citation_verifier.verify_citations(arguments["bibliography"])
        payload = json.dumps([r.model_dump() for r in results])
        return [TextContent(type="text", text=payload)]
    raise ValueError(f"unknown tool: {name}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
```

> **Note:** `asyncio.run` is at module top-level in `__main__` only — never inside an
> already-running event loop (project rule).

---

## Phase 5 — Tests (all `@pytest.mark.mocked`)

### Task 5.1 — Write `conftest.py` with fixtures and mocks

- [ ] Create `tests/services/skills/citation-check/conftest.py`. Add a fixture that patches the Gemma call and a fixture for fake Crossref/S2 responses.

```python
import sys
from pathlib import Path

import pytest

# Make the skill modules importable (they use flat imports).
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "services" / "skills" / "citation-check"))


@pytest.fixture
def patch_gemma(monkeypatch):
    """Patch claim_verifier._call_gemma to return scripted outputs."""
    import claim_verifier

    calls = {"responses": []}

    def fake(prompt: str) -> str:
        return calls["responses"].pop(0)

    monkeypatch.setattr(claim_verifier, "_call_gemma", fake)
    return calls
```

### Task 5.2 — `test_claim_verifier.py`: counts and contradiction

- [ ] Create `tests/services/skills/citation-check/test_claim_verifier.py`.

```python
import pytest

import claim_verifier
from models import ClaimVerificationResult


@pytest.mark.mocked
def test_verify_claims_counts(patch_gemma):
    # 1st call = extraction; next 2 = classification of each triplet.
    patch_gemma["responses"] = [
        '[{"subject":"BERT","predicate":"introduced by","object":"Devlin 2018"},'
        ' {"subject":"BERT","predicate":"is a","object":"RNN"}]',
        '{"verdict":"entailed","evidence":"Devlin et al. 2018 introduced BERT"}',
        '{"verdict":"contradicted","evidence":"BERT is a Transformer, not an RNN"}',
    ]
    result = claim_verifier.verify_claims(
        "BERT was introduced by Devlin 2018 and is an RNN.",
        ["Devlin et al. 2018 introduced BERT, a Transformer encoder."],
    )
    assert isinstance(result, ClaimVerificationResult)
    assert len(result.triplets) == 2
    assert result.entailed_count == 1
    assert result.contradicted_count == 1
    assert result.unverifiable_count == 0


@pytest.mark.mocked
def test_contradicted_verdict_surfaces_evidence(patch_gemma):
    patch_gemma["responses"] = [
        '[{"subject":"X","predicate":"equals","object":"5"}]',
        '{"verdict":"contradicted","evidence":"X equals 7"}',
    ]
    result = claim_verifier.verify_claims("X equals 5.", ["The paper states X equals 7."])
    t = result.triplets[0]
    assert t.verdict == "contradicted"
    assert t.evidence == "X equals 7"


@pytest.mark.mocked
def test_no_references_yields_unverifiable(patch_gemma):
    patch_gemma["responses"] = ['[{"subject":"A","predicate":"is","object":"B"}]']
    result = claim_verifier.verify_claims("A is B.", [])
    assert result.unverifiable_count == 1
    assert result.entailed_count == 0
```

### Task 5.3 — `test_citation_verifier.py`: exact / major / minor

- [ ] Create `tests/services/skills/citation-check/test_citation_verifier.py`. Patch `_resolve` to control the cascade output.

```python
import pytest

import citation_verifier

VALID_BIB = """@article{devlin2018bert,
  title = {BERT: Pre-training of Deep Bidirectional Transformers},
  author = {Devlin, Jacob and Chang, Ming-Wei},
  year = {2018},
  doi = {10.18653/v1/N19-1423}
}"""

FAKE_BIB = """@article{fake2099,
  title = {Quantum Telepathy in Large Language Models},
  author = {Nobody, A.},
  year = {2099}
}"""

WRONG_AUTHOR_BIB = """@article{devlin2018bert,
  title = {BERT: Pre-training of Deep Bidirectional Transformers},
  author = {Wrongname, Q.},
  year = {2018},
  doi = {10.18653/v1/N19-1423}
}"""


@pytest.mark.mocked
def test_exact_match_for_valid_doi(monkeypatch):
    monkeypatch.setattr(citation_verifier, "_resolve", lambda e: {
        "title": "BERT: Pre-training of Deep Bidirectional Transformers",
        "author": "Devlin, Jacob and Chang, Ming-Wei",
        "year": "2018", "doi": "10.18653/v1/N19-1423", "source": "crossref",
    })
    results = citation_verifier.verify_citations([VALID_BIB])
    assert results[0].verdict == "exact_match"
    assert results[0].field_errors == []
    assert results[0].source == "crossref"


@pytest.mark.mocked
def test_major_hallucination_for_fabricated_paper(monkeypatch):
    monkeypatch.setattr(citation_verifier, "_resolve", lambda e: None)
    results = citation_verifier.verify_citations([FAKE_BIB])
    assert results[0].verdict == "major_hallucination"
    assert results[0].source is None


@pytest.mark.mocked
def test_minor_hallucination_wrong_author(monkeypatch):
    monkeypatch.setattr(citation_verifier, "_resolve", lambda e: {
        "title": "BERT: Pre-training of Deep Bidirectional Transformers",
        "author": "Devlin, Jacob and Chang, Ming-Wei",
        "year": "2018", "doi": "10.18653/v1/N19-1423", "source": "crossref",
    })
    results = citation_verifier.verify_citations([WRONG_AUTHOR_BIB])
    assert results[0].verdict == "minor_hallucination"
    assert "author" in results[0].field_errors
    assert results[0].normalized_bibtex is not None
```

### Task 5.4 — Run the suite

- [ ] Run the mocked tests and confirm green.

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skills/citation-check/ -m mocked -v
```

---

## Phase 6 — Wiring & verification

### Task 6.1 — Register the skill with the MCP bridge

- [ ] Add `citation-check` to the bridge's skill registry (per `spec_mcp_bridge.md` / `spec_skills.md`) so `services/skills/citation-check/server.py` is spawned as a child-process MCP server. Confirm the two tools appear in the bridge's aggregated `list_tools`.

### Task 6.2 — Smoke-test the server over stdio

- [ ] Start the server standalone and confirm it speaks JSON-RPC on stdout and logs to stderr only.

```bash
cd /Users/zachstallbohm/Work/gemma/services/skills/citation-check
python server.py   # send an initialize request; confirm no stray prints on stdout
```

### Task 6.3 — Verify integration points

- [ ] Confirm `verify_claims` is callable from the `critique` Reflexion loop and `verify_citations` from `academic-writing` (per their specs). Document the tool names in both consuming skills' READMEs if present.

---

## Done criteria

- [ ] `services/skills/citation-check/` contains `server.py`, `claim_verifier.py`, `citation_verifier.py`, `models.py`, `SKILL.md`, `requirements.txt`.
- [ ] `python -m pytest tests/services/skills/citation-check/ -m mocked` is green.
- [ ] No `print()` anywhere; all diagnostics go to stderr.
- [ ] No `tiktoken` import; Gemma calls go through `GEMMA_BASE`.
- [ ] Both tools registered and visible via the MCP bridge.
