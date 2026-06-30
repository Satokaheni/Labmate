# Brief: Lower per-turn token cost

> **Status: DEFERRED / FUTURE WORK.** Pick up after the local-execution work is merged +
> live-tested. Overlaps heavily with [[wire-ui-mode-to-behavior]] and the local-execution
> skill-gate (both prune what's advertised), so design them together. Brief to brainstorm into
> a spec later — NOT ready to execute.

## Problem (observed 2026-06-30, live)
A single "find where WebSocket auth is handled" turn consumed **~11% of the 131k window
(~14k tokens)** — for find→read→answer. Simple navigation queries should not cost that much.

## MEASURE FIRST (this is task 1 — don't optimize blind)
We currently have **no visibility** into where the tokens go: the context-window telemetry
attributes the WHOLE fill to `conversation` (`main.py` `_context_window`: `systemPrompt: 0`,
`skillInstructions: 0`, `conversation: ctx_used`). So the strip can't tell us the breakdown.

**Task 1 (engineering): real per-segment token accounting.** Attribute the measured fill to
actual segments (system prompt, tool schemas, skill catalog, tool results, reasoning) using the
tokenizer, and surface it in the existing context-strip segments. Only then do we know which
lever pays off.

## Task 2 (RESEARCH): survey + evaluate reduction techniques for the dominant segment
Once task 1 names the dominant cost (likely the skill catalog and/or tool results), this becomes
a **research task** — not just an engineering tweak. Investigate the state of the art for that
segment and evaluate candidates against Labmate's constraints (local Q4 model, byte-stable
prefix cache, latency budget) BEFORE committing to an implementation. Output a short research
report (fits `research/llm-harness-research/`) recommending an approach with before/after numbers.

Research directions by what dominates (pick after task 1):
- **If the skill catalog dominates** — dynamic / retrieval-based tool advertising (embed the 29
  skill descriptions, advertise only top-k relevant to the goal), deferred tool loading + a
  tool-search tool (the Claude Code `defer_loading` + tool-search MCP pattern), learned/cheap
  routing to a skill subset, and how each interacts with prefix-cache determinism.
- **If tool results dominate** — context compression / summarization (e.g. LLMLingua-style prompt
  compression), symbol/function-scoped extraction vs raw head+tail, structured (AST-aware) reads,
  and de-duplicating content already in context.
- **General** — context distillation, retrieval-augmented tool/skill selection, and the
  accuracy/recall cost of each (a technique that drops 40% of tokens but misses the right skill
  10% of the time is a net loss).

Evaluate each candidate on: **token reduction %**, **correctness/recall impact** (does it still
find the right code/skill), **latency on the Q4 host**, and **implementation + prefix-cache
cost**. Recommend one, then spec + implement it.

## Suspected culprits (hypotheses — confirm with task 1)
- **The skill catalog.** All **29 skills** are advertised (name + description) in the prefix on
  EVERY turn, even a pure file-navigation task that uses none of them. Likely the biggest fixed
  cost (~1–4k tokens, to be measured). `BASE_SYSTEM_PROMPT` is ~1925 chars (~500 tokens) on top.
- **Full-file reads.** The model `read_file`'d the entire `auth.py` to show ONE function. Tool
  results are budgeted to `LABMATE_TOOL_RESULT_BUDGET=16000` chars head+tail — a single read can
  add ~4k tokens. The `search_files` hit already had the line; the full read may be redundant.

## Candidate levers (prioritize by task-1 measurement)
1. **Prune the advertised skill catalog.** Don't advertise all 29 skills every turn. Options:
   by mode (Coding → code skills only — see [[wire-ui-mode-to-behavior]]), by client-attach
   (file work needs few/no skills — overlaps the local-execution skill-gate), or a cheap
   relevance pre-pass. **Biggest suspected win**, and it composes with the steer/manifest work.
2. **Progressive skill disclosure (Claude Code's model).** Keep the catalog to name + ONE-line
   description; load the full `SKILL.md` body only on `load_skill` (verify the current
   `catalog_prompt()` isn't already bloated). Resources never enter the prefix.
3. **Smarter reads.** Have `search_files` return enough surrounding snippet that a full
   `read_file` is often unnecessary; add a line-range option to `read_file`; avoid re-reading a
   file already in context. Function/symbol-scoped reads instead of whole-file.
4. **Tune `LABMATE_TOOL_RESULT_BUDGET`.** 16k head+tail is generous; consider a smaller default
   for read/search results, or symbol-scoped extraction over raw head+tail.
5. **Reasoning/thinking budgets per node.** `ASSESS_THINKING_BUDGET` etc. already trim decode;
   audit whether any node over-thinks a cheap decision (affects cost, not window fill).

## Decisions for the spec
- Measure-first: task 1 gates everything. Don't ship a lever without before/after numbers.
- Catalog pruning: soft (relevance-ranked subset) vs hard (mode/attach filter). Must keep the
  per-goal prefix **deterministic / byte-stable** (prefix cache) — the pruned set is resolved
  once at goal start, like the manifest.
- Composition: catalog pruning, the mode hint, and the local-execution skill-gate all touch
  "what's advertised" — unify them, don't stack three independent filters.

## Acceptance (rough)
- The same find→read turn costs materially less (target: well under half the ~14k) with no loss
  of correctness; per-segment telemetry shows the breakdown in the context strip.

## Flow & dependencies
1. **Task 1 — measure** (engineering): per-segment token accounting.
2. **Task 2 — research** the dominant segment: survey + evaluate techniques → short report with
   before/after numbers and a recommendation.
3. **Spec** the recommended lever (brainstorm → spec).
4. **Implement + verify** the before/after numbers hold.

Do AFTER local-execution Phase 1/2. Co-design with [[wire-ui-mode-to-behavior]] and the
skill-gate — all three touch *what gets advertised to the model*. See
[[project-local-execution-surface]].
