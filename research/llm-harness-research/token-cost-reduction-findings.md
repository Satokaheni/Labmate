# Token-Cost Reduction — Research Findings (Task 2)

> Research phase of `docs/superpowers/plans/2026-06-30-token-cost-reduction.md`, driven by the
> live per-segment measurement (2026-07-01) and a cross-harness observation of hermes-agent +
> openclaw, plus a 12-month web-search supplement. Produced via the `/research` skill.

## The measurement that triggered this
A navigation-query turn ships a ~6,975-token prompt of which **~90% is fixed scaffolding**:

| Segment | Tokens | % |
|---|---:|---:|
| tool_schemas | 3,650 | 52.3% |
| skill_catalog | 2,604 | 37.3% |
| system_base | 689 | 9.9% |
| continuity | 26–364 (accumulates) | <5% |
| conversation | 6–20 | ~0% |
| tool_results | 0 (nav) … dominant on file reads | variable |

Actual task content is ~32 tokens. `tool_schemas` + `skill_catalog` = **89.6%**.

## The crux (and its empirical confirmation)
Labmate's `PromptAssembler` builds a **byte-stable** system+tools prefix so llama.cpp reuses the
KV cache — it only re-evals the ~167 NEW tokens per turn, not the 7k. So **the fixed block is
latency-cheap *while cached*; the payoff of shrinking it is window headroom + KV-cache VRAM, NOT
latency.** ANY technique that makes the prefix VARY per turn breaks the cache → full ~7k re-eval
every turn.

The web search confirmed this empirically: *"Don't Break the Cache"* (arXiv:2601.06007) shows
caching dynamic tool defs/results is **net-negative TTFT** vs a stable system-prefix-only cache.
So two of our candidate levers are confirmed **counterproductive on a single local Q4 host**:
dynamic top-k catalog pruning, and LLMLingua-style compression of a cached prefix.

## ⚠️ PREREQUISITE — verify the premise on OUR host FIRST
The search flagged that **Gemma-class shared-KV (SWA) architectures sometimes log
`"cache reuse not supported"` in llama.cpp** even with flash-attention. Labmate's primary model
IS Gemma 4 31B. **The entire byte-stable-prefix strategy is conditional on the cache actually
being reused on our build.** Check before committing to anything below:
```bash
grep -iE "cache|slot|reuse|swa|prefix" .data/logs/llama-server.log | tail -40
# On the 2nd identical request, confirm a non-zero cached prefix in the /completion timings
# (cache_n / prompt_n / "cached"). If cache_n stays ~0, the prefix is NOT being reused.
```
- **Cache IS reused** → optimize for window/VRAM with the **cache-safe** levers; avoid prefix-breaking ones.
- **Cache is NOT reused** → byte-stability is moot; just cut raw token count (terse schemas +
  defer-loading + code-mode), and dynamic per-turn pruning becomes fair game.

## What the reference harnesses do (both converge)
| | hermes-agent | openclaw |
|---|---|---|
| Tool list | Full set, **built once/session**, passed by reference every turn | Full set, **built once/run**, frozen |
| Cache stability | Deliberate — "owns the prompt-cache contract"; MCP refresh gated to turn boundaries, no-op on no change | Deliberate — MCP tools **sorted by name** for byte-stability; explicit `SYSTEM_PROMPT_CACHE_BOUNDARY` splitting cached prefix from dynamic suffix |
| Deferred loading | **Yes** — `tool_search`/`tool_describe`/`tool_call` bridge; defers MCP + non-core tools when they exceed ~10% of context; core tools never deferred | **Yes** — `tool-search.ts` with 3 modes (`tools`/`code`/`directory`); **system prompt lists tool NAMES only**, defers full JSON schemas until `tool_describe` |
| Schema verbosity | Native verbose; **bridge schemas deliberately terse** ("every byte added here is a byte the user pays on every turn") | Terse TypeBox; per-provider projection |
| Local-model handling | threshold-gated | **`local-model-lean`: auto-enables tool-search (limit 5) + denies high-latency tools** |

**Both** keep the model-facing tool block byte-stable within a session, and **both** put the
big win behind a **deferred tool-search tier**. openclaw's *names-only system prompt* + *local-model
preset* are the most direct templates for Labmate (Labmate IS a local-model, skill-catalog harness).
hermes's issue #18074 and openclaw's issue #20430 (volatile metadata in the prefix invalidating the
KV cache) are live evidence of the exact traps to avoid.

## Ranked levers (cache-aware)
Fields: **savings** (of its target segment), **recall risk**, **cache impact**, **decode vs prompt
time**, **what it buys**.

### Cache-SAFE cluster (recommended)
| Lever | Target | Savings | Recall risk | Cache | Buys |
|---|---|---:|---|---|---|
| **Deferred tool-search tier** (names/core visible; schemas loaded on demand) — hermes+openclaw | tool_schemas | high (defer the bulk) | low–med (extra hop; both harnesses ship it) | SAFE (visible set stable/session) | window + VRAM |
| **Names-only skill catalog** (name + 1-line; load body on `load_skill`) — openclaw pattern; verify current `catalog_prompt` | skill_catalog | med–high | low (already the load_skill model) | SAFE | window + VRAM |
| **Mask, don't remove** (logit-bias / `allowed_tools` gating at DECODE time) — Manus | tool_schemas (behavioral) | 0 prefix (behavioral) | low | **SAFE** (prefix untouched; decode-time) | correctness/routing, not tokens |
| **Terse schemas + Tool-Use-Examples** (strip param prose, add 1–2 examples: 72%→90% acc) | tool_schemas | med | **improves** recall | SAFE (static) | window + VRAM |
| **TERSE Tool Catalog reformat** (WHEN/ERR/TAGS, drop machine verbosity: ~66% w/ better routing) | skill_catalog | high | improves | SAFE | window + VRAM |
| **Static subsetting by mode/attach** (resolve ONCE/session) | both | med | low | SAFE (per-session) | window + VRAM |
| **Deterministic serialization** (stable JSON key order) | — | — | — | **PREREQUISITE** (non-det key order silently breaks the prefix) | protects every other lever |
| **Move volatile fields to prefix TAIL** (no ids/timestamps in system prompt) — openclaw #20430 | system_base | small | none | SAFE (protects cache) | protects cache |
| **llama.cpp slot pinning** (`--slots`, `id_slot`, pre-warm) | — | — | — | operational | realizes the savings |

### tool_results (the variable cost — cache-safe, append-only)
| Lever | Savings | Recall risk | Cache | Notes |
|---|---:|---|---|---|
| **Code-mode / programmatic tool calling** (results stay in sandbox; only summary enters context: 37%) | high on read-heavy | low | neutral (surface fixed) | biggest variable-cost win; openclaw has `code` mode |
| **Smarter reads** (line-range/symbol-scoped read_file; search returns snippet) | high | low | SAFE (tail) | fixes the original 14k full-file-read case |
| **Append-only observation masking** (elide old tool bodies, keep tool+args + all errors) | med | med (over-masking hurts — arXiv:2606.00408) | SAFE **iff append-only** | never rewrite cached history |
| **TOON serialization** for tabular results (lossless 30–60%) | med | model-familiarity caveat (Gemma may not parse well) | SAFE (tail) | result-side only |

### BREAKING — avoid on this host (confirmed net-negative)
| Lever | Why avoid |
|---|---|
| Dynamic top-k catalog pruning (per-query relevance) | Varies the prefix → full re-eval every turn; and RAG-MCP recall trap (arXiv:2605.18857: retrieval@k ≠ rank-1) |
| LLMLingua compression of the CACHED prefix | Lossy AND breaks byte-stability → net latency loss |
| Summarize/prune of cached history mid-prefix | Rewrites the cached region |

## Recommendation
**Gate on the prerequisite check above.** Assuming the Gemma prefix cache IS reused:

1. **Biggest, lowest-risk win — deferred tool-search + names-only catalog.** Copy the
   hermes/openclaw pattern: advertise a small stable CORE set + `tool_search`/`describe`/`call`
   (or a `load_skill`-style bridge), defer the full tool schemas and the 29-skill bodies. This
   attacks both the 52% and the 37% while staying byte-stable (the visible core is fixed per
   session). Adopt openclaw's **local-model-lean** posture as the default.
2. **For any per-turn "only relevant tools" behavior — mask, don't remove.** Keep all defs in the
   cached prefix; gate with decode-time logit masking. This is the ONLY cache-safe way to get
   dynamic-subsetting behavior. Do NOT rebuild the prefix per query.
3. **Terse schemas + tool-use-examples + TERSE-catalog reformat** — recall-improving, not just
   savings; safe to combine with (1).
4. **tool_results (do this regardless of the cache verdict):** smarter reads + code-mode +
   append-only masking. This is the variable cost that actually spikes the window on real work
   (the original 14k case), and it's largely cache-neutral.
5. **Guardrails:** deterministic serialization; audit the system prompt for volatile tokens; pin a
   llama.cpp slot.

## Honest "is it even worth it?" verdict
- **If the cache is reused (likely-but-VERIFY):** the fixed 7k is already latency-cheap, so the
  *latency* payoff of shrinking it is small — the real wins are **window headroom** (7k of 131k =
  5.3% before any work) and **KV-cache VRAM**. That's worth a **bounded** effort — do (1) deferred
  loading (high leverage, both harnesses prove it works) and (4) tool_results, and STOP. The earlier
  **routing-latency fix (the 3.8k-token runaway pre-flight call) is higher-value for felt latency**
  than shrinking a cached prefix.
- **If the cache is NOT reused on Gemma:** priorities flip — the 7k is paid in full every turn
  (latency + window), so raw token cuts (terse schemas + defer-loading + code-mode) become
  **high-value for latency too**, and dynamic pruning is no longer off-limits.

**Bottom line:** verify the cache first; then ship deferred tool-search + names-only catalog
(cache-safe, harness-proven) + smarter reads/code-mode for tool_results; skip dynamic top-k and
prefix compression. Don't over-invest in shrinking a prefix the cache already makes cheap.

## Sources
Anthropic advanced tool use (defer_loading, programmatic tool calling 37%, tool-use-examples
72→90%); *Don't Break the Cache* (arXiv:2601.06007); Manus context-engineering (mask-don't-remove,
append-only, deterministic serialization); RAG-MCP (arXiv:2505.03275) + 99% Success Paradox
(arXiv:2605.18857); MCP-Zero (arXiv:2506.01056); TERSE Tool Catalog; TOON (arXiv:2603.03306 /
2605.29676); Masking Stale Observations (arXiv:2606.00408); llama.cpp KV-cache-reuse discussions
(#13606, #8860); openclaw #20430; hermes-agent #18074. Code: hermes `model_tools.py`,
`tools/tool_search.py`, `agent/turn_context.py`; openclaw `src/agents/tool-search.ts`,
`local-model-lean.ts`, `system-prompt.ts`, `agent-bundle-mcp-materialize.ts`,
`system-prompt-cache-boundary.ts`.
