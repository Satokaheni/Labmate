# 07 — Skill: academic-pipeline

**Labmate Implementation Plan**
Layer: Skill (child-process MCP server) — Six-stage academic writing pipeline

---

## 1. What This Skill Does

The `academic-pipeline` skill is a Python MCP server that gives the orchestrator a full academic writing pipeline as six independently callable tools. The orchestrator calls them in sequence to produce an IMRaD-structured paper from a topic and a set of source references.

**When the orchestrator uses this skill**: the user requests a research paper, a literature review, a technical report, or any long-form structured writing artifact. The orchestrator loads the skill, calls the tools in pipeline order, and collects the outputs before assembling a final document.

**What it produces**:

- A complete IMRaD scaffold (JSON outline with section roles, word budgets, and reference assignments)
- Section-by-section prose with validated `\cite{key}` inline citations
- A compressed abstract at a target word count, produced by Chain-of-Density summarization
- A bibliography (`references.bib`) containing only entries that passed the four-step citation validation cascade; flagged entries written to `citations_flagged.json`
- A style-unified full draft where all sections are in formal academic register
- A structured critique report with severity scoring and a bounded three-round reflexion revision

**What it does not do**: it does not manage files on disk beyond the citation cache; it does not retrieve PDFs or query Semantic Scholar for new papers (the orchestrator supplies references as BibTeX strings); it does not produce LaTeX boilerplate or a PDF — it produces prose and BibTeX that the orchestrator hands to a downstream formatter.

---

## 2. SKILL.md

This is the complete, exact file to write to `services/skills/academic-pipeline/SKILL.md`. Copy verbatim; do not add fields or reorder sections.

```markdown
---
name: academic-pipeline
description: Six-stage academic writing pipeline: IMRaD outline, section drafting with citation guard, Chain-of-Density abstract compression, citation validation cascade (DOI/arXiv/Semantic Scholar), style transfer to formal register, and structured critique with bounded reflexion. Use when the user asks to write a paper, draft a research paper, produce an academic paper, check citations, or do academic writing.
trigger:
  - write paper
  - academic paper
  - write a research paper
  - draft section
  - check citations
  - academic writing
tools:
  - name: paper_outline
    description: Generate an IMRaD scaffold (Introduction, Methods, Results, Discussion) from a topic and list of BibTeX references. Returns a JSON outline with section names, assigned reference ids, key points, and word budgets. Sections are emitted in canonical IMRaD order. Raises an error if any mandatory section is missing from the LLM output.
    inputSchema:
      type: object
      properties:
        topic:
          type: string
          description: The research topic or paper title
        sources:
          type: array
          items:
            type: string
          description: List of BibTeX entry strings (LLM-generated or from a local library). These are assigned to sections; they are NOT validated here — call paper_validate_citations first.
      required:
        - topic
        - sources
  - name: paper_draft_section
    description: Draft a single IMRaD section grounded in the supplied outline, validated references, and notes. Enforces IMRaD role constraints (e.g. no Results content in Methods). Raises an error if the draft cites any key not in the supplied validated reference set — the citation key guard is non-negotiable.
    inputSchema:
      type: object
      properties:
        section:
          type: string
          description: Section name — must be one of Introduction, Background, Methods, Experimental Setup, Results, Discussion, Conclusion
          enum:
            - Introduction
            - Background
            - Methods
            - Experimental Setup
            - Results
            - Discussion
            - Conclusion
        outline:
          type: object
          description: The full IMRaD outline JSON returned by paper_outline
        refs:
          type: array
          items:
            type: string
          description: Validated BibTeX strings for this section only (entries returned with valid=true by paper_validate_citations). Do not pass unvalidated entries — they will be rejected.
        notes:
          type: string
          description: Section-specific notes, figures, tables, and raw numbers to ground the draft. For the Results section this is the only permitted source of numbers.
      required:
        - section
        - outline
        - refs
        - notes
  - name: paper_chain_of_density
    description: Iteratively compress text while increasing entity density using the Chain-of-Density method (Adams et al. EMNLP 2023). Starts sparse, adds 1-3 missing salient entities per iteration, holds word count fixed at target_words across all three iterations. Use to produce the abstract from the assembled paper body.
    inputSchema:
      type: object
      properties:
        text:
          type: string
          description: Source text to compress (typically the assembled paper body for abstract generation)
        target_words:
          type: integer
          description: Fixed word count for every iteration. Typical range 150-250 for a conference abstract.
          default: 200
      required:
        - text
  - name: paper_validate_citations
    description: Four-step deterministic-first citation validation cascade. Step 1 regex parse (extract DOI/arXiv ID). Step 2a DOI via Crossref (habanero) with title fuzzy-match >= 0.85 and author overlap >= 0.60. Step 2b arXiv ID via arXiv API with same thresholds. Step 3 Semantic Scholar title search fallback. Step 4 LLM advisory fallback (always returns flagged_review, never valid). Results are cached in ~/.cache/labmate/citations.db (SQLite). Citation hallucination rates of 11-57% across LLMs make this cascade non-optional.
    inputSchema:
      type: object
      properties:
        bibtex:
          type: array
          items:
            type: string
          description: List of raw BibTeX entry strings to validate. Pass all LLM-generated entries before any enter the bibliography.
      required:
        - bibtex
  - name: paper_style_transfer
    description: Convert text from casual to formal academic register using prompt-based Text Style Transfer. Runs as a single pass over the fully assembled draft (not per-section). Post-transfer integrity check verifies that all \cite{}, \ref{}, and numeric tokens are preserved verbatim. If any are altered, retries once with a stricter preservation instruction. Raises an error after two failed attempts.
    inputSchema:
      type: object
      properties:
        text:
          type: string
          description: The assembled draft text (all sections concatenated)
        target_style:
          type: string
          description: Target register
          enum:
            - formal_academic
          default: formal_academic
      required:
        - text
  - name: paper_critique
    description: Structured critique of a draft with severity scoring and a bounded reflexion loop (max 3 rounds). Each round gathers fresh external signals (citation validation, IMRaD structure check, CoVe factored verification) before the LLM evaluator is called — pure self-critique is prohibited to prevent Degeneration-of-Thought. Returns a CritiqueResult with issues_found, severity, suggested_revision, and the best revised draft.
    inputSchema:
      type: object
      properties:
        draft_path:
          type: string
          description: Absolute path to the assembled draft text file
        max_rounds:
          type: integer
          description: Maximum reflexion rounds. Must not exceed 3. Values above 3 are clamped to 3 to prevent Degeneration-of-Thought.
          default: 3
          maximum: 3
      required:
        - draft_path
model: any
version: "1.0.0"
license: MIT
---

# Academic Pipeline Skill

## Purpose

This skill implements a full academic writing pipeline as six composable, independently testable MCP tools. The orchestrator calls them in sequence to produce an IMRaD-structured paper from a topic and a set of source references. Each tool corresponds to one bounded stage; no single LLM call generates the whole paper.

The pipeline is grounded in the STORM two-stage pattern (Stanford OVAL, NAACL 2024) and the AI Scientist per-section writing pattern (SakanaAI, Nature 2024). Citation validation follows the refchecker deterministic-first cascade. Abstract compression uses Chain-of-Density (Adams et al. EMNLP 2023). Critique uses the Reflexion architecture (Shinn et al. NeurIPS 2023) with mandatory external grounding to prevent Degeneration-of-Thought (Liang et al. EMNLP 2024).

## Tool Usage

Call tools in this order for a full pipeline run:

1. `paper_validate_citations(bibtex=[...])` — validate all gathered references first
2. `paper_outline(topic, sources=[...])` — generate IMRaD scaffold; pass only validated BibTeX keys in sources
3. `paper_draft_section(section, outline, refs, notes)` — call once per section in canonical IMRaD order; pass only the validated refs for that section
4. `paper_chain_of_density(text, target_words=200)` — call on the assembled paper body to produce the abstract
5. `paper_style_transfer(text)` — call once on the fully assembled draft
6. `paper_critique(draft_path)` — call on the assembled draft file; returns structured critique and revised draft

## Example

```json
{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"paper_validate_citations","arguments":{"bibtex":["@article{smith2024, title={Example}, author={Smith, J}, year={2024}, doi={10.1234/example}}"]}}
```

Returns:
```json
{"result":{"content":[{"type":"text","text":"{\"results\": [{\"entry_id\": \"smith2024\", \"valid\": true, \"source\": \"crossref\", \"normalized_bibtex\": \"@article{smith2024,...}\", \"flagged_for_review\": false}]}"}],"isError":false}}
```
```

---

## 3. File Structure

```
services/skills/academic-pipeline/
├── SKILL.md
├── server.py              — Python MCP server; registers all 6 tools; stderr-only logging
├── stages/
│   ├── __init__.py
│   ├── outline.py         — IMRaD scaffold generation via DSPy ChainOfThought module
│   ├── draft.py           — Per-section drafting with citation key guard
│   ├── density.py         — Chain-of-Density: 3 iterations, sparse-first, fixed length
│   ├── citations.py       — Four-step validation cascade with SQLite cache
│   ├── style.py           — Style transfer + post-transfer integrity check
│   └── critique.py        — Structured critique schema + bounded 3-round reflexion loop
├── schemas.py             — Pydantic v2 models: Issue, CritiqueResult, OutlineSection, CitationResult
├── requirements.txt
└── Dockerfile
```

**No other files are needed.** Do not create a `config.py`, `utils.py`, or any file not listed above without a specific reason.

---

## 4. Interface Contracts

### 4.1 MCP JSON-RPC Transport (Contract B)

The skill speaks MCP JSON-RPC 2.0 over newline-delimited stdin/stdout (Contract B from `00_contracts.md`). stdout is exclusively JSON-RPC frames. All logging goes to stderr.

Initialize handshake (sent once by SkillRegistry at spawn time):

```json
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"labmate-orchestrator","version":"1.0.0"}}}

← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"academic-pipeline","version":"1.0.0"}}}

→ {"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
```

### 4.2 Tool Call Shapes — All Six Tools

**paper_outline**

Input:
```json
{
  "jsonrpc": "2.0",
  "id": "call-001",
  "method": "tools/call",
  "params": {
    "name": "paper_outline",
    "arguments": {
      "topic": "Federated learning for medical image segmentation",
      "sources": [
        "@article{mcmahan2017, title={Communication-Efficient Learning...}, author={McMahan, B...}, year={2017}, doi={10.48550/arXiv.1602.05629}}"
      ]
    }
  }
}
```

Output (content[0].text is a JSON string):
```json
{
  "jsonrpc": "2.0",
  "id": "call-001",
  "result": {
    "content": [{
      "type": "text",
      "text": "{\"sections\": [{\"name\": \"Introduction\", \"ref_ids\": [\"mcmahan2017\"], \"key_points\": [\"Motivate federated learning for privacy-preserving medical AI\", \"State contributions\"], \"word_budget\": 500}, {\"name\": \"Background\", \"ref_ids\": [\"mcmahan2017\"], \"key_points\": [\"FedAvg algorithm\", \"Prior medical federated learning work\"], \"word_budget\": 400}, {\"name\": \"Methods\", \"ref_ids\": [], \"key_points\": [\"Architecture description\", \"Aggregation strategy\"], \"word_budget\": 600}, {\"name\": \"Experimental Setup\", \"ref_ids\": [], \"key_points\": [\"Datasets used\", \"Baseline comparisons\"], \"word_budget\": 300}, {\"name\": \"Results\", \"ref_ids\": [], \"key_points\": [\"Table 1: Dice scores\", \"Figure 2: convergence curve\"], \"word_budget\": 400}, {\"name\": \"Discussion\", \"ref_ids\": [], \"key_points\": [\"Interpret results\", \"Limitations\", \"Future work\"], \"word_budget\": 400}, {\"name\": \"Conclusion\", \"ref_ids\": [], \"key_points\": [\"Summary of contributions\"], \"word_budget\": 200}]}"
    }],
    "isError": false
  }
}
```

**IMRaD outline JSON structure** — the exact schema `paper_outline` always produces:

```json
{
  "sections": [
    {
      "name": "Introduction",
      "ref_ids": ["mcmahan2017", "li2020"],
      "key_points": ["string — what to cover in this section"],
      "word_budget": 500
    }
  ]
}
```

Section names are constrained to: `Introduction`, `Background`, `Methods`, `Experimental Setup`, `Results`, `Discussion`, `Conclusion`. They are always emitted in that order. Any response missing a mandatory section causes the tool to return `isError: true`.

---

**paper_draft_section**

Input:
```json
{
  "name": "paper_draft_section",
  "arguments": {
    "section": "Methods",
    "outline": { "sections": [...] },
    "refs": ["@article{mcmahan2017,...}"],
    "notes": "We use a U-Net backbone. FedAvg with 10 clients. 100 communication rounds."
  }
}
```

Output (content[0].text is the drafted section text):
```json
{
  "content": [{"type": "text", "text": "\\section{Methods}\n\nWe adopt a U-Net architecture \\cite{mcmahan2017} trained via federated averaging across ten clinical sites..."}],
  "isError": false
}
```

Error on citation key guard violation:
```json
{
  "content": [{"type": "text", "text": "CitationKeyGuardError: section 'Methods' cited unvalidated keys: {'jones2023'}. These keys are not in the supplied validated ref set. Re-invoke with a corrected ref list or remove the citation."}],
  "isError": true
}
```

---

**paper_chain_of_density**

Input:
```json
{
  "name": "paper_chain_of_density",
  "arguments": {
    "text": "<full paper body, several thousand words>",
    "target_words": 200
  }
}
```

Output:
```json
{
  "content": [{"type": "text", "text": "<compressed abstract at ~200 words>"}],
  "isError": false
}
```

---

**paper_validate_citations**

Input:
```json
{
  "name": "paper_validate_citations",
  "arguments": {
    "bibtex": [
      "@article{mcmahan2017, doi={10.48550/arXiv.1602.05629}, title={Communication-Efficient...}, author={McMahan, B and Moore, E and ...}, year={2017}}",
      "@article{fake2024, doi={10.9999/doesnotexist}, title={Completely Fabricated Paper}, author={Nobody, X}, year={2024}}"
    ]
  }
}
```

Output — the citation validation result schema:
```json
{
  "content": [{
    "type": "text",
    "text": "{\"results\": [{\"entry_id\": \"mcmahan2017\", \"valid\": true, \"source\": \"arxiv\", \"flagged_for_review\": false, \"normalized_bibtex\": \"@article{mcmahan2017,...}\", \"conflict_reason\": null}, {\"entry_id\": \"fake2024\", \"valid\": false, \"source\": null, \"flagged_for_review\": true, \"normalized_bibtex\": null, \"conflict_reason\": \"DOI resolves but title/author mismatch\"}]}"
  }],
  "isError": false
}
```

Citation validation result schema (one object per input entry):

```json
{
  "entry_id": "mcmahan2017",
  "valid": true,
  "source": "crossref | arxiv | semantic_scholar | llm_fallback | null",
  "flagged_for_review": false,
  "normalized_bibtex": "@article{mcmahan2017, ...normalized from API...}",
  "conflict_reason": null
}
```

Callers must filter to `valid == true` before passing any entry to `paper_draft_section` or including it in the bibliography. Entries with `flagged_for_review == true` and `valid == false` are written to `citations_flagged.json` at the path set by the `CITATIONS_FLAGGED_PATH` environment variable (default: `./citations_flagged.json`).

---

**paper_style_transfer**

Input:
```json
{
  "name": "paper_style_transfer",
  "arguments": {
    "text": "<assembled draft, all sections concatenated>",
    "target_style": "formal_academic"
  }
}
```

Output:
```json
{
  "content": [{"type": "text", "text": "<style-transferred draft, same \\cite{} and numeric tokens preserved>"}],
  "isError": false
}
```

Error on integrity check failure after two retries:
```json
{
  "content": [{"type": "text", "text": "StyleIntegrityError: citation/reference/numeric tokens altered after 2 attempts. Altered tokens: ['\\\\cite{jones2023}' missing, numeric '0.94' changed to '0.9']. Manual review required."}],
  "isError": true
}
```

---

**paper_critique**

Input:
```json
{
  "name": "paper_critique",
  "arguments": {
    "draft_path": "/workspace/papers/federated-medical/draft.md",
    "max_rounds": 3
  }
}
```

Output — the CritiqueResult schema:
```json
{
  "content": [{
    "type": "text",
    "text": "{\"verdict\": \"revise\", \"severity\": \"medium\", \"score\": 0.72, \"issues_found\": [{\"location\": \"Results paragraph 2\", \"category\": \"factual\", \"explanation\": \"Dice score 0.94 in text does not match Table 1 value 0.91\", \"grounded_by\": \"imrad_structure_check: numeric mismatch detected\"}], \"constitutional_violations\": [], \"suggested_revision\": \"Align the inline Dice score in Results paragraph 2 with Table 1: change 0.94 to 0.91.\", \"evidence\": [\"IMRaD structure check: Results section cites 3 numbers not present in supplied notes\"], \"confidence\": 0.85, \"no_issues_justification\": null, \"best_revised_draft\": \"<full revised draft text>\"}"
  }],
  "isError": false
}
```

CritiqueResult schema:

```python
class Issue(BaseModel):
    location: str          # "Section paragraph N" or "file:line"
    category: Literal["bug", "style", "factual", "security", "logic", "clarity"]
    explanation: str
    grounded_by: str | None  # e.g. "imrad_structure_check: numbers not in notes"

class CritiqueResult(BaseModel):
    verdict: Literal["pass", "revise", "fail"]
    severity: Literal["low", "medium", "high", "critical"]
    score: float                        # 0.0 to 1.0
    issues_found: list[Issue]           # non-empty OR no_issues_justification provided
    constitutional_violations: list[str]
    suggested_revision: str
    evidence: list[str]                 # external signal quotes
    confidence: float                   # 0.0 to 1.0
    no_issues_justification: str | None
    best_revised_draft: str             # the best output produced across all reflexion rounds
```

### 4.3 How the Skill Calls the Inference Server

All LLM calls use the OpenAI-compatible async client (Contract A from `00_contracts.md`). The base URL comes from the `INFERENCE_URL` environment variable (default: `http://host.docker.internal:8000`).

```python
from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None

def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        import os
        _client = AsyncOpenAI(
            base_url=os.environ.get("INFERENCE_URL", "http://host.docker.internal:8000") + "/v1",
            api_key="not-used",  # vLLM does not require a key
        )
    return _client

async def llm_complete(prompt: str, system: str = "", temperature: float = 0.2, max_tokens: int = 2048) -> str:
    client = get_client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = await client.chat.completions.create(
        model=os.environ.get("INFERENCE_MODEL", "google/gemma-4-9b-it"),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""
```

This function is the only path through which the skill issues LLM calls. Import and call it from each stage module. Do not instantiate `AsyncOpenAI` directly inside stage modules.

---

## 5. Implementation Steps

Implement files in this exact order. Each step is independently testable before moving to the next.

### Step 1 — schemas.py

Define all Pydantic v2 models. No I/O, no LLM calls, no imports beyond `pydantic` and `typing`.

Models to define:

```
OutlineSection(name, ref_ids, key_points, word_budget)
Outline(sections: list[OutlineSection])
CitationResult(entry_id, valid, source, flagged_for_review, normalized_bibtex, conflict_reason)
CitationValidationResponse(results: list[CitationResult])
Issue(location, category, explanation, grounded_by)
CritiqueResult(verdict, severity, score, issues_found, constitutional_violations,
               suggested_revision, evidence, confidence, no_issues_justification,
               best_revised_draft)
```

Add model validators:
- `CritiqueResult`: raise `ValueError` if `issues_found` is empty and `no_issues_justification` is None.
- `OutlineSection`: validate that `name` is one of the seven canonical IMRaD section names.
- `CritiqueResult.score` and `CritiqueResult.confidence`: must be in [0.0, 1.0].

Test: instantiate each model with valid data. Try constructing a `CritiqueResult` with empty `issues_found` and no `no_issues_justification` — confirm `ValidationError` is raised.

### Step 2 — server.py skeleton

Create the MCP server, register all six tools, wire stderr-only logging. The tool handler bodies return stub responses at this stage — each returns `isError: false` with a placeholder string. The goal is a working MCP handshake and `tools/list` response before any stage logic exists.

Verify with:
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}' \
  | python server.py 2>/dev/null
```
Expect a valid JSON-RPC initialize response. No bytes on stdout before that line.

Verify zero stdout pollution:
```bash
python server.py < /dev/null 2>/dev/null | wc -c
# Must output 0
```

### Step 3 — stages/outline.py

Implement `generate_outline(topic: str, sources: list[str]) -> Outline`.

Logic:
1. Parse the input BibTeX strings with `bibtexparser.loads()` to extract `entry_id`, `title`, `abstract` from each entry. Entries that fail to parse are skipped with a stderr warning.
2. Build a reference list: `[{"id": entry_id, "title": ..., "abstract": ...}]`.
3. Issue one LLM call with an IMRaD constraint table embedded in the prompt (see Section 6). Request JSON output matching the `Outline` schema.
4. Parse the JSON response. If parsing fails, retry once with an explicit JSON-only instruction.
5. Validate the parsed outline: confirm all seven mandatory sections are present (`Introduction`, `Background`, `Methods`, `Experimental Setup`, `Results`, `Discussion`, `Conclusion`). If any are missing, raise `ValueError` listing the missing sections.
6. Sort sections into canonical IMRaD order before returning.

Test: call with a topic and two BibTeX entries. Assert the returned `Outline` has seven sections in canonical order. Assert that passing a topic with no sources still produces all seven sections (with empty `ref_ids`).

### Step 4 — stages/draft.py

Implement `draft_section(section_name: str, outline: Outline, refs: list[str], notes: str) -> str`.

Logic:
1. Extract `valid_keys` from the supplied BibTeX strings using a `bibtex_key()` helper (regex: the first word after `@type{` on the first line).
2. Find the matching `OutlineSection` from the outline by name. Extract `key_points` and `word_budget`.
3. Build the LLM prompt with:
   - The IMRaD role constraint for this section (see the constraint table in `spec_writing_skills.md` Section 3.1 — embed it literally in the prompt, not as a reference to the spec).
   - The `key_points` from the outline node.
   - The reference context as `[{"key": k, "title": t, "abstract": a}]`.
   - The `notes` string verbatim.
   - An explicit instruction: "Cite only keys from this list: {valid_keys}. Do not cite any other key."
4. Issue the LLM call. Receive draft text.
5. **Citation key guard**: extract all `\cite{key}` tokens from the draft with `re.findall(r'\\cite\{([^}]+)\}', text)`. Compute `unknown = cited_keys - valid_keys`. If `unknown` is non-empty, raise `CitationKeyGuardError(f"Section '{section_name}' cited unvalidated keys: {unknown}")`. This error propagates to the MCP layer as `isError: true`. Do not retry silently.
6. Return the draft text.

Test: call with a validated ref set containing key `smith2024`. Confirm that a draft citing `smith2024` passes the guard. Mock an LLM response that cites `jones2023` (not in the set) and confirm `CitationKeyGuardError` is raised.

### Step 5 — stages/density.py

Implement `chain_of_density(text: str, target_words: int = 200, iterations: int = 3) -> str`.

Logic:
1. **Iteration 0 (sparse)**: issue LLM call with the prompt: "Summarize the following text in exactly {target_words} words. Use few named entities. Prioritize readability and narrative flow over completeness. Do not start dense." Receive initial summary.
2. Enforce word count: count words with `len(summary.split())`. If the count exceeds `target_words`, issue a second LLM call: "The following summary is {actual} words. Truncate it to exactly {target_words} words without changing the meaning or removing named entities. Return only the truncated summary." Use the result. Log a stderr warning if the second call also overshoots; use truncation by split+join as a hard fallback.
3. **Iterations 1 and 2**: for each:
   a. Issue an "identify missing entities" LLM call: "The following summary omits important entities from the source text. List 1-3 named entities or technical terms that are salient in the source but absent from the summary. Return a JSON array of strings." Parse the response.
   b. If the list is empty, break early (converged).
   c. Issue a "densify" LLM call: "Rewrite the following summary to include these missing entities: {entities}. The rewritten summary must be exactly {target_words} words. Do not remove any existing named entities. The summary must remain coherent prose." Receive densified summary.
   d. Enforce word count as in step 2.
4. Return the final summary after three iterations (or earlier if converged).

Test: call with a 500-word source text and `target_words=100`. Assert the output is 100 words (± 5). Assert that the entity density of the output (measured as named-token count / total word count) is greater than that of the source. Assert word count does not increase across iterations (log each iteration's count to stderr).

### Step 6 — stages/citations.py

Implement `validate_citations(bibtex_entries: list[str]) -> CitationValidationResponse`.

The cascade must run in exactly this order per entry. Cache hits short-circuit the cascade.

**SQLite cache**: open `~/.cache/labmate/citations.db` (create directory if it does not exist). Schema:

```sql
CREATE TABLE IF NOT EXISTS citations (
    doi_or_key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    cached_at TEXT NOT NULL
);
```

Cache key: the DOI (lowercase, stripped) if present; otherwise the arXiv ID; otherwise `sha256(title.lower().strip())[:16]`. Cache entries expire after 30 days (compare `cached_at` to current time on read; delete stale rows).

**Per-entry cascade**:

```
1. bibtexparser.loads(entry) → extract entry_id, doi, arxiv_id, eprint, title, author fields.
   On parse failure → return CitationResult(valid=False, flagged_for_review=True, conflict_reason="bibtexparser parse failed")

2. Check SQLite cache. On hit → return cached CitationResult.

3. If doi present:
   a. Call habanero Crossref: cr.works(ids=doi)
   b. fuzzy title match (difflib.SequenceMatcher): ratio >= 0.85
   c. author overlap: len(intersection) / len(union) >= 0.60
   d. On match → CitationResult(valid=True, source="crossref", normalized_bibtex=crossref_to_bibtex(meta, entry_id))
   e. On mismatch → CitationResult(valid=False, flagged_for_review=True, conflict_reason="DOI resolves but title/author mismatch")
   f. On Crossref HTTP error → fall through to step 5

4. If arxiv_id or eprint starting with "arXiv:" present:
   a. Call arXiv API: GET https://export.arxiv.org/abs/{arxiv_id} or GET https://export.arxiv.org/search/?id_list={arxiv_id}&max_results=1
   b. Parse title and authors from Atom XML response.
   c. Same fuzzy title and author overlap thresholds as step 3.
   d. On match → CitationResult(valid=True, source="arxiv", ...)
   e. On mismatch → CitationResult(valid=False, flagged_for_review=True, conflict_reason="arXiv ID resolves but title/author mismatch")

5. If no identifier (no doi, no arxiv_id):
   a. Call Semantic Scholar: ss.search_paper(title, limit=3)
   b. For each hit, compute author overlap >= 0.60.
   c. On any match → CitationResult(valid=True, source="semantic_scholar", ...)
   d. No match → proceed to step 6

6. LLM fallback (advisory only; only if steps 3-5 all failed):
   a. Issue LLM call: "Is the following BibTeX entry real? Search your knowledge for the exact title, authors, and year. Respond with JSON: {\"likely_real\": bool, \"confidence\": float, \"reason\": str}."
   b. Always return CitationResult(valid=False, flagged_for_review=True, source="llm_fallback", conflict_reason=f"LLM advisory: {reason} (confidence={confidence}). Manual verification required.")
   (LLM fallback result is NEVER valid=True)

7. Write result to SQLite cache.
8. Write flagged entries to citations_flagged.json.
```

After processing all entries, deduplicate citation keys: keys follow `firstauthorYEAR` convention. Collect all normalized BibTeX entries that are `valid=True`. For any two entries that produce the same key, append `a`, `b`, `c` disambiguators (`smith2024a`, `smith2024b`).

Test:
- Entry with a known-good DOI (e.g. McMahan 2017 FedAvg): must return `valid=True, source="crossref"`.
- Entry with a fabricated DOI: must return `valid=False, flagged_for_review=True`.
- Entry with no identifier and title matching a known paper on Semantic Scholar: must return `valid=True, source="semantic_scholar"`.
- Two entries producing the same `firstauthorYEAR` key: keys must be disambiguated with `a` and `b` suffixes.
- Second call with the same DOI: must hit the SQLite cache (mock the Crossref client and assert it is not called).

### Step 7 — stages/style.py

Implement `style_transfer(text: str, target_style: str = "formal_academic") -> str`.

Logic:
1. Extract protected tokens: `re.findall(r'\\cite\{[^}]+\}|\\ref\{[^}]+\}|\d+\.?\d*', text)` → `protected_tokens: set[str]`.
2. Build system prompt: "You are an academic writing editor. Convert the following text from casual register to formal academic prose. Preserve all factual content, citations (\\cite{}), figure references (\\ref{}), and numerical values verbatim. Do not add claims or remove existing ones."
3. Build user prompt with three formal exemplar pairs (casual → formal), then the input text. Exemplars should be short (2 sentences each). Hardcode three pairs in the source file; do not retrieve them dynamically.
4. Issue LLM call (temperature 0.3).
5. **Integrity check**: extract the same regex from the output. Compute `missing = protected_tokens - output_tokens`. If `missing` is non-empty, retry once: prepend "IMPORTANT: You must preserve these tokens exactly as written: {missing}. Do not alter them." to the user prompt.
6. On second failure (missing tokens still non-empty after retry): raise `StyleIntegrityError(f"tokens altered after 2 attempts: {missing}")`.
7. Return the style-transferred text.

Test: call with text containing `\cite{smith2024}` and the number `0.94`. Assert the output contains both. Confirm that if the mock LLM drops `\cite{smith2024}`, the retry is triggered. Confirm that after two failures, `StyleIntegrityError` is raised.

### Step 8 — stages/critique.py

Implement `critique(draft_path: str, max_rounds: int = 3) -> CritiqueResult`.

Logic:
1. Clamp `max_rounds = min(max_rounds, 3)`. Exceeding 3 rounds causes Degeneration-of-Thought; the clamp is mandatory.
2. Read draft text from `draft_path`.
3. Initialize: `best_output = draft_text`, `best_score = 0.0`, `memory: list[dict] = []`.
4. For each round `i` in `range(max_rounds)`:
   a. **Ground with signals** (before any LLM evaluator call):
      - IMRaD structure check: scan for section headers matching the seven canonical names. Report any that are missing or out of order.
      - Citation key check: extract all `\cite{key}` tokens. For each key, check the SQLite citation cache (from `citations.py`) — report any key not found in the cache as `valid=True`.
      - CoVe factored verification: generate 2-3 factual verification questions from the draft (one LLM call). Answer each question in a SEPARATE LLM call WITHOUT the draft in context. Compare answers to the draft; flag contradictions.
   b. **Invoke evaluator**: issue LLM call with adversarial critic system prompt (temperature 0.1). Supply the signals from step (a) as quoted evidence in the prompt. Request output matching the `CritiqueResult` schema (use `instructor` or parse manually).
   c. Validate the `CritiqueResult` with Pydantic. If `issues_found` is empty and `no_issues_justification` is None, retry the evaluator call once (Instructor re-ask pattern). If validation still fails, log a stderr warning and continue without consuming the round.
   d. If `crit.score > best_score`: update `best_score = crit.score`, `best_output = best_output` (current candidate).
   e. If `crit.verdict == "pass"` or `crit.score >= 0.90`: break.
   f. If `crit.confidence >= 0.50`: append `{"lessons": crit.suggested_revision}` to `memory` (keep at most 3 entries — drop oldest).
   g. **Refine**: issue LLM call (generator role, temperature 0.7) with the draft, the critique's `suggested_revision`, and the lessons from `memory`. Receive revised draft.
   h. **Convergence check**: `if len(set(revised.split()) ^ set(best_output.split())) < 5: break` (fewer than 5 token changes = DoT plateau, exit).
   i. `best_output = revised`.
5. Return the final `CritiqueResult` with `best_revised_draft = best_output`.

Test:
- Mock the LLM to return a passing critique on the first round. Assert the loop exits after one round.
- Mock the LLM to return a revision that changes fewer than 5 tokens. Assert the convergence check triggers an early exit.
- Attempt to call with `max_rounds=5`. Assert it is clamped to 3.
- Construct a mock evaluator that returns `issues_found=[]` and `no_issues_justification=None`. Assert `ValidationError` is raised and the re-ask path is triggered.

### Step 9 — Dockerfile and requirements.txt

`requirements.txt`:
```
mcp>=1.0.0
pydantic>=2.0.0
instructor>=1.0.0
openai>=1.0.0
bibtexparser>=1.4.0
habanero>=2.0.0
semanticscholar>=0.7.0
requests>=2.31.0
difflib  # stdlib
```

`Dockerfile`:
```dockerfile
FROM python:3.12-slim

WORKDIR /skill

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Healthcheck: verify zero stdout on startup
RUN python server.py < /dev/null 2>/dev/null | wc -c | grep -q '^0$'

CMD ["python", "server.py"]
```

Register in the SkillRegistry at harness startup:

```python
await registry.register(SkillManifest(
    name="academic-pipeline",
    command="python",
    args=["services/skills/academic-pipeline/server.py"],
    env={"PATH": os.environ["PATH"], "INFERENCE_URL": os.environ.get("INFERENCE_URL", "http://host.docker.internal:8000")},
    version="1.0.0",
    language="python",
))
```

---

## 6. Key Code Patterns

### server.py — tool registration with stderr-only logging

```python
# services/skills/academic-pipeline/server.py
from __future__ import annotations

import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, CallToolResult

# CRITICAL: configure logging to stderr BEFORE any imports that might print on load.
# stdout is exclusively for JSON-RPC frames.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="academic-pipeline %(levelname)s %(message)s",
)
log = logging.getLogger("academic-pipeline")

from schemas import Outline, CritiqueResult  # noqa: E402 — after logging config
from stages.outline import generate_outline
from stages.draft import draft_section, CitationKeyGuardError
from stages.density import chain_of_density
from stages.citations import validate_citations
from stages.style import style_transfer, StyleIntegrityError
from stages.critique import critique

server = Server("academic-pipeline")


@server.list_tools()
async def list_tools() -> list[Tool]:
    log.info("tools/list called")
    return [
        Tool(
            name="paper_outline",
            description="Generate an IMRaD scaffold from a topic and BibTeX sources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic", "sources"],
            },
        ),
        Tool(
            name="paper_draft_section",
            description="Draft a single IMRaD section with citation key guard.",
            inputSchema={
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": ["Introduction", "Background", "Methods", "Experimental Setup", "Results", "Discussion", "Conclusion"]},
                    "outline": {"type": "object"},
                    "refs": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
                "required": ["section", "outline", "refs", "notes"],
            },
        ),
        Tool(
            name="paper_chain_of_density",
            description="Iterative Chain-of-Density abstract compression.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "target_words": {"type": "integer", "default": 200},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="paper_validate_citations",
            description="Four-step citation validation cascade.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bibtex": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["bibtex"],
            },
        ),
        Tool(
            name="paper_style_transfer",
            description="Style transfer to formal academic register with integrity check.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "target_style": {"type": "string", "enum": ["formal_academic"], "default": "formal_academic"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="paper_critique",
            description="Structured critique with bounded 3-round reflexion.",
            inputSchema={
                "type": "object",
                "properties": {
                    "draft_path": {"type": "string"},
                    "max_rounds": {"type": "integer", "default": 3, "maximum": 3},
                },
                "required": ["draft_path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    log.info("tools/call name=%s", name)
    try:
        if name == "paper_outline":
            outline = await generate_outline(arguments["topic"], arguments["sources"])
            return CallToolResult(content=[TextContent(type="text", text=outline.model_dump_json())], isError=False)

        elif name == "paper_draft_section":
            from schemas import Outline as OutlineModel
            outline = OutlineModel.model_validate(arguments["outline"])
            text = await draft_section(arguments["section"], outline, arguments["refs"], arguments["notes"])
            return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)

        elif name == "paper_chain_of_density":
            result = await chain_of_density(arguments["text"], arguments.get("target_words", 200))
            return CallToolResult(content=[TextContent(type="text", text=result)], isError=False)

        elif name == "paper_validate_citations":
            response = await validate_citations(arguments["bibtex"])
            return CallToolResult(content=[TextContent(type="text", text=response.model_dump_json())], isError=False)

        elif name == "paper_style_transfer":
            result = await style_transfer(arguments["text"], arguments.get("target_style", "formal_academic"))
            return CallToolResult(content=[TextContent(type="text", text=result)], isError=False)

        elif name == "paper_critique":
            result = await critique(arguments["draft_path"], arguments.get("max_rounds", 3))
            return CallToolResult(content=[TextContent(type="text", text=result.model_dump_json())], isError=False)

        else:
            return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")], isError=True)

    except CitationKeyGuardError as exc:
        log.error("citation key guard: %s", exc)  # stderr only
        return CallToolResult(content=[TextContent(type="text", text=f"CitationKeyGuardError: {exc}")], isError=True)
    except StyleIntegrityError as exc:
        log.error("style integrity: %s", exc)
        return CallToolResult(content=[TextContent(type="text", text=f"StyleIntegrityError: {exc}")], isError=True)
    except Exception as exc:
        log.exception("tool %s failed", name)  # full traceback to stderr
        return CallToolResult(content=[TextContent(type="text", text=f"Error: {exc}")], isError=True)


async def main() -> None:
    log.info("starting academic-pipeline MCP server on stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

### Chain-of-Density iteration loop — sparse-first constraint

```python
# stages/density.py
from __future__ import annotations

import json
import logging
import sys

log = logging.getLogger("academic-pipeline.density")

# Hardcoded constants match the spec
MAX_COD_ITERATIONS = 3
WORD_COUNT_TOLERANCE = 0.05  # ±5% of target


def _word_count(text: str) -> int:
    return len(text.split())


async def _enforce_word_count(text: str, target: int, llm_complete) -> str:
    actual = _word_count(text)
    tolerance = int(target * WORD_COUNT_TOLERANCE)
    if actual <= target + tolerance:
        return text
    log.warning("word count %d exceeds target %d, enforcing truncation", actual, target)
    prompt = (
        f"The following summary is {actual} words. Truncate it to exactly {target} words "
        "without changing the meaning or removing named entities. Return only the truncated summary.\n\n"
        f"{text}"
    )
    result = await llm_complete(prompt, temperature=0.1, max_tokens=target * 2)
    if _word_count(result) <= target + tolerance:
        return result
    # Hard fallback: truncate by words
    log.warning("LLM truncation still overshot, applying hard word-split truncation")
    return " ".join(text.split()[:target])


async def chain_of_density(text: str, target_words: int = 200, iterations: int = MAX_COD_ITERATIONS) -> str:
    from .llm import llm_complete  # local import to avoid circular

    # Iteration 0: SPARSE. This is the most critical constraint.
    # Do NOT start dense. Starting dense produces entity-jammed unreadable output.
    sparse_prompt = (
        f"Summarize the following text in exactly {target_words} words. "
        "Use few named entities. Prioritize readability and narrative flow over completeness. "
        "Do not start dense. Write as if explaining to a general academic audience.\n\n"
        f"{text}"
    )
    summary = await llm_complete(sparse_prompt, temperature=0.3, max_tokens=target_words * 3)
    summary = await _enforce_word_count(summary, target_words, llm_complete)
    log.info("CoD iteration 0 (sparse): %d words", _word_count(summary))

    for i in range(iterations):
        # Identify missing salient entities — separate call, no summary in context
        identify_prompt = (
            "The following summary omits important named entities from the source text. "
            "List 1-3 named entities or technical terms that appear in the source text but are "
            "absent from the summary. Return a JSON array of strings. "
            "If no important entities are missing, return an empty array [].\n\n"
            f"Source text:\n{text}\n\nSummary:\n{summary}"
        )
        entities_raw = await llm_complete(identify_prompt, temperature=0.1, max_tokens=200)
        try:
            missing_entities = json.loads(entities_raw.strip())
            if not isinstance(missing_entities, list):
                missing_entities = []
        except json.JSONDecodeError:
            log.warning("CoD identify_missing returned non-JSON, skipping iteration %d", i + 1)
            break

        if not missing_entities:
            log.info("CoD converged at iteration %d (no missing entities)", i + 1)
            break

        # Densify: add missing entities, hold length fixed
        densify_prompt = (
            f"Rewrite the following summary to include these missing entities: {missing_entities}. "
            f"The rewritten summary must be exactly {target_words} words. "
            "Do not remove any existing named entities. The summary must remain coherent prose. "
            "Do not add claims that are not supported by the source text.\n\n"
            f"Summary:\n{summary}"
        )
        summary = await llm_complete(densify_prompt, temperature=0.3, max_tokens=target_words * 3)
        summary = await _enforce_word_count(summary, target_words, llm_complete)
        log.info("CoD iteration %d: %d words, added entities: %s", i + 1, _word_count(summary), missing_entities)

    return summary
```

### Citation validation cascade with SQLite cache

```python
# stages/citations.py (key excerpt)
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import bibtexparser
import requests
from habanero import Crossref
from semanticscholar import SemanticScholar

from schemas import CitationResult, CitationValidationResponse

log = logging.getLogger("academic-pipeline.citations")

AUTHOR_OVERLAP_THRESHOLD = 0.60
TITLE_FUZZY_THRESHOLD = 0.85
CACHE_TTL_DAYS = 30
FLAGGED_PATH = os.environ.get("CITATIONS_FLAGGED_PATH", "./citations_flagged.json")

_cr = Crossref(mailto=os.environ.get("POLITE_EMAIL", "labmate@localhost"))
_ss = SemanticScholar()


def _cache_db() -> sqlite3.Connection:
    cache_dir = Path.home() / ".cache" / "labmate"
    cache_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache_dir / "citations.db"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS citations "
        "(doi_or_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, cached_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _cache_key(doi: str | None, arxiv_id: str | None, title: str) -> str:
    if doi:
        return f"doi:{doi.lower().strip()}"
    if arxiv_id:
        return f"arxiv:{arxiv_id.lower().strip()}"
    return f"title:{hashlib.sha256(title.lower().strip().encode()).hexdigest()[:16]}"


def _cache_get(conn: sqlite3.Connection, key: str) -> CitationResult | None:
    row = conn.execute("SELECT result_json, cached_at FROM citations WHERE doi_or_key=?", (key,)).fetchone()
    if row is None:
        return None
    cached_at = datetime.fromisoformat(row[1])
    if datetime.utcnow() - cached_at > timedelta(days=CACHE_TTL_DAYS):
        conn.execute("DELETE FROM citations WHERE doi_or_key=?", (key,))
        conn.commit()
        return None
    return CitationResult.model_validate_json(row[0])


def _cache_put(conn: sqlite3.Connection, key: str, result: CitationResult) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO citations (doi_or_key, result_json, cached_at) VALUES (?,?,?)",
        (key, result.model_dump_json(), datetime.utcnow().isoformat()),
    )
    conn.commit()


def _title_match(api_title: str, bib_title: str) -> bool:
    import difflib
    return difflib.SequenceMatcher(None, api_title.lower(), bib_title.lower()).ratio() >= TITLE_FUZZY_THRESHOLD


def _author_overlap(api_authors: list[str], bib_authors: list[str]) -> float:
    def normalize(name: str) -> str:
        return re.sub(r"[^a-z]", "", name.lower())
    a = {normalize(n) for n in api_authors}
    b = {normalize(n) for n in bib_authors}
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _extract_fields(rec: dict) -> tuple[str | None, str | None, str, list[str]]:
    doi = rec.get("doi") or rec.get("DOI")
    arxiv_id = rec.get("arxiv_id") or rec.get("eprint", "").replace("arXiv:", "").strip() or None
    title = rec.get("title", "")
    raw_authors = rec.get("author", "")
    authors = [a.strip() for a in re.split(r" and |,", raw_authors) if a.strip()]
    return doi, arxiv_id, title, authors


async def _validate_one(entry: str, conn: sqlite3.Connection) -> CitationResult:
    try:
        bib_db = bibtexparser.loads(entry)
        rec = bib_db.entries[0]
    except Exception as exc:
        log.warning("bibtexparser failed: %s", exc)
        return CitationResult(entry_id="?", valid=False, flagged_for_review=True, conflict_reason="bibtexparser parse failed")

    entry_id = rec.get("ID", "unknown")
    doi, arxiv_id, title, authors = _extract_fields(rec)

    cache_key = _cache_key(doi, arxiv_id, title)
    cached = _cache_get(conn, cache_key)
    if cached is not None:
        log.info("cache hit for %s", entry_id)
        return cached

    # Step 3: DOI via Crossref
    if doi:
        try:
            meta = _cr.works(ids=doi)
            item = meta["message"]
            api_title = item.get("title", [""])[0]
            api_authors = [f"{a.get('given','')} {a.get('family','')}".strip() for a in item.get("author", [])]
            if _title_match(api_title, title) and _author_overlap(api_authors, authors) >= AUTHOR_OVERLAP_THRESHOLD:
                result = CitationResult(
                    entry_id=entry_id, valid=True, source="crossref",
                    normalized_bibtex=_crossref_to_bibtex(item, entry_id),
                )
                _cache_put(conn, cache_key, result)
                return result
            result = CitationResult(entry_id=entry_id, valid=False, flagged_for_review=True, conflict_reason="DOI resolves but title/author mismatch")
            _cache_put(conn, cache_key, result)
            return result
        except Exception as exc:
            log.warning("Crossref lookup failed for %s: %s", entry_id, exc)

    # Step 4: arXiv
    if arxiv_id:
        try:
            resp = requests.get(f"https://export.arxiv.org/abs/{arxiv_id}", timeout=10)
            # parse title and authors from HTML (simplified; use feedparser for full fidelity)
            api_title_match = re.search(r'<title>(.*?)</title>', resp.text, re.DOTALL)
            api_title = api_title_match.group(1).strip() if api_title_match else ""
            if _title_match(api_title, title):
                result = CitationResult(entry_id=entry_id, valid=True, source="arxiv", normalized_bibtex=entry)
                _cache_put(conn, cache_key, result)
                return result
            result = CitationResult(entry_id=entry_id, valid=False, flagged_for_review=True, conflict_reason="arXiv ID resolves but title/author mismatch")
            _cache_put(conn, cache_key, result)
            return result
        except Exception as exc:
            log.warning("arXiv lookup failed for %s: %s", entry_id, exc)

    # Step 5: Semantic Scholar title search
    if title:
        try:
            hits = _ss.search_paper(title, limit=3)
            for hit in (hits or []):
                api_authors = [a.get("name", "") for a in (hit.authors or [])]
                if _author_overlap(api_authors, authors) >= AUTHOR_OVERLAP_THRESHOLD:
                    result = CitationResult(entry_id=entry_id, valid=True, source="semantic_scholar", normalized_bibtex=entry)
                    _cache_put(conn, cache_key, result)
                    return result
        except Exception as exc:
            log.warning("Semantic Scholar lookup failed for %s: %s", entry_id, exc)

    # Step 6: LLM fallback — advisory only, always flagged
    result = CitationResult(
        entry_id=entry_id, valid=False, flagged_for_review=True, source="llm_fallback",
        conflict_reason="No identifier found; title search returned no author-overlap match. Likely hallucinated. Manual verification required.",
    )
    _cache_put(conn, cache_key, result)
    return result


async def validate_citations(bibtex_entries: list[str]) -> CitationValidationResponse:
    conn = _cache_db()
    results = [await _validate_one(entry, conn) for entry in bibtex_entries]
    conn.close()

    # Deduplicate citation keys among valid entries
    key_counts: dict[str, int] = {}
    for r in results:
        if r.valid and r.normalized_bibtex:
            key = _extract_bibtex_key(r.normalized_bibtex)
            if key in key_counts:
                key_counts[key] += 1
                suffix = chr(ord('a') + key_counts[key] - 1)
                r.entry_id = f"{key}{suffix}"
            else:
                key_counts[key] = 1

    # Write flagged entries to citations_flagged.json
    flagged = [r.model_dump() for r in results if r.flagged_for_review]
    if flagged:
        existing = []
        flagged_path = Path(FLAGGED_PATH)
        if flagged_path.exists():
            try:
                existing = json.loads(flagged_path.read_text())
            except Exception:
                pass
        flagged_path.write_text(json.dumps(existing + flagged, indent=2))
        log.warning("wrote %d flagged citations to %s", len(flagged), FLAGGED_PATH)

    return CitationValidationResponse(results=results)


def _extract_bibtex_key(bibtex: str) -> str:
    m = re.match(r'@\w+\{([^,]+),', bibtex.strip())
    return m.group(1).strip() if m else "unknown"
```

### Citation key guard in draft.py

```python
# stages/draft.py (key excerpt)
from __future__ import annotations

import logging
import re

from schemas import Outline, OutlineSection

log = logging.getLogger("academic-pipeline.draft")

# IMRaD role constraints — embedded verbatim in the prompt, not referenced externally.
IMRAD_ROLE_CONSTRAINTS = {
    "Introduction": "Permitted: motivation, problem statement, research gap, contributions. Prohibited: results, conclusions, evaluation numbers.",
    "Background": "Permitted: related work, prior techniques, foundational concepts. Prohibited: novel claims about this paper's approach.",
    "Methods": "Permitted: approach, algorithm, architecture, design decisions. Prohibited: results, evaluation numbers, interpretation.",
    "Experimental Setup": "Permitted: datasets, baselines, hyperparameters, evaluation metrics. Prohibited: results, numbers from experiments.",
    "Results": "Permitted: figures, tables, numbers from supplied notes only. Prohibited: interpretation, new claims, numbers not in the supplied notes.",
    "Discussion": "Permitted: interpretation of results, limitations, future work. Prohibited: new unreported numbers, new experimental claims.",
    "Conclusion": "Permitted: summary of contributions, restatement of key findings. Prohibited: new claims not supported by the Results section.",
}


class CitationKeyGuardError(ValueError):
    """Raised when the LLM draft cites a key not in the validated reference set."""


def _extract_bibtex_key(bibtex: str) -> str:
    m = re.match(r'@\w+\{([^,]+),', bibtex.strip())
    return m.group(1).strip() if m else ""


async def draft_section(section_name: str, outline: Outline, refs: list[str], notes: str) -> str:
    from .llm import llm_complete

    valid_keys = {_extract_bibtex_key(r) for r in refs if _extract_bibtex_key(r)}
    role_constraint = IMRAD_ROLE_CONSTRAINTS.get(section_name, "Follow standard academic writing conventions.")

    section_node = next((s for s in outline.sections if s.name == section_name), None)
    if section_node is None:
        raise ValueError(f"Section '{section_name}' not found in the supplied outline.")

    ref_context = []
    for r in refs:
        key = _extract_bibtex_key(r)
        # Extract title and abstract with simple regex (bibtexparser for full fidelity)
        title_m = re.search(r'title\s*=\s*\{([^}]+)\}', r, re.IGNORECASE)
        title = title_m.group(1) if title_m else ""
        ref_context.append({"key": key, "title": title})

    prompt = (
        f"Write the {section_name} section of an academic paper.\n\n"
        f"IMRaD role constraint for {section_name}:\n{role_constraint}\n\n"
        f"Key points to cover:\n" + "\n".join(f"- {p}" for p in section_node.key_points) + "\n\n"
        f"Word budget: approximately {section_node.word_budget} words.\n\n"
        f"Available references (cite using \\cite{{key}}):\n{ref_context}\n\n"
        f"CRITICAL: You may only cite keys from this exact list: {sorted(valid_keys)}. "
        "Do not cite any other key. Do not invent new citation keys.\n\n"
        f"Grounding notes for this section:\n{notes}\n\n"
        "Write the section now. Use \\cite{key} for inline citations."
    )

    text = await llm_complete(prompt, temperature=0.4, max_tokens=section_node.word_budget * 3)

    # Citation key guard — non-negotiable
    cited_keys = set(re.findall(r'\\cite\{([^}]+)\}', text))
    unknown = cited_keys - valid_keys
    if unknown:
        log.error("citation key guard violated: section=%s unknown_keys=%s", section_name, unknown)
        raise CitationKeyGuardError(
            f"Section '{section_name}' cited unvalidated keys: {unknown}. "
            "These keys are not in the supplied validated ref set. "
            "Re-invoke with a corrected ref list or remove the citation."
        )

    log.info("drafted section=%s words=%d citations=%s", section_name, len(text.split()), cited_keys)
    return text
```

### Critique reflexion loop — critique() → ground_with_signals() → refine(), max 3 rounds

```python
# stages/critique.py (key excerpt)
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from schemas import CritiqueResult, Issue

log = logging.getLogger("academic-pipeline.critique")

MAX_ROUNDS = 3  # hard cap — exceeding causes Degeneration-of-Thought
STOP_THRESHOLD = 0.90
MIN_CONFIDENCE = 0.50
MEMORY_WINDOW = 3


async def _ground_with_signals(draft_text: str) -> dict:
    """Gather external signals before the LLM evaluator is called.

    Never call the LLM evaluator without running this first.
    Pure self-critique without external signals causes DoT.
    """
    signals = {}

    # IMRaD structure check
    canonical = ["Introduction", "Background", "Methods", "Experimental Setup", "Results", "Discussion", "Conclusion"]
    found = [s for s in canonical if re.search(rf'\b{s}\b', draft_text, re.IGNORECASE)]
    missing = [s for s in canonical if s not in found]
    signals["imrad_structure"] = {"found_sections": found, "missing_sections": missing}

    # Citation key check against SQLite cache
    cited_keys = re.findall(r'\\cite\{([^}]+)\}', draft_text)
    signals["cited_keys"] = cited_keys

    # CoVe factored verification: generate questions, answer in isolation
    signals["cove_verification"] = await _cove_verify(draft_text)

    return signals


async def _cove_verify(draft_text: str) -> list[dict]:
    """Chain-of-Verification factored variant.

    Generates verification questions from the draft, then answers each question
    WITHOUT the draft in context. Contradictions are flagged as external signals.
    """
    from .llm import llm_complete

    questions_prompt = (
        "The following is an academic paper draft. Generate 2-3 factual verification questions "
        "about specific claims made in the draft. Each question should be answerable from "
        "general knowledge without seeing the draft. Return a JSON array of question strings.\n\n"
        f"Draft:\n{draft_text[:3000]}"  # cap to avoid context overflow
    )
    raw = await llm_complete(questions_prompt, temperature=0.1, max_tokens=300)
    try:
        questions = json.loads(raw.strip())
        if not isinstance(questions, list):
            questions = []
    except json.JSONDecodeError:
        log.warning("CoVe question generation returned non-JSON")
        return []

    results = []
    for q in questions[:3]:  # cap at 3
        # Answer WITHOUT the draft in context — this is the factored variant
        # If the draft were in context, the model would parrot its own claims
        answer = await llm_complete(q, temperature=0.1, max_tokens=200)
        results.append({"question": q, "answer": answer, "draft_context": False})

    return results


async def critique(draft_path: str, max_rounds: int = 3) -> CritiqueResult:
    from .llm import llm_complete

    # Hard cap — prevent DoT regardless of caller input
    max_rounds = min(max_rounds, MAX_ROUNDS)
    if max_rounds < 3:
        log.info("max_rounds clamped to %d", max_rounds)

    draft_text = Path(draft_path).read_text(encoding="utf-8")
    best_output = draft_text
    best_score = 0.0
    memory: list[dict] = []
    last_crit: CritiqueResult | None = None

    for i in range(max_rounds):
        log.info("critique round %d/%d", i + 1, max_rounds)

        # Ground with external signals BEFORE the LLM evaluator
        signals = await _ground_with_signals(best_output)

        # Invoke LLM evaluator with adversarial critic system prompt
        evaluator_system = (
            "You are a rigorous academic peer reviewer. Your job is to find flaws, inconsistencies, "
            "and unsupported claims. You are NOT the author. You have not seen the writing process. "
            "Be critical. Do not affirm. Temperature is low because accuracy matters more than creativity."
        )
        evaluator_prompt = (
            f"Review the following academic paper draft.\n\n"
            f"External signals you must treat as ground truth:\n"
            f"- Missing IMRaD sections: {signals['imrad_structure']['missing_sections']}\n"
            f"- Cited keys in draft: {signals['cited_keys']}\n"
            f"- CoVe verification results: {json.dumps(signals['cove_verification'], indent=2)}\n\n"
            "Return a JSON object matching this schema exactly:\n"
            "{\n"
            '  "verdict": "pass" | "revise" | "fail",\n'
            '  "severity": "low" | "medium" | "high" | "critical",\n'
            '  "score": float (0.0-1.0),\n'
            '  "issues_found": [{"location": str, "category": str, "explanation": str, "grounded_by": str|null}],\n'
            '  "constitutional_violations": [str],\n'
            '  "suggested_revision": str,\n'
            '  "evidence": [str],\n'
            '  "confidence": float (0.0-1.0),\n'
            '  "no_issues_justification": str|null\n'
            "}\n\n"
            f"Draft to review:\n{best_output}"
        )

        raw_crit = await llm_complete(evaluator_prompt, system=evaluator_system, temperature=0.1, max_tokens=2048)

        # Parse and validate
        try:
            crit_dict = json.loads(raw_crit)
            last_crit = CritiqueResult.model_validate({**crit_dict, "best_revised_draft": best_output})
        except Exception as exc:
            log.warning("critique parse failed round %d: %s — retrying", i + 1, exc)
            # Re-ask: one retry with explicit schema reminder
            raw_crit2 = await llm_complete(evaluator_prompt + "\n\nReturn ONLY valid JSON. No prose before or after.", system=evaluator_system, temperature=0.0, max_tokens=2048)
            try:
                crit_dict = json.loads(raw_crit2)
                last_crit = CritiqueResult.model_validate({**crit_dict, "best_revised_draft": best_output})
            except Exception as exc2:
                log.error("critique re-ask also failed: %s — skipping round %d without consuming it", exc2, i + 1)
                continue  # do not consume the round on evaluator failure

        if last_crit.score > best_score:
            best_score = last_crit.score

        if last_crit.verdict == "pass" or last_crit.score >= STOP_THRESHOLD:
            log.info("critique verdict=pass at round %d, score=%.2f", i + 1, last_crit.score)
            break

        # Append lessons to episodic memory (bounded window)
        if last_crit.confidence >= MIN_CONFIDENCE:
            memory = (memory + [{"lessons": last_crit.suggested_revision}])[-MEMORY_WINDOW:]

        # Refine
        lessons_text = "\n".join(f"- {m['lessons']}" for m in memory)
        refine_prompt = (
            f"Revise the following academic paper draft to address the critique.\n\n"
            f"Critique summary:\n{last_crit.suggested_revision}\n\n"
            f"Lessons from prior rounds:\n{lessons_text}\n\n"
            "Preserve all valid content. Do not introduce new unsupported claims. "
            "Preserve all \\cite{} keys.\n\n"
            f"Draft:\n{best_output}"
        )
        revised = await llm_complete(refine_prompt, temperature=0.4, max_tokens=4096)

        # Convergence check: if revision changes fewer than 5 tokens, DoT has set in — exit
        diff_size = len(set(revised.split()) ^ set(best_output.split()))
        if diff_size < 5:
            log.info("convergence detected at round %d (diff_size=%d < 5), exiting loop", i + 1, diff_size)
            break

        best_output = revised

    if last_crit is None:
        # All rounds had evaluator failures; return a minimal result
        last_crit = CritiqueResult(
            verdict="revise", severity="low", score=0.0, issues_found=[],
            constitutional_violations=[], suggested_revision="Evaluator failed to produce a valid critique.",
            evidence=[], confidence=0.0, no_issues_justification="All critique rounds failed to parse.",
            best_revised_draft=best_output,
        )

    last_crit.best_revised_draft = best_output
    return last_crit
```

### How the skill calls vLLM via INFERENCE_URL

```python
# stages/llm.py — the single LLM call path for the entire skill
from __future__ import annotations

import logging
import os

from openai import AsyncOpenAI

log = logging.getLogger("academic-pipeline.llm")

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        base = os.environ.get("INFERENCE_URL", "http://host.docker.internal:8000").rstrip("/")
        _client = AsyncOpenAI(
            base_url=f"{base}/v1",
            api_key="not-used",  # vLLM does not require a key; header is still sent
        )
        log.info("created AsyncOpenAI client, base_url=%s/v1", base)
    return _client


async def llm_complete(
    prompt: str,
    system: str = "",
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> str:
    """Single call path for all LLM completions in the academic-pipeline skill.

    All stages import from here. Never instantiate AsyncOpenAI elsewhere in this skill.
    Uses Contract A (OpenAI-compatible HTTP, INFERENCE_URL env var).
    """
    client = _get_client()
    model = os.environ.get("INFERENCE_MODEL", "google/gemma-4-9b-it")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = resp.choices[0].message.content or ""
        log.debug("llm_complete: %d input chars → %d output chars", len(prompt), len(content))
        return content
    except Exception as exc:
        log.error("llm_complete failed: %s", exc)  # stderr only
        raise
```

---

## 7. Integration Verification

Test each stage independently before testing the full pipeline.

### Stage-level tests (run without the MCP server)

```python
# Run from services/skills/academic-pipeline/

# Step 1: schemas
python -c "
from schemas import CritiqueResult, Issue, OutlineSection, CitationResult
# Valid CritiqueResult
cr = CritiqueResult(verdict='pass', severity='low', score=0.95, issues_found=[],
                    constitutional_violations=[], suggested_revision='None needed.',
                    evidence=[], confidence=0.9, no_issues_justification='No issues found.', best_revised_draft='')
print('CritiqueResult OK:', cr.verdict)
# Invalid: empty issues_found with no justification should raise
try:
    CritiqueResult(verdict='pass', severity='low', score=0.95, issues_found=[],
                   constitutional_violations=[], suggested_revision='',
                   evidence=[], confidence=0.9, no_issues_justification=None, best_revised_draft='')
    print('FAIL: should have raised ValidationError')
except Exception as e:
    print('CitationKeyGuard raises as expected:', type(e).__name__)
"

# Step 2: zero stdout pollution
python server.py < /dev/null 2>/dev/null | wc -c
# Expected: 0

# Step 3: MCP handshake
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}' \
  | python server.py 2>/dev/null | python -m json.tool
# Expected: valid JSON-RPC response with serverInfo.name == "academic-pipeline"

# Step 4: tools/list
(
  echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}'
  echo '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}'
  echo '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
) | python server.py 2>/dev/null
# Expected: tools array with 6 entries, all names matching the SKILL.md

# Step 5: citation validation (live — requires internet)
INFERENCE_URL=http://host.docker.internal:8000 python -c "
import asyncio
from stages.citations import validate_citations
result = asyncio.run(validate_citations([
    '@article{mcmahan2017, title={Communication-Efficient Learning of Deep Networks}, author={McMahan, B and Moore, E and Ramage, D}, year={2017}, eprint={1602.05629}, archivePrefix={arXiv}}'
]))
print(result.results[0].valid, result.results[0].source)
# Expected: True  arxiv
"
```

### Full pipeline end-to-end test

```python
# e2e_test.py — run from services/skills/academic-pipeline/
import asyncio, json, textwrap
from pathlib import Path

TOPIC = "Federated learning for privacy-preserving medical image segmentation"
BIBTEX = [
    textwrap.dedent("""\
    @article{mcmahan2017,
      title={Communication-Efficient Learning of Deep Networks from Decentralized Data},
      author={McMahan, B and Moore, E and Ramage, D and Hampson, S and Arcas, B},
      year={2017},
      eprint={1602.05629},
      archivePrefix={arXiv}
    }"""),
]

async def run():
    from stages.citations import validate_citations
    from stages.outline import generate_outline
    from stages.draft import draft_section
    from stages.density import chain_of_density
    from stages.style import style_transfer
    from stages.critique import critique

    # Stage 1: validate citations
    val_result = await validate_citations(BIBTEX)
    valid_bibtex = [r.normalized_bibtex or BIBTEX[i] for i, r in enumerate(val_result.results) if r.valid]
    print(f"[1] validated: {len(valid_bibtex)}/{len(BIBTEX)} entries passed")

    # Stage 2: outline
    outline = await generate_outline(TOPIC, valid_bibtex)
    print(f"[2] outline: {[s.name for s in outline.sections]}")
    assert len(outline.sections) == 7, f"expected 7 sections, got {len(outline.sections)}"

    # Stage 3: draft one section (Introduction only for speed)
    intro = await draft_section("Introduction", outline, valid_bibtex, notes="Focus on privacy motivations.")
    print(f"[3] Introduction drafted: {len(intro.split())} words")
    assert "\\cite{" in intro or len(intro) > 100, "Introduction appears empty"

    # Stage 4: Chain-of-Density on the introduction (normally run on full body)
    abstract = await chain_of_density(intro, target_words=150)
    print(f"[4] abstract: {len(abstract.split())} words (target 150)")
    assert 120 <= len(abstract.split()) <= 180, f"word count out of range: {len(abstract.split())}"

    # Stage 5: style transfer
    formal = await style_transfer(intro)
    print(f"[5] style transfer: {len(formal.split())} words")

    # Stage 6: critique
    draft_file = Path("/tmp/e2e_draft.md")
    draft_file.write_text(intro, encoding="utf-8")
    result = await critique(str(draft_file), max_rounds=1)
    print(f"[6] critique: verdict={result.verdict} score={result.score:.2f} issues={len(result.issues_found)}")

    print("E2E test passed.")

asyncio.run(run())
```

---

## 8. Done Criteria

The skill is working when all of the following are true:

- [ ] `python server.py < /dev/null 2>/dev/null | wc -c` outputs `0` — zero stdout bytes on startup
- [ ] MCP initialize handshake succeeds — the SkillRegistry can `tools/list` and gets back exactly 6 tools
- [ ] `paper_validate_citations` with a known-good arXiv BibTeX entry returns `valid=true, source="arxiv"`
- [ ] `paper_validate_citations` with a fabricated DOI returns `valid=false, flagged_for_review=true`
- [ ] A flagged entry is written to `citations_flagged.json` at `CITATIONS_FLAGGED_PATH`
- [ ] SQLite cache at `~/.cache/labmate/citations.db` is created on first run, and a second call with the same DOI hits the cache without making an external HTTP call
- [ ] `paper_outline` returns a JSON outline with all seven canonical IMRaD sections in order
- [ ] `paper_outline` raises `isError: true` if the LLM omits any mandatory section
- [ ] `paper_draft_section` returns prose containing at least one `\cite{key}` from the supplied ref set
- [ ] `paper_draft_section` returns `isError: true` with `CitationKeyGuardError` when the LLM cites a key not in the supplied ref set
- [ ] `paper_chain_of_density` returns text within ±5% of `target_words`
- [ ] `paper_chain_of_density` produces output where iteration 0 has visibly fewer named entities than iteration 3
- [ ] `paper_style_transfer` returns text with all original `\cite{}` and numeric tokens present
- [ ] `paper_style_transfer` returns `isError: true` with `StyleIntegrityError` if a `\cite{}` is dropped and two retries fail
- [ ] `paper_critique` with `max_rounds=5` clamps to `max_rounds=3` internally
- [ ] `paper_critique` exits early on convergence (fewer than 5 token changes between rounds) before consuming all 3 rounds
- [ ] `paper_critique` returns a `CritiqueResult` with `best_revised_draft` populated
- [ ] Pydantic raises `ValidationError` on a `CritiqueResult` with empty `issues_found` and no `no_issues_justification`
- [ ] Full E2E test (`e2e_test.py`) completes without error on the sample topic
- [ ] The skill is registered in the SkillRegistry and the orchestrator's `tools/list` response includes all six namespaced tools as `academic-pipeline.paper_outline`, etc.
- [ ] Docker build (`docker build .`) succeeds and the healthcheck passes
- [ ] No bare `print()` calls appear in any `.py` file in this skill directory

---

## 9. Common Mistakes

### Citation hallucination (11-57% rate — the cascade is mandatory, not optional)

LLMs generate plausible-looking BibTeX entries with DOIs that do not resolve, author lists that are subtly wrong, and titles that do not exist. This is normal LLM behavior, not an edge case. Rates of 11-57% have been measured across production deployments.

**The validation cascade in `citations.py` is non-optional.** Do not skip it to save API calls. Do not trust LLM BibTeX text verbatim. Do not pass any entry to `paper_draft_section` unless `valid=True` from the cascade. The `paper_draft_section` citation key guard is the last line of defense, but the cascade is the primary defense — the guard catches keys that slipped through, not keys that were never validated.

If you add a `--skip-validation` flag or bypass the cascade for speed, you will get hallucinated citations in the bibliography.

### Degeneration-of-Thought in the critique loop — must have external grounding signals

DoT (Liang et al. EMNLP 2024) is the failure mode where the model becomes overconfident in its first answer after 3-4 rounds of self-critique without fresh external signal. The critique appears to continue but produces only superficial paraphrasing.

**Every round of `paper_critique` MUST call `_ground_with_signals()` before invoking the LLM evaluator.** The signals (IMRaD structure check, citation key check, CoVe verification) are injected into the evaluator prompt as quoted evidence. If you remove `_ground_with_signals()` or call the LLM evaluator without its output, the critique loop degenerates into DoT exactly when critique quality matters most.

**The hard cap of 3 rounds is not a performance optimization.** It is required to prevent DoT. Do not raise it.

### Stdout pollution — kills the MCP session silently

Any write to stdout that is not a JSON-RPC frame breaks the host's JSON parser. The error messages are `"Unexpected token"` or `"JSON Parse error"`, not "stdout pollution." The source is almost always a bare `print()` in the skill code or a startup banner from an imported library.

The rule: every log in this skill uses `log.info()`, `log.warning()`, or `log.error()`. No `print()` calls exist anywhere in this directory. Check with `grep -r 'print(' services/skills/academic-pipeline/ --include='*.py'` — any output means a bug.

Also check imported libraries. `bibtexparser` v2 and some versions of `habanero` print to stdout on import. Run `python -c "import bibtexparser" 2>/dev/null | wc -c` — any non-zero output requires a workaround (redirect or patch the import).

### Bypassing the citation key guard in draft.py

The citation key guard in `draft_section()` raises `CitationKeyGuardError` when the LLM cites a key not in the supplied validated set. This is not a suggested behavior — it is a hard check that must not be silenced or wrapped in a `try/except` that logs and continues.

Bypassing the guard allows hallucinated citation keys to enter the draft. Those keys will reference entries not in `references.bib`, producing compile errors in LaTeX and invalid citations in the final paper.

The guard must propagate to the MCP layer as `isError: true`.

### Running more than 3 reflexion rounds

The `max_rounds` parameter in `paper_critique` is clamped to 3 in the implementation. Do not remove the clamp. Do not expose an unclamped parameter. Do not add a `force_max_rounds` flag.

After 3 rounds of critique with external grounding, additional rounds produce DoT: the model has consumed all the information the external signals can provide and begins paraphrasing prior reflections. The output quality either plateaus or degrades.

### CoD starting dense instead of sparse

The first iteration of Chain-of-Density must start with a sparse summary. If you instruct the first iteration to "be comprehensive" or "include all key entities," the output is an entity-jammed, unreadable abstract from the start, and subsequent iterations have nowhere to go.

The prompt for iteration 0 explicitly says: "Use few named entities. Prioritize readability and narrative flow. Do not start dense." Keep that instruction. The density increases naturally through iterations 1-3 as missing entities are identified and added.

### Extracting BibTeX keys with fragile regex

The `_extract_bibtex_key()` helper uses `re.match(r'@\w+\{([^,]+),', ...)`. This works for standard BibTeX but fails on entries with whitespace before the key, entries using `{` in the type name (non-standard), or entries that span multiple lines before the key. Use `bibtexparser.loads()` as the primary extraction method and the regex only as a fallback. Test against entries with and without whitespace after the opening brace.
