# Local-Execution Surface — Implementation Plan (Phase 0 + Phase 1)

> Supersedes the brief `2026-06-30-local-execution-surface.md` with the **manifest-first**
> contract (frontend = local MCP/skill host is the Phase 2 target; Phase 0/1 build the
> forward-compatible contract). Monorepo. Branch: `feat/local-execution-surface`.

**Goal:** make discovery + edit + verify run on the client (frontend workspace), gated by an
explicit capability manifest, with the pod `/workspace` as a no-client fallback only.

**Build loop:** every task Haiku implements → Opus judges → fix until pass. Phase close: Opus
project reviewer; if React code was touched, also `npx react-doctor@latest` (ignore text-too-small).

---

## The contract (single source of truth)

**WS frame — frontend → ws_gateway, sent immediately after `auth.ok`:**
```json
{
  "type": "client.capabilities",
  "protocolVersion": 1,
  "tools": [
    { "name": "read_file",  "source": "builtin" },
    { "name": "write_file", "source": "builtin" },
    { "name": "list_dir",   "source": "builtin" }
  ]
}
```

**Tool descriptor:** `{ name, source: "builtin"|"mcp"|"skill", namespace?, schema? }`
- `schema` is the OpenAI tool object `{type:"function", function:{name,description,parameters}}`.
- **builtin** tools declare NAME ONLY; the orchestrator advertises its own *canonical* schema
  (so the client can't drift the wording → protects prefix-cache + model familiarity).
- **mcp/skill** tools (Phase 2) MUST supply `schema`; advertised verbatim, name namespaced
  `mcp__<server>__<tool>`.

**Advertised-tools order (must be byte-stable per goal):**
1. skill interface (`load_skill`, `call_skill_tool`) — when skills enabled
2. `code_semantic_search` — when declared (or no-client + codegraph fallback)
3. `memory_search` — when memory enabled
4. builtins in FIXED canonical order: read_file, write_file, list_dir, search_files, run_tests
   (only those the manifest declares); `run_bash` ONLY when **no client attached** (fallback)
5. client mcp/skill tools (Phase 2) — sorted by name
6. `finish` — always last

`client_attached := manifest is not None`. When attached, `run_bash` is never advertised.

---

## Phase 0 — manifest handshake (plumbing; not independently live-testable)

### T0.1 [backend] Manifest contract + PromptAssembler ingestion
- Create `services/orchestrator/tool_manifest.py`: `ToolDescriptor`/`ClientManifest` (TypedDict or
  dataclass), `parse_manifest(payload) -> ClientManifest | None`, `CANONICAL_BUILTIN_SCHEMAS`
  (read_file/write_file/list_dir/search_files/run_tests — move the canonical schemas here),
  `build_tool_list(manifest, *, skill_router, codegraph_enabled, memory_enabled) -> list[dict]`
  implementing the order above (deterministic; client mcp/skill tools sorted by name).
- Rewire `PromptAssembler.__init__` to accept `client_manifest=None` and delegate tool assembly to
  `build_tool_list`. Keep `_static_tail_schemas` for the **no-client fallback** path
  (read/write/list/run_bash/run_tests/finish unchanged).
- **Tests:** `canonical_prefix()` byte-identical across two assemblers with the same manifest;
  client-attached drops `run_bash`; an mcp tool is namespaced + sorted; no-client path unchanged
  (existing prefix tests stay green).

### T0.2 [backend] Handshake transport + payload threading
- `ws_gateway/server.py`: accept `{type:"client.capabilities", ...}` in `_ws_loop`; store on the
  connection; on `send`, include `client_capabilities` (the manifest) in the goal payload pushed to
  `labmate:goals`.
- `main.py _handle`: parse `payload["client_capabilities"]` via `parse_manifest`; derive
  `client_attached`; thread the manifest into the orchestrator call that builds the PromptAssembler.
- **Tests:** capabilities frame stored + echoed into the goal payload; `_handle` parses it; absent
  field → `client_attached=False` (fallback).

### T0.3 [backend] Dispatch routes off the manifest
- `coding_orchestrator.py`: replace the static `LOCAL_TOOL_NAMES` membership check in
  `_run_react_loop` dispatch with a **per-task set derived from the manifest** (builtins +
  mcp/skill tool names). Manifest tools route through `request_local_tool`. No client → existing
  behavior.
- **Tests:** a manifest-declared tool dispatches via `request_local_tool`; unknown tool errors
  enumerated; no-client path unchanged.

### T0.4 [frontend] Declare capabilities on connect
- `useLabmateWS.ts`: after `auth.ok`, send the `client.capabilities` frame declaring builtins
  (read_file/write_file/list_dir). Mirror the descriptor type in `electron/protocol.ts` (or
  `src/protocol/`) for the eventual repo split.
- **Tests:** frame sent after auth.ok with the three builtins; vitest + tsc green.

## Phase 1 — find → read → edit → verify on the client (LIVE-TESTABLE milestone)

### T1.1 [frontend] search_files (ripgrep) handler + manifest entry
- `electron/tool-executor.ts`: `search_files` handler — ripgrep over workspace roots
  (`query[regex], path?, glob?, max_results?`), return structured hits `{file, line, text}`.
  Bundle `rg` via electron-builder `extraFiles`, fall back to system `rg`. Add `search_files` to
  the declared manifest + `LOCAL_TOOL_NAMES` in `main.ts`.
- **Tests:** handler returns ranked hits on a fixture; path-escape guard honored; manifest includes it.

### T1.2 [frontend] run_tests handler + permission allowlist
- `electron/tool-executor.ts`: `run_tests` handler — spawn the project test command
  (default `pytest`, configurable) in the workspace root; capture exit code + raw output; stream
  back. Executor-side allow/ask/deny policy (test-command allowlist), built as a general per-tool
  policy. Add to manifest.
- **Tests:** spawn mocked; exit code + output captured; disallowed command rejected.

### T1.3 [backend] Gating + run_tests round-trip
- When `client_attached`: `run_bash` not advertised (T0.1 already); `code_semantic_search` gated
  OFF unless declared; repo-reading skills gated so the model prefers client primitives.
- `run_tests` round-trip: chunked `tool.result` / raised XREAD deadline so minutes-long runs fit
  (the current 30s round-trip won't).
- **Tests:** client-attached advertises no run_bash + no code_semantic_search (undeclared);
  run_tests round-trip with a long deadline; pod path intact when no client.

### T1.4 [backend] Invariant regression test
- Assert: in `_run_react_loop`, while a client is attached, **no tool dispatch targets
  `self.workspace`**. `request_local_tool` round-trip test for `search_files` (mirror read_file test).
- **Acceptance:** `pytest tests/services/orchestrator/ -q` green; the "find where WebSocket auth is
  handled → token-verify fn" query resolves via search_files→read_file against the frontend
  workspace (ground truth `services/ws_gateway/auth.py:101` `verify_token`).

---

## Frontend vs backend deliverables
- **Frontend:** `client.capabilities` frame + descriptor type; `search_files` + `run_tests`
  handlers; ripgrep bundling; permission policy.
- **Backend:** `tool_manifest.py`; PromptAssembler ingestion; ws_gateway frame + payload threading;
  `client_attached`; dispatch routing; gating; run_tests streaming; invariant regression test.

## Constraints (non-negotiable)
Prefix byte-stability (manifest sorted/canonical); MCP stdout-is-sacred; AutoTokenizer never
tiktoken; Redis Streams; tests mirror services/, `@pytest.mark.asyncio` on async; no-client pod
fallback never broken.

## Stop point
End of Phase 1 → push branch → live-test on RunPod.
