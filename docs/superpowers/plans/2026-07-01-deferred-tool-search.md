# Deferred Tool-Search — Token-Cost Reduction Implementation Plan

> **Fork note:** This is the **"prefix-cache IS reused"** branch of the token-cost research
> (`research/llm-harness-research/token-cost-reduction-findings.md`). It is a NO-GO until the live
> llama-server check confirms the Gemma prefix cache is actually reused. If it is NOT reused, most
> of this still helps (it cuts raw tokens too), but the cache-safety constraints below relax and
> dynamic per-turn pruning becomes fair game.
>
> **For agentic workers:** CLAUDE.md Implementation Workflow (Haiku implements → Opus judges).
> Measure-first: every phase re-runs `measure_prompt_segments` (already shipped, PR #27) to report
> real before/after token numbers, and the routing eval to catch recall regressions.

**Goal:** cut the fixed prefix (`tool_schemas` 52% + `skill_catalog` 37% of a ~7k prompt) with a
cache-safe **deferred tool-search bridge**, mirroring the hermes-agent + openclaw mechanics — so
capability schemas/descriptions flow through the message TAIL (as tool results), NEVER into the
byte-stable prefix.

**Architecture:** Add 3 static bridge tools to the model-visible set — `find_tools(query)`,
`describe_tool(name)`, and reuse the existing `call_skill_tool`/`load_skill` for dispatch — and
REMOVE the deferrable tool schemas + (phase 2) the full skill catalog from the prefix, replacing
them with a searchable catalog assembled once per goal. The visible `tools=` array becomes
{core tools + bridge tools} and is byte-identical across turns; search results and loaded schemas
are returned as tool-call results in the tail.

## The one invariant (from the harness mechanics)
**Loading a capability must never mutate the visible `tools=` array or the system-prompt catalog
region.** hermes `tool_describe` returns `json.dumps({name, description, parameters})` as a RESULT
(`tools/tool_search.py:632-657`); openclaw does the same (`tool-search.ts:1748-1752`) and pins the
tool region ahead of a `SYSTEM_PROMPT_CACHE_BOUNDARY` (`system-prompt-cache-boundary.ts:8-66`).
Labmate's `PromptAssembler.system_message()` + `tools()` are the cached prefix; they must stay
byte-stable per goal. Everything a `find_tools`/`describe_tool` call surfaces goes into the
`{"role":"tool", ...}` message, i.e. the tail.

## Labmate mapping (what is core vs deferrable)
Current prefix (from `services/orchestrator/tool_manifest.py::build_tool_list` +
`PromptAssembler`):
- **CORE tools — always visible (never deferred):** `read_file`, `write_file`, `list_dir`,
  `search_files`, `run_tests`, `run_bash`, `finish`, `call_skill_tool`, `load_skill`.
- **Deferrable tools (tool_schemas):** the hosted MCP tools (`mcp__codegraph__*`, ~8), plus
  `code_semantic_search` and `memory_search` when present.
- **Deferrable catalog (skill_catalog):** the 29-skill `catalog_prompt()` name+description menu.
  NOTE: skills are ALREADY progressive (body loads on `load_skill`); the 2.6k is just the routing
  MENU. Deferring the menu trades tokens for a routing-search hop → **recall-gated (phase 2).**

## Phased delivery (lowest-risk first; each phase measured)

### Phase 1 — Deferred tool-search for the auxiliary/MCP tools (safe, no routing-recall risk)
Defer the non-core tools (MCP + `code_semantic_search` + `memory_search`) behind the bridge;
keep the core tools and the full skill catalog exactly as-is. This proves the cache-safe machinery
on the low-risk pool before touching skill routing.

- **New module `services/orchestrator/tool_search.py`:** a `ToolCatalog` (index name +
  description + top-level param names; BM25 or a simple tokenized-substring scorer — copy hermes
  `tool_search.py:347-418` shape) built from the DEFERRABLE tool schemas; and the two bridge tool
  schemas `find_tools`/`describe_tool` (terse — "every byte here is paid every turn").
- **`build_tool_list` change:** partition into `visible = core + bridge` vs `deferred`; advertise
  only `visible`. Resolve the partition ONCE per goal (cache-safe). An activation gate
  (`ENABLE_TOOL_SEARCH`, and a threshold like hermes' 10%-of-context or "always on" for local)
  decides whether to defer at all — when off/below-threshold, behavior is byte-identical to today.
- **Dispatch in `_run_react_loop`:** handle `find_tools(query)` → return top-k catalog matches as a
  tool result; `describe_tool(name)` → return the full schema as a tool result. Actual execution
  still goes through the EXISTING tool dispatch (a described MCP tool is called by its real name,
  which the model now knows from the result) — OR add a thin `call_tool(name,args)` bridge if the
  model can't call a name that isn't in `tools=`. **Decide this explicitly:** many local models
  WON'T emit a tool_call for a name absent from `tools=`, so a `call_tool` bridge (hermes
  `tool_call`) is likely required — mirror `tool_search.py:680-710`.
- **Prefix-cache:** `find_tools`/`describe_tool`/`call_tool` are in the byte-stable `tools=`;
  results are tail. Re-verify `prefix_fingerprint()` is identical across turns.
- **Measure + gate:** re-run `measure_prompt_segments` (expect `tool_schemas` drop by the deferred
  pool); run the live skill-execution smoke (§11) + the routing eval — Phase 1 must NOT change
  routing accuracy (it doesn't touch the skill catalog).

### Phase 2 — Defer/compress the skill catalog (bigger win, RECALL-GATED)
The 2.6k skill menu is the biggest single deferrable. Three options, in ascending
savings/risk — pick per the routing eval:
1. **Terse catalog** (cache-safe, lowest risk): shorten each skill line to name + an ultra-terse
   1-line, or names only, moving full descriptions behind `load_skill`/a `describe_skill` result.
   openclaw "directory" mode shape (names visible, detail deferred).
2. **Static mode/attach subsetting** (cache-safe): advertise only the skills for the active
   mode/client-attach, resolved once per goal. Reuses the existing mode signal.
3. **`find_skills(query)` bridge** (highest savings, highest recall risk — the "99% paradox"):
   remove the menu, add a skill-search bridge; the model searches → `load_skill`. Only ship this
   if the routing eval holds.
- **Hard gate:** run `eval/run_routing_eval.py` (existing) before/after. Acceptance: **no skill's
  routing accuracy drops > 0.05** (the repo's standard). If a variant regresses recall, fall back
  to the next-safer option. This is the make-or-break metric — deferring the menu is a NET LOSS if
  the model stops finding the right skill.

## Global constraints
- **Cache-safety is the invariant:** the `tools=` array + system-prompt tool/catalog region stay
  byte-stable per goal; loaded detail is tail-only. Add a test asserting `prefix_fingerprint()` is
  unchanged across turns with tool-search ON.
- **Off by default / flag-gated** (`ENABLE_TOOL_SEARCH=0`) → behavior byte-identical to today;
  no regression when off. Local-model default can flip on (openclaw `local-model-lean`).
- **Deterministic serialization** (stable JSON key order) so the deferred partition + catalog are
  byte-stable — non-deterministic ordering silently breaks the prefix.
- Bridge-tool names reserved; core tools hardcoded and never deferred.
- No `print`/stdout; best-effort (a catalog/search failure falls back to advertising the tool
  normally, never breaks the loop).

## Acceptance
- `measure_prompt_segments` shows a real `tool_schemas` (+ phase-2 `skill_catalog`) reduction on a
  live turn, with `prefix_fingerprint` still stable across turns (cache intact).
- Routing eval: no skill drops > 0.05 (phase 2 gate).
- Live skill-execution smoke (§11) green — deferred tools/skills still callable end-to-end.
- The felt-latency win is bounded/honest: if the cache is reused this buys window+VRAM, so stop
  after the measured reduction plateaus rather than chasing the last few hundred tokens.

## Sequencing vs the other backlog items
Per the research verdict, the **routing-latency runaway** (the ~3.8k-token pre-flight generation)
is higher felt-latency value than shrinking a cached prefix — consider doing that first/in parallel.
This plan is the window/VRAM play; ship Phase 1 (safe) regardless, gate Phase 2 on the routing eval.
