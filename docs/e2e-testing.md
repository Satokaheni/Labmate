# Labmate E2E Testing Runbook

This document is for a Claude session on RunPod. It describes what Labmate is, what we are testing, and the exact steps to run the full stack end-to-end.

---

## What Labmate Is

Labmate is a local autonomous coding agent. A user types a task into the CLI; it travels through Redis to the orchestrator, which runs a LangGraph state machine (plan → execute → check → reflect), calls the Gemma 4 31B model via llama.cpp, and returns a result. The CLI waits for the result and renders it.

```
User (CLI)
  └─ XADD labmate:goals ──► Redis Stream
                                └─ XREADGROUP ──► Orchestrator (LangGraph)
                                                      └─ HTTP ──► llama-server (Gemma 4, :8000)
                                                      └─ stdio ──► MCP Bridge (TypeScript)
                                SET labmate:result:<id>
                                PUBLISH labmate:result:<id> "ready"
  ◄── pubsub / GET ────────────────────────────────────────────────
```

**Storage:** MongoDB (sessions, workspaces, checkpoints), Chroma (vector memory), Redis (task stream + result cache).

**Key constraint:** On this pod there is only one GPU, so `QWEN_BASE` defaults to `GEMMA_BASE` — Gemma 4 serves both the architect and editor roles.

---

## Repository

```
git clone git@github.com:Satokaheni/Labmate.git
cd Labmate
```

---

## Step 1 — Install dependencies (first time only)

```bash
infrastructure/local/install.sh
```

This builds llama.cpp from source, downloads the Gemma 4 31B GGUF, and installs Python packages. Takes ~15–30 minutes on first run. Idempotent — safe to re-run.

---

## Step 2 — Start the model server

```bash
infrastructure/local/serve-model.sh
```

Starts `llama-server` on port **8000** with the Gemma 4 31B GGUF. The script blocks until the model is loaded into VRAM and the `/health` endpoint responds (up to ~10 min for first load). Pass `--no-wait` to return immediately.

**Verify:**
```bash
curl -s http://localhost:8000/health | grep '"status":"ok"'
```

**Logs:** `.data/logs/llama-server.log`

---

## Step 3 — Start support services + orchestrator

```bash
infrastructure/local/start.sh
```

Starts (natively, no Docker — this pod blocks container namespaces):
- **MongoDB** `:27017` — replica set `rs0`, required for LangGraph checkpointing
- **Redis** `:6379` — AOF persistence, hosts the `labmate:goals` stream
- **Chroma** `:8765` — vector memory
- **MCP Bridge** — built from `services/mcp-bridge/` if `dist/index.js` is missing; the orchestrator spawns it as a child process
- **Skill worker** — pulls from Redis and dispatches skill MCP servers
- **Orchestrator** — the LangGraph loop that processes goals

The script is idempotent — already-running processes are left alone.

**Verify each service:**
```bash
infrastructure/local/status.sh
```

Or manually:
```bash
redis-cli ping                                          # → PONG
mongosh --quiet --eval 'rs.status().myState'            # → 1 (PRIMARY)
curl -s http://localhost:8765/api/v2/heartbeat          # → {"nanosecond heartbeat": ...}
curl -s http://localhost:8000/health | grep '"status"'  # → "ok"
```

**Logs:**
```
.data/logs/orchestrator.log
.data/logs/skill-worker.log
.data/logs/mongod.log
.data/logs/redis.log
.data/logs/chroma.log
```

---

## Step 4 — Start the CLI

```bash
infrastructure/local/start-cli.sh
```

This sources `local.env`, checks Redis and the orchestrator are alive, then launches:

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

### Scenario 1 — Redis round-trip (smoke test)

Verify that a task can be pushed and a result retrieved without the orchestrator doing anything meaningful.

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
# Start a long-ish task in the REPL, then kill the orchestrator mid-flight
kill $(cat .data/pids/orchestrator.pid)

# Restart the orchestrator
./start.sh

# Resume the session
./start-cli.sh --resume <session-id>

# Success: LangGraph loads the AsyncMongoDBSaver checkpoint and continues
# from where it left off (may re-run the current node)
```

---

## Watching logs during a test

Open separate terminals for these while running CLI tests:

```bash
tail -f .data/logs/orchestrator.log
tail -f .data/logs/llama-server.log
```

The orchestrator log shows:
- `goal received` — task pulled from Redis stream
- `plan node` — Gemma 4 architect call
- `execute node` — sub-goal execution
- `session recorded` / `session completed` — workspace tracking
- `xack` — message acknowledged (means the loop completed without poison-pill)

---

## Stopping everything

```bash
infrastructure/local/stop.sh
```

Stops the orchestrator, skill worker, MCP bridge, MongoDB, Redis, and Chroma. Does **not** delete data — volumes in `.data/` persist.

To also kill the model server:
```bash
kill $(cat .data/pids/llama-server.pid)
```

---

## Known gaps / things to verify

These are non-blocking gaps Opus flagged during the last code review. Watch for them during e2e:

| Gap | What to look for |
|---|---|
| Exception path records `ok=True` | If orchestrator throws during `run_task`, MongoDB session shows `ok=True` even though it failed. Check `.data/logs/orchestrator.log` for errors and verify the session document. |
| `--resume` silent fallthrough | If the workspace isn't in `~/.labmate/workspaces.json`, `--resume` silently drops to the workspace picker with no message. |
| `stream()` drops user/workspace identity | If you use the streaming API path (not the Redis round-trip path), `user_id` and `workspace_id` won't be threaded through. The CLI uses Redis, so this won't surface here. |

---

## What was just implemented (context for this session)

The three tasks completed before this e2e run:

1. **Discord unwired** — `services/connectors/deferred/` is intentionally excluded. Do not wire it.
2. **Workspace + User tracking** — MongoDB `workspaces`, `users`, and `sessions` collections with full CRUD (`WorkspaceManager`). The orchestrator records a session on every goal, upserts the workspace on first sight, and marks completion with `ok` flag.
3. **CLI connector** — `services/cli/` is a full Typer + Rich CLI with REPL, one-shot mode, session resume, workspace picker, pubsub-safe result retrieval, and local identity.

All 182 unit/integration tests pass. This e2e run is the first time the full stack runs together.
