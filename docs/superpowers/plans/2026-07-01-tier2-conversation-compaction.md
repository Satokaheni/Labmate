# Brief: Tier 2 — Rolling-summary conversation compaction

> **Status: DEFERRED / FUTURE WORK.** Pick up AFTER Tier 1 (bounded recent-turn window —
> the immediate conversation-continuity fix) is merged, and after / alongside
> [[project-token-cost-reduction]] (its Task 1 per-segment token accounting gives the
> measurement that triggers Tier 2). Brief to brainstorm → spec → implement later — NOT
> ready to execute. Builds on the existing `/compact` path + `2026-06-25-smarter-compaction.md`.

## Problem
Tier 1 seeds a **bounded window of recent turns** (last ~N, final-answers only) into the
model context so a follow-up like "is *it* NP-complete?" sees the prior turn. That fixes the
"Which problem?" amnesia cheaply and with flat per-turn cost. But it has a hard horizon: once a
session runs longer than the window, older turns fall off and long-range context is lost. Tier 2
adds a **rolling summary of the evicted (older) turns** so long-range memory survives without
carrying the full transcript verbatim (which would blow the window + prompt-eval cost — see the
7112-token-prompt / 11%-of-window observations in [[project-token-cost-reduction]]).

## The shape (grounded in the hermes + openclaw harnesses)
Both reference harnesses converge on the SAME structure — a **hybrid**, never pure-summary,
never summarize-every-turn:

- **hermes** (`agent/context_compressor.py`): carries the full transcript, compresses only at
  **~60% of the context window**: protect the **head** (system + first ~3 exchanges) and the
  **tail** (last ~20K tokens) *verbatim*, and **summarize only the middle** into a structured,
  *iteratively-updated* summary (re-summarize builds on the prior summary, doesn't start over),
  bounded 2K–12K tokens, with a `SUMMARY_PREFIX` "this is background — respond to the message
  below, not the summary" marker. Orphaned tool-calls cleaned up.
- **openclaw** (`src/agents/compaction-planning.ts`, `compaction.ts`): two tiers — a cheap
  bounded recent **window** (Labmate's Tier 1) feeding an agent-level **token-budget
  compaction** (`maxHistoryShare ≈ 0.5 × window × safety-margin`): drop oldest *chunks*,
  **summarize them as a system-role prefix**, keep recent verbatim. On-demand by default;
  automatic only in a "safeguard" mode.

**Three invariants both agree on (adopt all three):**
1. **Recent turns always stay verbatim** — the summary is additive for *older* content only.
2. **Summarization is threshold-gated, never per-turn.**
3. **The summary is a labeled background block**, not mixed into the live turn.

## Design for Labmate (hybrid, threshold-gated)
On a session whose recent-window + summary would exceed a token threshold:
1. **Keep verbatim:** the Tier-1 recent window (tail) + optionally the first user turn (head/goal anchor).
2. **Summarize the evicted middle** into a structured running summary (task snapshot / decisions
   / open asks / facts established), *iteratively updated* from the prior summary.
3. **Prepend the summary as a labeled background block** (system or a marked user message) BEFORE
   the recent window, AFTER the byte-stable system+tools prefix.
4. Persist the running summary per session (alongside `chat_turns`) so it survives restarts and
   is reloaded with the window.

## ⚠️ Labmate-specific constraint the references do NOT share
Hermes and openclaw summarize with a **cheaper/faster auxiliary model**. **Labmate is
single-model** — Gemma 4 31B (Q4) on one GPU, `QWEN_BASE` defaults to `GEMMA_BASE`. There is **no
cheaper model**: a summary is a **full Gemma generation** at ~39 tok/s. That makes the
"threshold-gated, never per-turn" rule **non-negotiable** here — a per-turn summary would
re-introduce exactly the decode-latency tax measured in the token-cost work (a routing call alone
cost ~46s). So: summarize rarely (on threshold crossing), bound the summary tightly (small
`max_tokens` + `thinking_budget`), and update it iteratively so each compaction is cheap.

## Reuse, don't rebuild
- **`/compact`** is already wired end-to-end (ws_gateway `compact` handler → goals queue →
  `compact.done`; frontend `/compact` command shipped in the frontend-session PR). Tier 2's
  *manual* path is this; Tier 2 adds the *automatic, threshold-gated* trigger + the rolling-summary
  policy.
- **`2026-06-25-smarter-compaction.md`** — CONFIRM its implementation status first (audit); if it
  already produces a summary, Tier 2 is "gate it on a token threshold + make it iterative +
  persist it," not a new summarizer.
- **Prefix-cache stability** (`prompt_assembler.py`) — the summary+window go AFTER the stable
  prefix; the prefix must stay byte-identical so llama.cpp keeps the cache.
- **Per-segment token accounting** ([[project-token-cost-reduction]] Task 1) — this is the meter
  that decides WHEN to compact; do that measurement first so the threshold is real, not guessed.

## Open decisions for the spec
- Threshold: % of window (hermes 60%) vs an absolute token budget vs `reserveTokens`-style.
- Recent-window size (shared with Tier 1) + summary token ceiling (hermes 2K–12K; scale down for the Q4 budget).
- Summary structure (freeform vs hermes' sectioned snapshot) + iterative-update prompt.
- Where the summary lives (system message vs marked user block) and how it interacts with the doc-skill / manifest prefix.
- Tool-call/tool-result handling in evicted turns (drop vs summarize) + orphan repair.
- Trigger: preflight (before the call) and/or post-overflow (hermes does both).

## Acceptance (rough)
- A long session retains long-range context (a fact from 30 turns ago is still answerable) at
  **flat, bounded** per-turn token cost, with the summary generated only on threshold crossings
  (measurable: N compactions ≪ N turns), and no prefix-cache invalidation.

## Dependencies & sequencing
1. **Tier 1** (bounded recent window) merged — the foundation Tier 2 extends.
2. **[[project-token-cost-reduction]] Task 1** (per-segment token accounting) — the trigger meter.
3. Confirm **smarter-compaction** status (reuse vs build).
4. Brainstorm → spec → implement + verify (flat cost + long-range recall before/after).
