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
# CodeGraph CLI v0.9.9       # client semantic search (Phase 2 — not yet shipped)
```

## Prerequisites by feature

| Prereq | Needed for | Phase | Install | Env override / notes |
|--------|-----------|-------|---------|----------------------|
| **ripgrep (`rg`)** | `search_files` (regex code search over the workspace) | 1 | `brew install ripgrep` | `LABMATE_RG_PATH` to point at a specific binary. Handler also probes `/opt/homebrew/bin/rg` + `/usr/local/bin/rg`. Missing → clear "ripgrep not found" error. **Bundling `rg` with the app is a deferred hardening item.** |
| **A test runner** (default `pytest`) | `run_tests` (run the project suite + verify) | 1 | project-dependent (`pip install pytest`, `npm i`, …) | `LABMATE_TEST_CMD` overrides the command (default `pytest --tb=short -q`). `LABMATE_TEST_TIMEOUT_MS` (default 120000). Only allow-listed runners may execute: pytest, python, python3, npm, npx, yarn, pnpm, jest, vitest, go, cargo, make, bun, deno. |
| **A configured workspace root** | all client file tools (read/write/list/search/test) | 0–1 | set in the frontend (top-bar workspace / post-login modal) | Not a binary, but a setup prereq — with no root, tools return "path is outside all workspace roots". |
| **Node.js runtime** | the Electron client itself (and, later, hosting Node/TS MCP servers + TS skills) | app baseline / 2 | bundled with the app | — |

## Coming in Phase 2 (not yet shipped — listed so we plan installs early)

| Prereq | Needed for | Notes |
|--------|-----------|-------|
| **CodeGraph CLI v0.9.9** | client-side `code_semantic_search` (P2-C) | User installs it, runs its init/index in the workspace; the frontend connects to `<workspace>/.codegraph/daemon.sock`. Gated OFF until the client declares the `codegraph` capability. |
| **User-installed local MCP servers** | client-hosted MCP tools (P2-A) | Each server brings its own runtime/deps; registered like `claude mcp add`. Their tools are namespaced `mcp__<server>__<tool>`. |
| **`SKILL.md` skills on disk** | local skills (P2-B) | Pure markdown (frontmatter + body); discovered by the frontend, advertised to the model. No extra runtime — the model uses the local tools the skill describes. |

## How a missing prereq behaves
Client tools fail **gracefully** and surface a clear error back to the model (never a crash):
`search_files` → "ripgrep (rg) not found…"; `run_tests` blocked runner → exit 126; no workspace
root → "path is outside all workspace roots". The **no-client / headless path** (Discord, CLI,
eval) needs none of these — it falls back to the pod's tools.
