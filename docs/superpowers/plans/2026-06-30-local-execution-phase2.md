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

**Next:** live-validate on RunPod — drive `mcp__ast-ts-refactor__find_references` with a relative
tsconfig and confirm it resolves + executes (no thrash, no abs-path error). Then proceed to P2-B.

## P2-B — local `SKILL.md` discovery (after P2-A merges)
Frontend discovers `SKILL.md` files (frontmatter name/description) in the workspace / a skills
dir; declares them as `source:'skill'` in the manifest (metadata only). The orchestrator
advertises the metadata; on use, the body is loaded and the model uses the local tools the skill
describes — **no skill runtime on the client** (the documentation model). Reclassify the Python
repo-reading skills (supersede via client primitives, or treat as candidates for a future client
runtime); keep content+model skills server-side fed client content.

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
