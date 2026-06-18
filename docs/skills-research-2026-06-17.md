# LLM Skills/Tools Research for Labmate

**Date**: 2026-06-17
**Scope**: arxiv, conference papers (NeurIPS/ICLR/EMNLP/ACL/ICML/CVPR), GitHub repos

---

## Executive Summary

The agent-tooling landscape in 2024–2026 has matured from monolithic frameworks into a set of composable, well-understood capabilities — and almost all of them map cleanly onto Labmate's polyglot child-process MCP-server model. The highest-leverage additions for a local, single-GPU coding + academic-writing agent fall into four clusters: (1) **repository-level code graphs** (RepoGraph) that complement the already-planned `ast-repo-map` with line-level reference edges proven to lift SWE-bench scores; (2) **scientific document ingestion + grounded RAG** (MinerU/Docling for PDF parsing, PaperQA2 for cited retrieval, Semantic Scholar/OpenAlex for citation graphs), which directly feeds the `academic-writing` and `critique` skills; (3) **citation and claim-level hallucination checking** (RefChecker, CiteCheck) that turns the planned `critique` Reflexion loop into a verifiable grounding pipeline; and (4) **a secure local code-execution sandbox** for test running and figure generation, which is a hard prerequisite for any test-generation or data-analysis skill. Memory architecture work (Mem0, Letta, Zep, LangMem) validates Labmate's MongoDB+Chroma+Redis stack and offers concrete patterns (deduplicating self-edits, temporal validity, procedural self-editing) worth adopting rather than re-inventing. Tree-search planning (LATS, RethinkMCTS) is powerful but should be deferred — it conflicts with the local single-GPU latency budget and the existing LangGraph goal-tree design already covers the common case.

The recurring theme: prefer **library-as-MCP-server** integrations (PaperQA2, MinerU, RepoGraph, RefChecker) over re-implementing research prototypes, and lean on **claim-triplet / retrieval grounding** as the connective tissue between the coding and writing halves of the agent.

---

## High-Priority Additions

### repo-graph (RepoGraph)

**Source**: RepoGraph, arXiv:2410.14684 (ICLR-adjacent, v2 Mar 2025) — https://arxiv.org/abs/2410.14684 ; reading list https://github.com/YerbaPage/Awesome-Repo-Level-Code-Generation
**What it does**: Builds a line-level repository code graph capturing reference/definition edges between code elements, exposed to an agent as a `search_repograph` action. Plugging it into RAG, Agentless, SWE-agent, and AutoCodeRover gave a new SOTA among open-source frameworks on SWE-bench and generalized to CrossCodeEval.
**Why for Labmate**: Complements the planned `ast-repo-map` (PageRank file ranking) with the missing piece — *cross-file dependency edges at line granularity* so the orchestrator can answer "what calls this / what would break." This is exactly the structural context that flat tree-sitter maps lack, and it directly improves localization for any code-edit task.
**Implementation path**: Python MCP server wrapping the RepoGraph builder; persist the graph in MongoDB (or a lightweight SQLite sidecar like the existing codegraph index) and expose `search_repograph(symbol|query)`. Could co-locate with `ast-repo-map`.
**Complexity**: Medium
**Dependencies**: tree-sitter (already in stack), networkx, the RepoGraph reference implementation.

### paper-rag (PaperQA2)

**Source**: PaperQA, arXiv:2312.07559 — https://arxiv.org/abs/2312.07559 ; repo https://github.com/future-house/paper-qa
**What it does**: High-accuracy agentic RAG over scientific PDFs/office docs/source code. Retrieves metadata (with citation counts + retraction checks via Semantic Scholar/OpenAlex), parses and caches PDFs into a full-text index, then answers with inline citations. Reports superhuman performance on science QA, summarization, and contradiction detection; agentic multi-call retrieval beats linear RAG.
**Why for Labmate**: This is the literature-grounding engine the `academic-writing` (IMRaD) and `critique` skills need. It produces *cited* answers — essential for the citation-validation requirement already in the spec — and runs fully local against a folder of PDFs plus optional Semantic Scholar lookups.
**Implementation path**: Python MCP server wrapping `paper-qa`; point its vector store at the existing Chroma container (client-server mode per CLAUDE.md rule #4) instead of its default index. Embeddings via a local model to honor the single-GPU/local-first constraint.
**Complexity**: Medium
**Dependencies**: paper-qa, Chroma (existing), a local embedding model; Semantic Scholar API key (optional, free tier).

### pdf-parse (MinerU / Docling)

**Source**: MinerU (OpenDataLab) and Docling; benchmark OmniDocBench, CVPR 2025 — https://github.com/opendatalab/OmniDocBench
**What it does**: Multi-stage scientific-PDF parsers that do layout detection, reading-order reconstruction, table extraction (complex tables as HTML), LaTeX formula recognition, 84-language OCR, and figure/caption pairing. Output is clean Markdown/HTML suitable for LLM input.
**Why for Labmate**: A prerequisite for `paper-rag` and for any "read this paper / extract this figure" workflow. MinerU's formula + table fidelity is what makes downstream academic reasoning reliable; Docling's figure-vs-illustration VLM filtering is useful for figure analysis.
**Implementation path**: Python MCP server `pdf-parse(path) -> markdown + extracted assets`. MinerU is heavier (runs detection models — can use the GPU) ; Docling is lighter/CPU-friendly. Recommend Docling as default, MinerU as an opt-in high-fidelity mode.
**Complexity**: Medium (Docling) / High (MinerU model setup)
**Dependencies**: docling / docling-parse, or mineru + PDF-Extract-Kit; PyMuPDF.

### citation-check (RefChecker + CiteCheck)

**Source**: RefChecker, arXiv:2405.14486 — https://arxiv.org/abs/2405.14486 ; CiteCheck (retrieval-grounded citation hallucination detection), arXiv:2605.27700 — https://arxiv.org/html/2605.27700v1
**What it does**: RefChecker decomposes an LLM response into **claim-triplets** and verifies each against reference material with 3-way classification (entailed / contradicted / unverifiable), outperforming sentence/document-level checks on an 11k-claim benchmark. CiteCheck grounds each *reference* against scholarly sources and classifies exact match / minor hallucination (corrupted fields) / major hallucination (no matching paper).
**Why for Labmate**: This operationalizes the "external grounding" promise of the `critique` skill and the citation-validation requirement of `academic-writing`. CiteCheck specifically catches fabricated or mangled citations — the single most damaging failure mode for an academic agent.
**Implementation path**: Python MCP server `verify_claims(text, references)` returning per-claim labels, plus `verify_citations(bibliography)` hitting Semantic Scholar/Crossref. Wire it into the `critique` Reflexion loop as the evaluator's grounding step.
**Complexity**: Medium
**Dependencies**: RefChecker reference impl, an extractor LLM (can be the local Gemma/Qwen), Semantic Scholar + Crossref APIs.

### code-sandbox (local E2B-style execution)

**Source**: E2B (Firecracker microVMs) https://e2b.dev/ ; comparison incl. Daytona/Modal — see Modal blog. Self-hostable: E2B and Daytona are open-source.
**What it does**: Isolated, ephemeral execution environment purpose-built for agent-generated code — runs shell + a stateful code interpreter (Python/JS/etc.) with hardware/VM-level isolation against destructive or untrusted code.
**Why for Labmate**: A hard prerequisite for *any* test-running, data-analysis, or figure-generation skill, and for safely executing model-written code. The threat model (code not human-reviewed) is exactly Labmate's.
**Implementation path**: TypeScript or Python MCP server exposing `run_code` / `run_shell` against a **self-hosted, local** sandbox. Given the local-first/single-GPU constraint, prefer self-hosted E2B (Firecracker) or Daytona (Docker/Sysbox) rather than the cloud APIs; a minimal first version can use a locked-down Docker container with resource limits, then upgrade to microVM isolation.
**Complexity**: Medium (Docker baseline) / High (self-hosted microVM)
**Dependencies**: Docker (already in stack) or self-hosted E2B/Daytona; no GPU needed unless the executed code needs one.

### test-gen (mutation-guided test generation)

**Source**: Mutation-Guided LLM Test Generation @ Meta (ACH), arXiv:2501.12862 (FSE '25) — https://arxiv.org/abs/2501.12862 ; MuTAP (2024); Test vs Mutant, arXiv:2602.08146
**What it does**: Generates unit tests targeted at specific fault classes by feeding *surviving mutants* back into the prompt, raising mutation score and bug-detection rate. Meta's ACH had engineers accept 73% of generated tests; adversarial test-vs-mutant loops push each side to close the other's blind spots.
**Why for Labmate**: Turns the agent from "writes code" into "writes code with a safety net," and the mutation-feedback loop is a natural fit for the Reflexion/LangGraph control flow already planned.
**Implementation path**: SKILL.md-driven workflow orchestrated by LangGraph, calling a small Python MCP server that runs a mutation tool (mutmut/cosmic-ray for Python, StrykerJS for TS) and feeds surviving mutants back to the brain. Requires `code-sandbox` to run the tests/mutants.
**Complexity**: Medium
**Dependencies**: mutmut / cosmic-ray (Python) or StrykerJS (TS); the code-sandbox skill.

---

## Medium-Priority Candidates

### citation-graph (Semantic Scholar / OpenAlex)

**Source**: Semantic Scholar Academic Graph API — https://www.semanticscholar.org/product/api/tutorial ; Open-source Agentic Hybrid RAG for literature review, arXiv:2508.05660
**What it does**: Citation/reference traversal, paper metadata, citation counts, and a paper-embedding nearest-neighbor API for similarity-based discovery. OpenAlex/Crossref add affiliations, DOIs, and bibliometrics.
**Why for Labmate**: Powers literature-review breadth (snowball sampling along citation edges) and feeds both `paper-rag` and `citation-check`. Lightweight, free, local-friendly (just HTTP).
**Implementation path**: Python (or TypeScript) MCP server `search_papers / get_citations / get_references / find_similar`.
**Complexity**: Low
**Dependencies**: Semantic Scholar + OpenAlex + Crossref HTTP APIs.

### knowledge-curation (STORM / Co-STORM)

**Source**: stanford-oval/storm — https://github.com/stanford-oval/storm
**What it does**: Multi-perspective question generation → research → outline → cited long-form article. Co-STORM adds a human-in-the-loop collaborative discourse protocol. Strong at the *pre-writing* (outline + reference gathering) stage.
**Why for Labmate**: Directly augments the `academic-writing` outline phase — STORM's core insight ("the hard part is asking good questions") is reusable as a planning sub-routine for literature reviews and related-work sections.
**Implementation path**: Adopt the *technique* (perspective-guided question generation) inside the `academic-writing` SKILL.md / DSPy pipeline rather than importing STORM wholesale; optionally a Python MCP server for the full report mode. Swap STORM's search backend for `paper-rag` + `citation-graph`.
**Complexity**: Medium
**Dependencies**: dspy (already planned for academic-writing), the search skills above.

### memory-dedup (Mem0 / Zep / LangMem patterns)

**Source**: Mem0 (arXiv:2504.19413), Zep (temporal knowledge graph), LangMem; agent-memory survey arXiv:2512.13564
**What it does**: Not a skill — patterns to bake into the existing StorageManager. Mem0: extract salient memories then **self-edit (add/update/delete) instead of appending duplicates** (reports ~90% token savings). Zep: temporal validity intervals on facts. LangMem: **procedural memory** (agent rewrites its own instructions). Consolidation every 50–200 episodes via a background daemon to avoid latency spikes; beware ~20% fact loss in naive summarization.
**Why for Labmate**: Validates the MongoDB+Chroma+Redis design and supplies concrete, battle-tested algorithms for the memory layer rather than reinventing dedup/consolidation. The transactional-outbox worker (CLAUDE.md rule #7) is the natural place to run consolidation.
**Implementation path**: Python classes/methods inside the orchestrator's memory layer (StorageManager + an outbox-driven consolidation worker). Not an MCP server.
**Complexity**: Medium
**Dependencies**: existing stack; optionally crib from mem0/letta source.

### repo-fault-localize (ARISE / Agentless localization)

**Source**: Agentless (40.67% SWE-bench Lite) ; ARISE, arXiv:2605.03117 ; LARGER, arXiv:2605.16352
**What it does**: Hierarchical "file → class/function → edit" localization (Agentless), graph-based agentic fault localization + repair (ARISE), and lexically-anchored graph retrieval that returns graph evidence inside ordinary search results (LARGER).
**Why for Labmate**: A structured localization workflow that sits on top of `repo-graph` and turns "fix this bug" into a tractable, staged process — and Agentless shows a non-agent pipeline can match agent methods at lower token cost, which suits the local budget.
**Implementation path**: SKILL.md workflow + LangGraph nodes using `repo-graph` and BM25 retrieval; mostly orchestration, not a new server.
**Complexity**: Medium
**Dependencies**: repo-graph skill, a BM25 index (rank-bm25 / Tantivy).

### web-search (grounded retrieval tool)

**Source**: Tool-MAD (multi-agent debate fact verification), arXiv:2601.04742 — combines static-corpus RAG with a live web search API.
**What it does**: A live web/search tool to fetch real-time facts the local corpus lacks; pairs with `citation-check` for grounding.
**Why for Labmate**: Fills the freshness gap for both coding (API/library docs) and writing (recent results). Keep it optional/configurable for offline operation.
**Implementation path**: TypeScript MCP server wrapping a search API (SearXNG self-hosted for local-first, or a commercial API).
**Complexity**: Low
**Dependencies**: SearXNG (self-host) or a search API.

---

## Interesting but Lower Priority

- **LATS / Language Agent Tree Search** (arXiv:2310.04406) and **RethinkMCTS** (arXiv:2409.09584): MCTS over reasoning+action for code gen. High quality but expensive (many LLM rollouts) — defer until single-GPU throughput is comfortable; LangGraph goal-tree covers the common case. https://arxiv.org/abs/2310.04406
- **OptiTree / HyperTree Planning** (arXiv:2510.22192, 2505.02322): hierarchical thought decomposition via tree search — relevant to the goal-tree decomposer, worth reading for design ideas, not a near-term skill.
- **ToolTree** (arXiv:2603.12740): dual-feedback MCTS for tool *planning* with bidirectional pruning — interesting if tool selection becomes a bottleneck.
- **ToolLLM/ToolBench** (arXiv:2307.16789) and **Gorilla/APIBench**, **AnyTool** (ICML 2024): foundational tool-use training/benchmarks. Useful as evaluation harnesses and for the neural API retriever idea; MCP already gives Labmate the tool-invocation substrate these predate.
- **GSAR: Typed Grounding for multi-agent hallucination detection** (arXiv:2604.23366): typed evidence grounding in ReAct loops — relevant if Labmate goes multi-agent.
- **Tool-MAD multi-agent debate** (arXiv:2601.04742): stronger fact verification via debate — heavier than CiteCheck/RefChecker; revisit if single-checker accuracy is insufficient.
- **AGONETEST** (arXiv:2511.20403) and **HITS** (ASE 2024): systematic test-gen benchmarking + method-slicing for coverage — adopt as evaluation methodology for `test-gen`.
- **Small-model agentic tool calling** (arXiv:2512.15943): targeted fine-tuning lets small models beat large ones at tool calls — relevant for squeezing the local 31B brain.

---

## Papers Worth Reading in Full

These have direct implementation value for the skills above:

1. **RepoGraph** — arXiv:2410.14684 — exact data structure + the `search_repograph` action design to copy for `repo-graph`. https://arxiv.org/abs/2410.14684
2. **PaperQA** — arXiv:2312.07559 — the agentic-RAG-over-papers loop that `paper-rag` should mirror; see PaperQA2 repo for the production pipeline. https://arxiv.org/abs/2312.07559
3. **RefChecker** — arXiv:2405.14486 — claim-triplet extraction + 3-way classification, the core of `citation-check`. https://arxiv.org/abs/2405.14486
4. **CiteCheck** — arXiv:2605.27700 — citation-specific grounding with the exact/minor/major taxonomy. https://arxiv.org/html/2605.27700v1
5. **Mutation-Guided Test Generation @ Meta (ACH)** — arXiv:2501.12862 — the surviving-mutant feedback loop and equivalent-mutant detection for `test-gen`. https://arxiv.org/abs/2501.12862
6. **Reflexion** — arXiv:2303.11366 (NeurIPS 2023) — the verbal-RL episodic-reflection loop that the `critique` skill is built on; pairs with grounding from RefChecker/CiteCheck. https://arxiv.org/abs/2303.11366
7. **Mem0** — arXiv:2504.19413 — extraction + adaptive self-editing memory update algorithm for the StorageManager. (See also memory survey arXiv:2512.13564.)
8. **Agentless** — hierarchical localization pipeline (40.67% SWE-bench Lite) — the staged file→function→edit workflow for `repo-fault-localize`.

---

## Sources

- RepoGraph: https://arxiv.org/abs/2410.14684 , https://arxiv.org/html/2410.14684v2
- Awesome-Repo-Level-Code-Generation: https://github.com/YerbaPage/Awesome-Repo-Level-Code-Generation
- ARISE: https://arxiv.org/html/2605.03117
- LARGER: https://arxiv.org/html/2605.16352
- PaperQA: https://arxiv.org/abs/2312.07559 , https://arxiv.org/html/2312.07559v2
- PaperQA2 repo: https://github.com/future-house/paper-qa
- Semantic Scholar API: https://www.semanticscholar.org/product/api/tutorial
- Open-source Agentic Hybrid RAG for literature review: https://arxiv.org/html/2508.05660
- MinerU / OmniDocBench (CVPR 2025): https://github.com/opendatalab/OmniDocBench
- READoc benchmark: https://arxiv.org/pdf/2409.05137
- RefChecker: https://arxiv.org/abs/2405.14486 , https://arxiv.org/html/2405.14486
- CiteCheck: https://arxiv.org/html/2605.27700v1
- GSAR: https://arxiv.org/html/2604.23366v1
- Tool-MAD: https://www.arxiv.org/pdf/2601.04742
- E2B: https://e2b.dev/
- Sandbox comparisons (Modal/Daytona/E2B): https://modal.com/resources/best-code-execution-sandboxes-ai-agents , https://www.zenml.io/blog/e2b-vs-daytona
- Mutation-Guided Test Generation @ Meta (ACH): https://arxiv.org/abs/2501.12862
- Test vs Mutant: https://arxiv.org/pdf/2602.08146
- AGONETEST: https://arxiv.org/pdf/2511.20403
- TestForge: arXiv:2503.14713
- LLM Unit Test via Property Retrieval: arXiv:2410.13542
- STORM: https://github.com/stanford-oval/storm
- ToolLLM/ToolBench: https://arxiv.org/abs/2307.16789
- Benchmarking LLM Tool-Use in the Wild: https://arxiv.org/html/2604.06185
- Reflexion (NeurIPS 2023): https://arxiv.org/abs/2303.11366 , https://papers.nips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html
- LATS: https://arxiv.org/pdf/2310.04406
- RethinkMCTS: https://arxiv.org/html/2409.09584v1
- OptiTree: https://arxiv.org/html/2510.22192
- HyperTree Planning: https://arxiv.org/html/2505.02322v1
- ToolTree: https://arxiv.org/html/2603.12740v1
- Mem0 / agent memory survey: arXiv:2504.19413 , arXiv:2512.13564
- Mem0 vs Zep vs Letta comparison: https://www.agenticwire.news/article/mem0-zep-letta-agent-memory
- Small Language Models for Agentic Tool Calling: https://arxiv.org/html/2512.15943v1
