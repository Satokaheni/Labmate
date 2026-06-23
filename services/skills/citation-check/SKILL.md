---
name: citation-check
description: >
  Verifies claims and citations against external evidence. verify_claims decomposes
  LLM text into claim-triplets and checks each against supplied references (entailed/
  contradicted/unverifiable). verify_citations checks BibTeX entries against
  Semantic Scholar/Crossref (exact/minor/major hallucination). Use as the grounding
  layer in any critique or academic writing workflow. Exposed as verify_claims and
  verify_citations tools.
trigger: "Use when verifying factual claims or bibliography entries in generated text"
tools:
  - verify_claims
  - verify_citations
version: "0.1.0"
license: MIT
requires: []
---

# Citation Check Skill

External grounding layer for the `critique` Reflexion loop and citation-validation
supplement for `academic-writing`. Two complementary verification tools:

- **`verify_claims(text, references)`** — RefChecker (arXiv:2405.14486).
  Decomposes `text` into (subject, predicate, object) claim-triplets via a local Gemma
  call, then classifies each triplet against the supplied `references` as
  **entailed**, **contradicted**, or **unverifiable**. Returns JSON with per-claim
  verdicts and aggregate counts.

- **`verify_citations(bibliography)`** — CiteCheck (arXiv:2605.27700).
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
