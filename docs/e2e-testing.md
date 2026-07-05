# Labmate E2E Testing Runbook

This document describes what Labmate is, what we are testing, and the exact steps to run the full stack end-to-end. It applies to any host that can run the local stack (RunPod-style pods work but aren't required).

---

## What Labmate Is

Labmate is a local autonomous coding agent. A user types a task into the CLI; it is submitted in-process to `services.local.main` (gateway + orchestrator on one asyncio loop), which runs a LangGraph state machine (plan → execute → check → reflect), calls the Gemma 4 model via llama.cpp, and returns a result. The CLI waits for the result and renders it.

```
User (CLI)
  └─ WebSocket ──► services.local.main (gateway + orchestrator, one asyncio loop)
                      └─ submit_goal() ──► Orchestrator (LangGraph)
                                              └─ HTTP ──► llama-server (Gemma 4, :8000)
                                              └─ stdio ──► MCP Bridge (TypeScript)
                      SQLite LocalStore ◄──── sessions / turns / checkpoints written
  ◄── WebSocket result ─────────────────────────────────────────────
```

**Storage:** one SQLite `LocalStore` (sessions, workspaces, turns, auth, LangGraph checkpoints) under `.data/labmate_state.sqlite`. No MongoDB, no Chroma, no Redis.

**Key constraint:** On a single-GPU host, `QWEN_BASE` defaults to `GEMMA_BASE` — Gemma 4 serves both the architect and editor roles.

---

## Repository

```
git clone git@github.com:Satokaheni/Labmate.git
cd Labmate
```

---

## Step 1 — Install dependencies (first time only)

```bash
infrastructure/install.sh
```

This builds llama.cpp from source, downloads the Gemma 4 31B GGUF, and installs Python packages. Takes ~15–30 minutes on first run. Idempotent — safe to re-run.

---

## Step 2 — Start the model server

```bash
infrastructure/serve-model.sh
```

Starts `llama-server` on port **8000** with the Gemma 4 31B GGUF. The script blocks until the model is loaded into VRAM and the `/health` endpoint responds (up to ~10 min for first load). Pass `--no-wait` to return immediately.

**Verify:**
```bash
curl -s http://localhost:8000/health | grep '"status":"ok"'
```

**Logs:** `.data/logs/llama-server.log`

---

## Step 3 — Start the local harness

```bash
infrastructure/start.sh
```

Starts (natively, no Docker, no MongoDB/Redis/Chroma):
- **`services.local.main`** `:8787` — the gateway (FastAPI + WebSocket) and the
  orchestrator (LangGraph loop) running together as one process on one asyncio
  loop, sharing one SQLite `LocalStore`. Skill dispatch is in-process — there is
  no standalone skill-worker.
- **MCP Bridge** — built from `services/mcp-bridge/` if `dist/index.js` is missing; the orchestrator spawns it as a child process
- **SearXNG** (optional, native) — metasearch backend for the web-search skill; non-fatal if not installed

The script is idempotent — already-running processes are left alone.

**Verify each service:**
```bash
infrastructure/status.sh
```

Or manually:
```bash
curl -s http://localhost:8000/health | grep '"status"'  # → "ok"   (llama-server)
curl -fsS http://localhost:8787/healthz                 # → {"ok":true}   (services.local.main)
```

**Logs:**
```
.data/logs/local.log          ← services.local.main (gateway + orchestrator combined)
.data/logs/llama-server.log
```

---

## Step 4 — Start the CLI

```bash
infrastructure/start-cli.sh
```

This sources `local.env`, checks the local harness (`services.local.main`) is alive via its pidfile and `/healthz`, then launches:

```
python -m services.cli
```

On first run it asks for a display name and saves your identity to `~/.labmate/identity.json`.

**Modes:**

| Command | What it does |
|---|---|
| `./start-cli.sh` | Interactive REPL with workspace picker |
| `./start-cli.sh "write a hello world in Python"` | One-shot task, prints result, exits |
| `./start-cli.sh --resume <session-id>` | Resume a previous session |
| `./start-cli.sh --workspace <workspace-id>` | Open a specific workspace directly |

---

## E2E Test Scenarios

Run these in order — each builds on the previous.

### Scenario 1 — Goal round-trip (smoke test)

Verify that a task can be submitted and a result retrieved without the orchestrator doing anything meaningful.

```bash
# In the REPL, type a trivial task:
> say hello

# Expected: the CLI spinner runs, then prints a response from the model.
# Success: no timeout, no "Connection error", result is readable text.
```

### Scenario 2 — Session persistence

```bash
# Start a REPL session, note the session ID printed in the header
./start-cli.sh

# Type a task, note the session ID
> write a function that adds two numbers

# Exit the REPL (/quit or Ctrl-C)
# Resume it
./start-cli.sh --resume <session-id>

# Success: workspace and context are restored, no picker shown
```

### Scenario 3 — Workspace with a code directory

```bash
./start-cli.sh
# At the workspace picker, choose "Create new"
# Name: test-workspace
# Path: /workspace/Labmate/services/cli   (or any real directory)
# Instructions: (leave blank)

# Then give a task that references the workspace:
> list all Python files in the workspace paths

# Success: orchestrator receives workspace_id, model can read the path context
```

### Scenario 4 — One-shot mode

```bash
./start-cli.sh "what is 2 + 2"

# Success: prints the answer and exits cleanly (exit code 0)
```

### Scenario 5 — Checkpoint resume after crash

```bash
# Start a long-ish task in the REPL, then kill the local harness mid-flight
kill $(cat .data/pids/local.pid)

# Restart the harness
./start.sh

# Resume the session
./start-cli.sh --resume <session-id>

# Success: LangGraph loads the SqliteSaver checkpoint (same .data/labmate_state.sqlite
# file the LocalStore uses) and continues from where it left off (may re-run the
# current node)
```

---

## Watching logs during a test

Open separate terminals for these while running CLI tests:

```bash
tail -f .data/logs/local.log
tail -f .data/logs/llama-server.log
```

The local-harness log (`local.log`, combined gateway + orchestrator) shows:
- `goal received` — task pulled off the in-process goal queue (`submit_goal`)
- `plan node` — Gemma 4 architect call
- `execute node` — sub-goal execution
- `session recorded` / `session completed` — workspace tracking (SQLite `LocalStore`)

---

## Stopping everything

```bash
infrastructure/stop.sh
```

Stops `services.local.main` (gateway + orchestrator), the MCP bridge, and SearXNG (if running). Does **not** delete data — `.data/labmate_state.sqlite` persists.

To also kill the model server:
```bash
kill $(cat .data/pids/llama-server.pid)
```

---

## Known gaps / things to verify

These are non-blocking gaps flagged during past code reviews. Watch for them during e2e:

| Gap | What to look for |
|---|---|
| Exception path records `ok=True` | If the orchestrator throws during `run_task`, the SQLite session row may show `ok=True` even though it failed. Check `.data/logs/local.log` for errors and verify the session row via the `LocalStore`. |
| `--resume` silent fallthrough | If the workspace isn't in `~/.labmate/workspaces.json`, `--resume` silently drops to the workspace picker with no message. |
| `stream()` drops user/workspace identity | If you use the streaming API path directly (bypassing the CLI's normal goal submission), `user_id` and `workspace_id` won't be threaded through. The CLI's normal path submits goals in-process with identity attached, so this won't surface here. |

---

## What was just implemented (context for this session)

The three tasks completed before the e2e run this runbook originally validated:

1. **Discord unwired** — `services/connectors/deferred/` is intentionally excluded. Do not wire it.
2. **Workspace + User tracking** — `workspaces`, `users`, and `sessions` tracked with full CRUD (`WorkspaceManager`), now backed by the SQLite `LocalStore` (originally MongoDB collections, migrated by the local-state-sqlite rearchitecture). The orchestrator records a session on every goal, upserts the workspace on first sight, and marks completion with `ok` flag.
3. **CLI connector** — `services/cli/` is a full Typer + Rich CLI with REPL, one-shot mode, session resume, workspace picker, and local identity.

> Note: the storage layer described above is stale relative to the current
> architecture — see `README.md`, `CLAUDE.md`, and `infrastructure/README.md`
> for the current single-process SQLite topology. This section is kept as
> historical context for when the CLI/workspace tracking was first built.
