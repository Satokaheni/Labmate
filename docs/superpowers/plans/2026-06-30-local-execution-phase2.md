# Local-Execution Phase 2 — Implementation Plan (client MCP/skill host + CodeGraph)

> Continues `2026-06-30-local-execution-impl.md` (Phase 0+1 = PR #22). Branch:
> `feat/local-execution-phase2`. Monorepo. Build loop: Haiku implements → Opus judges (backend
> AND frontend) → fix until pass; phase close = Opus project review + react-doctor if React touched.

**Goal:** the frontend becomes a **local MCP host + `SKILL.md` discovery** surface (the
"install superpowers-style plugins locally" experience), and serves **client-side semantic
search**. The backend already routes any client-declared `mcp`/`skill` tool via the Phase-0
manifest seam — so most of P2-A is frontend.

## Key design decisions (locked)
- **Use `@modelcontextprotocol/sdk` `Client` + `StdioClientTransport`** in the Electron main
  process to spawn + speak to local stdio MCP servers. The TS skills are already MCP stdio
  servers (`Server` + `StdioServerTransport`, `node dist/index.js`) — same SDK both sides.
- **First slice hosts the bundled TS skills** (`ast-ts-refactor`, `component-doc-gen`,
  `a11y-audit`) as built-in MCP servers, pointed at `services/skills/<name>/dist/index.js`. A
  user-facing "add MCP server" config UI is a FOLLOW-UP (like ripgrep bundling). Prod packaging
  of the skill `dist/` is also a follow-up; dev runs from the repo path.
- **Namespacing** `mcp__<server>__<tool>` — already in the Phase-0 contract; advertise + dispatch
  derive from the same manifest (can't drift).
- **Security:** the bundled skills are first-party/trusted. User-added servers need a trust gate —
  deferred with the config UI.

---

## P2-A — frontend as a local MCP host (the focus)

### T2.1 [frontend] Minimal MCP stdio client
- Add `@modelcontextprotocol/sdk` to `services/frontend` deps.
- `electron/mcp-host.ts`: a `McpHost` that, given a server spec `{name, command, args, cwd?}`,
  spawns it via `StdioClientTransport`, `connect`s an SDK `Client`, lists tools (`tools/list`),
  and calls a tool (`tools/call`). Lifecycle: `start()`, `listTools()`, `callTool(tool, args)`,
  `stop()`. Errors (server won't start, tool error) surface as structured results, never crash.
- **Tests:** spin up ONE real bundled skill (`node services/skills/ast-ts-refactor/dist/index.js`)
  in a test, assert `listTools()` returns its advertised tools and `callTool` round-trips; a
  bad spec → clean error. (Gate behind a "dist present" check so CI without a build SKIPS.)

### T2.2 [frontend] Host registry + tool collection
- A built-in server registry (the 3 TS skills → their `dist/index.js`; skip any whose `dist` is
  missing). Spawn all on app ready; collect each server's tools as **namespaced** descriptors
  `{name: mcp__<server>__<tool>, source:'mcp', namespace:<server>, schema:<the tool's input schema as an OpenAI tool object>}`.
  Expose `getMcpToolDescriptors()`. Stop all servers on app quit.
- **Tests:** registry collects + namespaces tools from a fake/real server; a missing-`dist`
  server is skipped, not fatal.

### T2.3 [frontend] Wire MCP tools into the manifest + dispatch
- `capabilities.ts` becomes dynamic: `capabilitiesFrame()` = builtins + `getMcpToolDescriptors()`
  (so the frame the client sends after auth includes the hosted MCP tools, with schemas).
- `tool-executor.ts` `executeTool`: when `name` starts with `mcp__`, route to
  `mcpHost.callTool(...)` (resolve server+tool from the namespaced name); builtins unchanged.
- **Tests:** an `mcp__ast-ts-refactor__<tool>` call routes to the host; the frame carries the
  MCP descriptors; builtins still work.

### T2.4 [backend] Harden + cover mcp routing
- Apply the Phase-0 review nit: in `build_tool_list`, only advertise an `mcp`/`skill` descriptor
  when it carries a valid `schema` with a `function` (the schema-less guard); keep
  `manifest_local_tool_names` in sync.
- **Tests:** a schema-carrying `mcp` tool is advertised (namespaced) AND in the dispatch set; a
  schema-less one is in neither.

### T2.5 [integration / live] Host one skill end-to-end
- With the 3 skills hosted, confirm the model can call e.g. `mcp__ast-ts-refactor__<tool>` and it
  executes on the client. Live test on RunPod. **Stop point for Phase-2-A live test.**

---

## P2-B.0 — hosted-skill robustness (DONE 2026-06-30) — from P2-A live testing

Live-testing the P2-A MCP host surfaced three failure modes when the model drove the hosted
`ast-ts-refactor` skill. All three fixed (Haiku→Opus-judge, all **PASS**); suites green
(frontend **231**, backend tool_manifest+prompt_assembler+skill_runner **115**).

| Task | Fix | Commit |
|---|---|---|
| **T-B0.1** dispatch-side rooting | `electron/mcp-path-rooting.ts::resolveMcpPathArgs` — the `mcp__` dispatch branch in `main.ts` now resolves path-typed args (`PATH_ARG_KEYS`) to **absolute** against the workspace root before calling the host. Fixes the live `tsconfig must be an absolute path` error regardless of what the model passes. | `93873a6` |
| ↳ coverage extension | Added `component_path`/`dir_path` (component-doc-gen) + `html_or_component_path` (a11y-audit) to `PATH_ARG_KEYS` — the other two bundled hosts also enforce absolute paths. | `4106260` |
| **T-B0.2** pod/hosted skill dedup | `hosted_skill_namespaces(manifest)` + an `exclude` param on `SkillRunner.catalog_prompt`/`tool_schema`, threaded from the manifest in `PromptAssembler` + `build_tool_list`. A client-hosted skill is dropped from the pod `load_skill` catalog so the model no longer sees it two ways (killed the load_skill/code-sandbox thrash). **No-client prefix byte-identical** (prefix-cache safe; proven by identity tests + mutation testing). When all pod skills are hosted, `load_skill`+`call_skill_tool` are omitted together. | `10cb1c1` |
| **T-B0.3** namespacing hardening | `parseNamespacedTool(name, knownServers)` — `McpHostManager.callTool` parses `mcp__<server>__<tool>` by **longest-prefix match against registered server names** instead of a fragile regex, so tool names with underscores (`find_references`) and server names with underscores can't mis-split. Error contracts (`Invalid namespaced tool name` / `Unknown MCP server`) preserved. | `7a24b91` |

**Live-validated 2026-06-30** ✅ — prompt "find all references to `capabilitiesFrame` … (tsconfig is
services/frontend/tsconfig.json)" drove `mcp__ast-ts-refactor__find_references` with the **relative**
tsconfig; dispatch rooted it to absolute, the hosted skill executed, real refs returned
(`capabilities.ts:27` def + `useLabmateWS.ts` call sites). No abs-path error, no load_skill/code-sandbox
thrash. **P2-B.0 closed.**

## P2-B — local `SKILL.md` discovery + hosted-skill auto-routing (after P2-A merges)

### P2-B.1 — `SKILL.md` discovery (the documentation model)
Frontend discovers `SKILL.md` files (frontmatter name/description) in the workspace / a skills
dir; declares them as `source:'skill'` in the manifest. The orchestrator advertises the metadata
in the `load_skill` catalog; on `load_skill(name)` the SKILL.md **body** is returned and the model
uses the local primitives (read/write/search/run_tests) the skill describes — **no skill runtime on
the client** (the documentation model). Reclassify the Python repo-reading skills (supersede via
client primitives, or treat as candidates for a future client runtime); keep content+model skills
server-side fed client content.

**Locked design — body delivery (no round-trip, prefix-safe):**
A documentation skill is a manifest descriptor:
```
{ name: string, source: 'skill', description: string, body: string }   // NO namespace, NO schema
```
- `description` → the `load_skill` catalog line + enum (the routing signal). Deterministic per
  client, so prefix-stable.
- `body` → the full SKILL.md text, returned ONLY when `load_skill(name)` fires (enters the message
  stream, NEVER the prefix). The manifest is a one-time connect frame, so carrying the body there is
  free in prefix-cache terms — **no body round-trip needed**.
- Schema-less `source:'skill'` ⇒ `_is_usable_descriptor` already returns False ⇒ NOT advertised as a
  flat tool. Consistent split: schema-carrying mcp/skill = flat tool (P2-A hosted MCP);
  schema-less skill = `load_skill` catalog entry (this slice).

Tasks: **T-B1.1** [frontend] discover+parse `SKILL.md` → descriptors; **T-B1.2** [frontend] merge into
`capabilitiesFrame`; **T-B1.3** [backend] `parse_manifest` preserves `description`/`body`, catalog
merge (`catalog_prompt` text + `load_skill` enum include client doc-skills, **description only** — body
never read here), byte-stable when none; **T-B1.4** [backend] `load_skill` dispatch resolves a client
doc-skill to its stored `body` (status `loaded`), pod skills unchanged.

### P2-B.2 — hosted-skill auto-routing ("don't make me name the skill")

> **DOC-SKILL auto-routing — RESOLVED 2026-06-30 by measurement (no machinery built).** Live 5-run
> measurement of the IMPLICIT prompt ("Write a short welcome greeting…", NO skill name) against the
> `repo-greeting` doc-skill: **marker 5/5, `load_skill` fired 5/5, fully deterministic (llm_calls=7,
> ok=True every run).** The `fcbd477` routing fix (doc-skills present → route to the ReAct loop) plus a
> clear skill description is SUFFICIENT — the model recognizes the doc-skill from its catalog
> description and loads it unprompted. The planned "first-class router-candidate voting" machinery was
> NOT needed and was NOT built (measure-first win). If a future doc-skill with a weak/overlapping
> description mis-routes, the lever is the description (or the repeatable doc-skill routing eval below),
> NOT new routing machinery.
>
> **HOSTED-MCP-tool half — VALIDATED LIVE 2026-06-30.** Ran the `39f86db` hosted routing eval on the
> Q4 model (6 cases × 3 repeats, ast-ts-refactor `find_references`/`rename_symbol`): **hosted accuracy
> 1.000 (18/18), zero misroutes, mean stability 1.000** (every case identical across repeats; the
> same-server find_references/rename pair never swapped). Report:
> `eval/reports/routing-eval-20260630-194907.md` (local on the pod, not committed). The
> description-driven auto-selection mechanism works. **CAVEAT — strong but NARROW:** one server, two
> tools, one cluster — it does NOT yet stress cross-server / near-neighbor disambiguation. To turn this
> into a real regression GATE, expand `eval/fixtures/hosted_routing.example.jsonl` +
> `hosted_tools.example.json` with the other bundled servers (`component-doc-gen`, `a11y-audit`) and
> near-neighbor tools across clusters, then re-run. Optional polish — the mechanism is already proven.
>
> **EXPANDED FIXTURE (3 clusters, 8 real tools, 15 cases × 3 repeats) — RUN 2026-06-30** (`01338d5`,
> report `routing-eval-20260630-200009.md`, local). **DISAMBIGUATION IS CLEAN — zero neighbor
> confusion** across all clusters (find_references↔move_symbol, generate↔generate_batch,
> audit_file↔audit_url all perfect; mean stability 0.978). Per-cluster: a11y **1.000 ✅**, doc_gen
> 0.750, ts_refactor 0.667, overall 0.800. **The two below-gate clusters are NOT routing failures —
> they are correct ABSTENTION**, never a wrong-neighbor pick: `rename_symbol` recall 0.0 and `generate`
> recall 0.5 because their REAL schemas require args the arg-sparse task can't supply (`rename_symbol`
> needs `tsconfig`+`file`=the declaring file; `generate` needs an absolute `component_path`), and the
> model won't emit a one-shot call it can't populate. In production these are **multi-step** flows
> (`find_references`/`search` to locate the declaration → `rename_symbol` with the file) — the
> contextless one-shot eval can't represent that, so the abstention is an INSTRUMENT limitation, not a
> description bug. **Decision: do NOT tune descriptions to chase the gate** (that would teach blind
> under-specified calls). Two OPTIONAL follow-ups, both separate from routing:
> 1. **Skill ergonomics** — make `rename_symbol`/`generate` auto-resolve their heavy params (find the
>    declaring file from the symbol; accept a component *name* + search for the file). Lifts the gate
>    naturally AND lowers real friction. A SKILL change, not a routing change.
> 2. **Production-faithful eval** — measure the full AGENTIC flow (does the ReAct loop rename via
>    find→rename?) instead of one-shot selection. That's what actually matters for "does rename work".
>
> Bottom line: the P2-B.2 question ("does the model auto-select the right hosted tool without being
> named?") is **answered YES for disambiguation**; arg-heavy-tool one-shot triggering is a separate,
> optional skill-ergonomics track.

**Problem (from P2-B.0 live testing):** the user still prefixes prompts with "use ast-ts-refactor"
because auto-selection of a **hosted MCP tool** is unproven on the Q4 Gemma model. The harness
already auto-routes — pod skills via `SkillRouter.select()`/`catalog_prompt` (gated by the routing
eval, §5: new skill ≥ 0.80, no existing skill drops > 0.05). But hosted MCP skills bypass that path:
they appear as flat `mcp__<server>__<tool>` tools in the ReAct loop and are selected purely off the
tool's own `inputSchema.description` (whatever the skill author wrote for its MCP layer) — which has
**never been run through the routing eval**. So "don't name the skill" is a **description-quality +
measurement** problem, not new architecture.

This is the CLAUDE.md discipline applied to hosted tools — **measure → tune descriptions → only then
add machinery**:
- **Measure first.** Extend the routing eval to cover hosted MCP tools: generate natural tasks
  ("find all references to function X in TypeScript") with NO skill name in the prompt, and score
  whether the model calls the right `mcp__<server>__<tool>`. Reuse `eval/extend_eval.py` /
  `eval/run_routing_eval.py`; the new signal source is each MCP tool's `tools/list` description, not
  a `SKILL.md`. Acceptance: ≥ 0.80 auto-select per hosted tool, no pod-skill regression > 0.05.
- **Tune descriptions** (the real lever). Where a hosted tool mis-routes, improve its source
  `inputSchema` description in `services/skills/<name>/src` (rebuild `dist/`), the same way a
  mis-routing pod skill gets its `SKILL.md` sharpened. No prompt changes needed.
- **Only if measurement still falls short**, consider a lightweight pre-selection hint step — but do
  NOT add it speculatively; gate it on the eval numbers.
- **Constraint:** any enrichment must keep prefix byte-stability (hosted tool schemas are sorted by
  final name in `build_tool_list`) and not break the no-client pod-routing path.
- **Known follow-up (latency, from the `fcbd477` review):** the plan-node fix routes EVERY skill-less
  goal to the ReAct loop whenever ANY client doc-skill is installed (so a trivial "what is 2+2" loses
  the one-call direct-answer fast-path — correctness is fine, the loop still answers via `finish`, only
  latency regresses). Acceptable as shipped. IF doc-skill users report latency on trivial goals, add a
  CHEAP relevance gate (lexical/embedding match of the goal against doc-skill *descriptions*) before
  routing to execute — do NOT revert to pod-only routing (that's the blindness `fcbd477` removed), and
  do NOT add an extra LLM judgement call (defeats the latency goal). Measure first.

### P2-B.3 — global user-installed MCP servers (`~/.labmate/mcp.json`) — IMPLEMENTED (Opus PASS), awaiting live test
**Status (2026-06-30):** T-B3.1 + T-B3.2 DONE (`c38b777` + collision-test hardening `fcd0477`), Opus-judged
**PASS** (282 frontend tests). `electron/labmate-home.ts` gained `labmateMcpConfigPath()` +
`readUserMcpServers()` (defensive parse — missing/corrupt/wrong-shape → `[]`, never throws; skips
entries with no `command` or a `__` in the name). `McpHostManager.startAll` extracted `_startServer`
and now hosts user servers after the built-ins, **built-ins win on name collision** (skip-on-fail
isolated; the collision test asserts the user spec is never started). `McpServerSpec` gained `env?`;
`cwd`/`env` flow to `StdioClientTransport`. Namespacing/rooting unchanged — a user server is just
another `mcp__<name>__<tool>` host. **T-B3.3 (live):** a test `~/.labmate/mcp.json` is staged
(`my-refactor` → the bundled ast-ts-refactor dist under a new name); rebuild the app and confirm
`mcp__my-refactor__*` tools appear in `getMcpTools()` and execute.

#### (original plan below)
### P2-B.3 — global user-installed MCP servers (`~/.labmate/mcp.json`)
**Motivation:** mirrors how Claude scopes MCP — a `--scope user` server is GLOBAL (available in every
session regardless of folder). Today Labmate hosts only the **built-in/first-party** TS skills
(`BUILTIN_MCP_SERVERS` hardcoded in `electron/mcp-registry.ts`) — the analog of Claude's built-in
tools (ship with the app, no install). What's missing is the **user-installed** path: drop/declare an
MCP server once and have it hosted for every session. Same global home as doc-skills (`~/.labmate/`,
established in P2-B.1) — unify the "install" surface:
```
~/.labmate/
  skills/                ← global doc-skills            (DONE, P2-B.1)
  mcp.json               ← global user MCP servers      (THIS SLICE)
```

**Design (locked enough to plan; confirm details at build time):**
- **Config shape** (`~/.labmate/mcp.json`, mirror Claude's `.mcp.json`):
  `{ "mcpServers": { "<name>": { "command": "node", "args": ["/abs/path/index.js"], "cwd"?, "env"? } } }`.
  Parse defensively (tolerate missing/corrupt file → empty; never crash startup).
- **Hosting:** `McpHostManager.startAll()` already spawns from a `{name, command, args, cwd?}` spec — it
  only needs to ALSO iterate the parsed `mcp.json` servers in addition to `BUILTIN_MCP_SERVERS`. Same
  `mcp__<server>__<tool>` namespacing, same `getToolDescriptors()` merge into `capabilitiesFrame`, same
  dispatch-side path rooting (P2-B.0). A user server failing to start is skipped + logged, never fatal
  (built-ins keep working).
- **Reuse, don't rebuild:** the whole backend manifest seam (advertise/route/round-trip) is
  source-agnostic — a user MCP tool is just another `source:'mcp'` descriptor with a schema. No backend
  changes expected; this is frontend (host registry) + a config reader. Keep `labmate-home.ts` as the
  one place that resolves `~/.labmate/` (`labmateMcpConfigPath()` = `<labmateHome>/mcp.json`).
- **Name-collision rule:** if a user server name collides with a built-in (`ast-ts-refactor` etc.),
  define precedence (recommend: built-ins win, log the shadow) so a user can't silently override a
  first-party skill.
- **Trust:** user-declared servers are arbitrary local executables. For now they're first-party-trusted
  (the user wrote the config); a trust/allowlist gate is a follow-up (same deferral as the P2-A "add MCP
  server" UI). Do NOT auto-discover executables — ONLY spawn what `mcp.json` explicitly declares.

**Tasks (when built):** T-B3.1 [frontend] `mcp.json` reader in `labmate-home.ts` (+defensive parse tests);
T-B3.2 [frontend] `McpHostManager` hosts the user servers alongside built-ins, collision rule, skip-on-
fail (+tests with a fake server spec); T-B3.3 [integration/live] declare a real local MCP server in
`~/.labmate/mcp.json`, confirm its tools appear in the manifest and execute. **Stop point: live test.**

**Later (not this slice):** project-scoped MCP (`<workspace>/.labmate/mcp.json`, Claude's `project`
scope) + a UI "add server" flow + trust gate. Global-first matches the user's mental model; project
scope layers on after.

## P2-C — client-side semantic search (CodeGraph)
`code_semantic_search` becomes a client-routed tool when the client declares `codegraph`: the
frontend queries the local CodeGraph daemon (`<workspace>/.codegraph/daemon.sock`) and returns
ranked hits. Retire the pod `codegraph_embedder` to no-client fallback only. Adds the CodeGraph
CLI prereq (track in `docs/local-execution-prerequisites.md`).

## P2-D — decommission pod discovery to fallback-only
Pod CodeGraph + `WORKSPACE_PATH` access exist ONLY for no-client/headless; never reached while a
capable client is attached.

## Constraints
Prefix byte-stability (manifest sorted/canonical); MCP stdout-is-sacred; tests mirror services/;
no-client pod fallback never broken; client deps tracked in the prerequisites doc.

## Stop points
End of P2-A → live test (host a TS skill). Then P2-B, then P2-C, each its own slice + review.
