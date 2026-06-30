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

**Task 1: real per-segment token accounting.** Attribute the measured fill to actual segments
(system prompt, tool schemas, skill catalog, tool results, reasoning) using the tokenizer, and
surface it in the existing context-strip segments. Only then do we know which lever pays off.

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

## Dependencies / ordering
After local-execution Phase 1/2. Co-design with [[wire-ui-mode-to-behavior]] and the
skill-gate. See [[project-local-execution-surface]].
