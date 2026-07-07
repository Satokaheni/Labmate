# Local-Execution Prerequisites (client machine)

In the local-execution architecture the **model + orchestrator + Redis/Mongo run on the
server**, but file-touching tools, certain MCPs, and `SKILL.md` skills run on the **user's
machine** (the Electron frontend). Those tools shell out to / connect to real local binaries
and daemons. This doc is the single source of truth for **what must be installed on the client**
for each feature to work.

> Keep this updated: whenever a change adds a client-side external dependency, add a row here
> in the same PR.

## Quick install (macOS)
```bash
brew install ripgrep        # search_files (Phase 1)
# pytest (or your project's test runner) must be on PATH for run_tests (Phase 1)
# CodeGraph (P2-C) — code intelligence hosted as a client MCP server:
#   install the codegraph CLI v0.9.9 (https://github.com/colbymchenry/codegraph)
#   codegraph init          # in each repo → builds <repo>/.codegraph/codegraph.db
#   codegraph serve --mcp   # runs it as a stdio MCP server (this is what the frontend hosts)
```

## Prerequisites by feature

| Prereq | Needed for | Phase | Install | Env override / notes |
|--------|-----------|-------|---------|----------------------|
| **ripgrep (`rg`)** | `search_files` (regex code search over the workspace) | 1 | `brew install ripgrep` | `LABMATE_RG_PATH` to point at a specific binary. Handler also probes `/opt/homebrew/bin/rg` + `/usr/local/bin/rg`. Missing → clear "ripgrep not found" error. **Bundling `rg` with the app is a deferred hardening item.** On the single-process local harness (`infrastructure/install.sh`), `rg` is also installed server-side for parity; a missing `rg` falls back to a pure-Python search. |
| **A test runner** (default `pytest`) | `run_tests` (run the project suite + verify) | 1 | project-dependent (`pip install pytest`, `npm i`, …) | `LABMATE_TEST_CMD` overrides the command (default `pytest --tb=short -q`). `LABMATE_TEST_TIMEOUT_MS` (default 120000). Only allow-listed runners may execute: pytest, python, python3, npm, npx, yarn, pnpm, jest, vitest, go, cargo, make, bun, deno. |
| **A configured workspace root** | all client file tools (read/write/list/search/test) | 0–1 | set in the frontend (top-bar workspace / post-login modal) | Not a binary, but a setup prereq — with no root, tools return "path is outside all workspace roots". |
| **Node.js runtime** | the Electron client itself (and, later, hosting Node/TS MCP servers + TS skills) | app baseline / 2 | bundled with the app | — |

## Phase 2

| Prereq | Needed for | Notes |
|--------|-----------|-------|
| **CodeGraph CLI v0.9.9** (https://github.com/colbymchenry/codegraph) | client-side CodeGraph / semantic search (P2-C) | Install the CLI, run **`codegraph init`** in the repo (builds `<repo>/.codegraph/codegraph.db`). CodeGraph is itself a stdio MCP server (`codegraph serve --mcp`). Two hosting modes exist: **(a) client-hosted** — a capable client declares it via `~/.labmate/mcp.json` (P2-B.3); its 8 tools (`mcp__codegraph__codegraph_search`/`_explore`/`_callers`/`_callees`/`_impact`/`_node`/`_files`/`_status`) route to the client, and the orchestrator's own `code_semantic_search` is auto-excluded for that client. **Verified working entry:** `{ "mcpServers": { "codegraph": { "command": "<abs>/codegraph", "args": ["serve","--mcp","--path","<repo>"], "cwd": "<repo>" } } }`. ⚠️ **Known limitation:** `--path` is static per `mcp.json` (global, spawned at app startup), so CodeGraph serves ONE hard-coded repo, not the chat's active workspace — **workspace-aware auto-hosting is a P2-C follow-up** (auto-detect `.codegraph/` in the active workspace root + spawn CodeGraph there). **(b) orchestrator-hosted (local single-process harness, default)** — the orchestrator spawns `codegraph serve --mcp --path $WORKSPACE_PATH` itself (see `services/orchestrator/main.py::_build_codegraph_params`/`codegraph_enabled`) and routes the agent's `code_semantic_search` tool to the daemon's `codegraph_explore` (NL question -> verbatim source, `k` dropped — internally capped). No `~/.labmate/mcp.json` entry is needed for this mode; it just requires the `codegraph` CLI installed and the repo `codegraph init`'d. Resolved via `shutil.which("codegraph")` or `LABMATE_CODEGRAPH_PATH`; gate with `ENABLE_CODEGRAPH=0` to disable explicitly (graceful no-op skip when the binary is absent, e.g. CI). |
| **User-installed local MCP servers** | client-hosted MCP tools (P2-A / P2-B.3) | Each server brings its own runtime/deps; declared in `~/.labmate/mcp.json` (like `claude mcp add --scope user`). Their tools are namespaced `mcp__<server>__<tool>`. |
| **`SKILL.md` skills on disk** | local doc-skills (P2-B.1) | Pure markdown (frontmatter + body) in `~/.labmate/skills/<name>/SKILL.md`; discovered by the frontend, advertised in the `load_skill` catalog. No extra runtime — the model uses the local primitives the skill describes. |

## How a missing prereq behaves
Client tools fail **gracefully** and surface a clear error back to the model (never a crash):
`search_files` → "ripgrep (rg) not found…"; `run_tests` blocked runner → exit 126; no workspace
root → "path is outside all workspace roots". The **no-client / headless path** (Discord, CLI,
eval) needs none of these — it falls back to the pod's tools.
