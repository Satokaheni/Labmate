# Labmate — Agent Coding Guide

This file is for any AI coding agent (Claude, Gemma, Qwen, or other) helping implement Labmate. Read it fully before touching any file.

---

## What This Project Is

Labmate is a local autonomous agent: Brain (LLM) → Nervous System (MCP bridge) → Hands (skills). It runs on a single GPU host. The LLM inference server runs directly on the host; all support services (MongoDB, Chroma, Redis, MCP bridge, orchestrator) run in Docker.

**Primary model:** Gemma 4 31B 4-bit (`gemma-4-31B-it-UD-Q4_K_XL.gguf`) served via **llama.cpp** (`llama-server`) with an OpenAI-compatible HTTP API on port 8000. Qwen2.5-Coder-32B is the intended specialist worker but on a single-GPU host `QWEN_BASE` defaults to `GEMMA_BASE` — both roles run on the same Gemma 4 model.

**Why llama.cpp over vLLM:** llama.cpp runs on any platform — CUDA, Metal (Mac Mini), CPU — with zero driver requirements. vLLM is CUDA-only and requires specific wheel versions per CUDA release. Research confirmed llama.cpp supports all required features: per-request `thinking_budget_tokens`, Gemma 4 tool calling via `--jinja`, and flash attention via `-fa on`.

---

## Current State vs. Target

The codebase is mid-migration. Do not confuse the two:

| | Current (M2) | Target (M3+) |
|-|-------------|--------------|
| Entry point | `main.py` | `services/orchestrator/` |
| Model loading | Unsloth direct in process | llama.cpp on host, HTTP API |
| Tool calling | Regex `[TOOL: name('arg')]` | MCP JSON-RPC over stdio |
| Memory | AgentMemory HTTP + Codegraph | MongoDB + Chroma + Redis |
| State machine | Manual loop in `orchestrator.py` | LangGraph StateGraph |
| Skills | `tools/` Python functions | Polyglot child-process MCP servers |

**Do not modify `core/orchestrator.py` or `main.py`.** They are the working M2 baseline. Build M3+ in `services/`.

---

## Session Log — 2026-06-20 (read this first)

This session took the M3 stack from "unit tests pass" to a **working, skill-aware orchestrator** running live on the pod. Branches: `fix/e2e-setup-and-redis` (pushed; the e2e + skill-selection milestone) → `feat/agent-event-stream` (current; latency/reliability fixes + the event-stream plan).

**Done & verified live (committed/pushed on `fix/e2e-setup-and-redis`):**
- **Full e2e bring-up** — installed all deps, fixed `install.sh` (missing service deps + skill deps), `start.sh` (stale-bridge rebuild), `stop.sh` (don't kill the model by default). See `docs/e2e-setup-findings.md`.
- **Pinned `redis>=5.0,<6`** — redis-py 8.x raises `TimeoutError` on empty blocking `xreadgroup` under a busy loop (silently killed goal consumption). Loop also defensively catches it.
- **Skill-aware planner + ReAct executor** (`spec_skills §2.2`) — catalog in-context, `load_skill`→`call_skill_tool`, dispatch to the `labmate:skill-tasks` worker; concurrency preserved. Plus real per-subtask reflexion + honest failure propagation.
- **100% skill selection** — `SkillRouter.select()` picks the right skill 18/18 isolated; **14/14 end-to-end** dispatch. Root fix was a deterministic bug: `SkillRunner.load_skill` activation counter never reset (`reset_activations()` now called per goal). Plus a directive that lifted recall and per-sample retries.

**Done this session on `feat/agent-event-stream` (uncommitted→committing now):**
- **Skill tool-name fix** — 6 skills' `SKILL.md` documented tool names with a namespace prefix their servers don't expose (e.g. pdf-parse `pdf_parse.parse` vs exposed `parse`), so the model emitted unusable names → `SkillUnavailable` → reflect-retry loops. Fixed all 6 (`a11y-audit`, `ast-repo-map`, `ast-ts-refactor`, `citation-graph`, `paper-to-slides`, `pdf-parse`) to bare names. pdf-parse now executes `ok=True` 3/3.
- **`plan_tool_call` cache read** — on a repeat `load_skill` the body is omitted (progressive-disclosure dedup); now falls back to `runner.loaded[name]` so plan doesn't return None on already-loaded skills (was forcing the slow ReAct fallback).
- **Event-stream implementation doc** written: `docs/superpowers/plans/2026-06-20-agent-event-stream.md`.

**Reverted (do NOT reintroduce):** `plan_tool_call` constrained-decoding (`response_format`) regressed tool-name selection; a `plan` fast-path and an LLM profiler were net-neutral/diagnostic.

**Known issues / latency state:** end-to-end is correct (14/14 dispatch) but **slow (~40–85 s/goal)**. Drivers, in order: (1) inherent ~6 s/call on the Q4 model × ~7 calls/goal; (2) **reflect-retry loops on failing skill executions** — many failures here are *environmental* (web-search/citation-graph need network, figma a key, code-sandbox Docker — none available on this pod), so they retry to exhaustion. In production with creds/network they succeed. Next latency lever (not yet done): **cap reflect-retries** on cleanly-failing skills.

## Next Step: implement the event-stream comms

**Immediate priority for the next session.** Implement `docs/superpowers/plans/2026-06-20-agent-event-stream.md` — a transport-agnostic event stream so a CLI/frontend can show, live: **which skill/tool was selected, when it runs/finishes, the model's reasoning** (for debugging), **and the final answer streamed token-by-token** (Claude-style). Events are `XADD`'d to a per-task Redis stream `labmate:events:<task_id>`; the doc has the full architecture, event schema (a subset of `FRONTEND_SPEC.md §4`), consumer contract, and 6 bite-sized TDD tasks. No new spec needed — `FRONTEND_SPEC.md` is the spec; this doc is the plan. Est. ~40–55 min via the Haiku→Opus workflow (mocked tests; only Tasks 5–6 need the live stack). Build with `superpowers:subagent-driven-development` or `executing-plans`.

---

## Architecture Map

```
Host process: llama-server (port 8000)
     │
     │  OpenAI-compatible HTTP  (INFERENCE_URL=http://host.docker.internal:8000)
     ▼
services/orchestrator/          ← Python, asyncio, LangGraph
     │
     │  stdin/stdout JSON-RPC 2.0
     ▼
services/mcp-bridge/            ← TypeScript, @modelcontextprotocol/sdk
     │
     │  child process spawn per skill
     ▼
services/skills/<name>/         ← TypeScript / Rust / Python
     │
     ▼
Memory:
  MongoDB  :27017  (sessions, messages, outbox)
  Chroma   :8000   (vector embeddings)
  Redis    :6379   (task queues via Streams, working cache)
```

---

## Spec Reference

Before implementing any component, read its spec:

| Component | Spec file |
|-----------|-----------|
| Orchestrator loop, LangGraph, Goal Tree | `research/llm-harness-research/specs/spec_orchestrator.md` |
| TypeScript MCP server | `research/llm-harness-research/specs/spec_mcp_bridge.md` |
| Python MCP client | `research/llm-harness-research/specs/spec_mcp_bridge.md` |
| MongoDB + Chroma + Redis | `research/llm-harness-research/specs/spec_memory.md` |
| llama.cpp serving + quantization | `research/llm-harness-research/specs/spec_inference.md` |
| SKILL.md format, SkillRunner, SkillRegistry | `research/llm-harness-research/specs/spec_skills.md` |
| Testing strategy, pytest-bdd | `research/llm-harness-research/specs/spec_testing.md` |
| Academic writing + critique skills | `research/llm-harness-research/specs/spec_writing_skills.md` |
| Docker, run-services.sh | `research/llm-harness-research/specs/spec_infrastructure.md` |
| Discord connector (**deferred — do not wire yet**) | `research/llm-harness-research/specs/spec_integrations.md` |

---

## Critical Rules

These are non-negotiable. Each one represents a category of production failure.

### 1. stdout is sacred in MCP servers
In any TypeScript, Python, or Rust MCP server (anything in `services/mcp-bridge/` or `services/skills/`):
- **NEVER** call `console.log()` — use `console.error()` or a logger wired to stderr
- **NEVER** call `print()` in Python skill servers — use `logging` to stderr
- **NEVER** write to Rust's stdout in skill servers — use `eprintln!()` or `tracing` to stderr
- stdout carries JSON-RPC 2.0 messages. Any non-JSON byte corrupts the stream silently.

### 2. anyio cancel scope — Python MCP client
The `ClientSession` from the Python `mcp` package uses anyio. It must enter AND exit in the same asyncio task. The single most common production failure:

```python
# WRONG — will raise RuntimeError: Attempted to exit cancel scope in a different task
async def get_session():
    async with ClientSession(...) as session:
        return session  # exits the cancel scope in the caller's task

# CORRECT — one owning task holds the session for its full lifetime
class MCPClientManager:
    async def run(self):  # this task owns the session forever
        async with ClientSession(...) as self._session:
            await self._ready.set()
            await self._shutdown.wait()
```

### 3. Gemma tokenizer — never tiktoken
When counting tokens anywhere in the Python orchestrator or memory layer:
```python
# WRONG
import tiktoken
enc = tiktoken.encoding_for_model("gpt-4")

# CORRECT
from transformers import AutoTokenizer
tokenizer = AutoTokenizer.from_pretrained("google/gemma-4-9b-it")
token_count = len(tokenizer.encode(text))
```
Gemma uses SentencePiece. tiktoken counts are wrong and cause context overflows.

### 4. Chroma — always client-server mode
```python
# WRONG — in-process, not suitable for multi-container
import chromadb
client = chromadb.PersistentClient(path="./chroma")

# CORRECT — connects to the lm-chroma container
client = chromadb.AsyncHttpClient(host="chroma", port=8000)
```

### 5. Redis — Streams for queues, not BRPOP
Task queues use Redis Streams (`XADD` / `XREADGROUP` / `XACK`), not `RPUSH`/`BRPOP`. Streams provide consumer groups, at-least-once delivery, and redelivery of crashed tasks.

### 6. llama.cpp serve command for Gemma 4
Use `llama-server` (build ≥ b8738). The critical flags:
- `-fa on` — flash attention (~40% KV VRAM reduction)
- `--reasoning-format deepseek` — puts reasoning in `message.reasoning_content`, separate from `content`
- `--reasoning-budget-message` — prevents abrupt cutoff when `thinking_budget_tokens` is hit
- **Do NOT** set `--reasoning-budget N` as a server flag — it disables per-request `thinking_budget_tokens` control

```bash
llama-server \
  -m models/gemma-4-31B-it-UD-Q4_K_XL.gguf \
  --jinja \
  --n-gpu-layers 999 \
  --ctx-size 16384 \
  --parallel 2 \
  --host 127.0.0.1 --port 8000 \
  -fa on \
  --reasoning-format deepseek \
  --reasoning-budget-message "\n</think>\n"
```

Per-request reasoning control (pass in `extra_body`):

**IMPORTANT:** Post-April-2026 llama.cpp builds default `thinking_budget_tokens` to `INT_MAX` when not set — this causes non-deterministic hangs. **Every request must set it explicitly.**

```python
# Planning, coding, writing, research — Labmate's core purpose — reasoning ON
# architect() default: thinking_budget=3000
# editor() default:    thinking_budget=2048
{"thinking_budget_tokens": 2048}   # or 3000 for deeper planning

# Tool selection only (LLM choosing which MCP tool to invoke) — reasoning OFF
{"thinking_budget_tokens": 0}
```

Which nodes get what:
| Node | Model call | `thinking_budget_tokens` |
|------|-----------|--------------------------|
| `plan_node` | `architect()` | 3000 |
| `execute_node` | `editor()` | 2048 |
| `check_node` | `architect()` | 1000 |
| `reflect_node` | `architect()` | 3000 |
| MCP tool dispatch | direct LLM call | 0 |

Never use `enable_thinking: false` via `chat_template_kwargs` — it is silently ignored for Gemma 4.

### 7. MongoDB transactional outbox
Never write to MongoDB and Chroma/Redis in two separate calls. Use the transactional outbox pattern: write the document + an outbox marker atomically in one MongoDB write. A background worker reads the outbox and projects to Chroma + Redis. This prevents partial writes from corruption.

### 8. LangGraph checkpointer
Use `AsyncMongoDBSaver` from `langgraph-checkpoint-mongodb` for the LangGraph `StateGraph` checkpointer. Do not use `MemorySaver` (in-memory, no persistence) or file-based savers.

---

## File Naming Conventions

| Language | Convention | Example |
|----------|-----------|---------|
| Python files | `snake_case.py` | `context_manager.py` |
| TypeScript files | `camelCase.ts` | `skillRegistry.ts` |
| TypeScript types/interfaces | PascalCase | `ToolCallResult` |
| Python classes | PascalCase | `ContextManager` |
| Python functions/methods | `snake_case` | `build_context()` |
| SKILL.md skill names | `kebab-case` | `ast-repo-map` |
| Docker container names | `lm-<name>` | `lm-mongodb` |
| Docker volumes | `<name>-data` | `mongo-data` |

---

## Service URLs (inside Docker network)

When writing code that runs inside a Docker container:

```python
INFERENCE_URL = os.getenv("INFERENCE_URL", "http://host.docker.internal:8000")
MONGO_URI     = os.getenv("MONGO_URI",     "mongodb://mongodb:27017/labmate")
CHROMA_URL    = os.getenv("CHROMA_URL",    "http://chroma:8000")
REDIS_URL     = os.getenv("REDIS_URL",     "redis://redis:6379/0")
MCP_BRIDGE    = os.getenv("MCP_BRIDGE_URL","http://mcp-bridge:9000")
```

Always read from environment variables. Never hardcode these.

---

## Testing Rules

- Tests live in `tests/` mirroring the `services/` structure
- Mark tests: `@pytest.mark.mocked` (no GPU, always runs in CI) or `@pytest.mark.live` (needs running inference server)
- Assert structure, not literal text — LLM output is non-deterministic
- Use `respx` to mock the llama.cpp OpenAI-compatible endpoint in mocked tests
- The cross-judge for LLM-as-judge tests must NOT be Gemma or Qwen (self-grading bias)
- Full testing spec: `research/llm-harness-research/specs/spec_testing.md`

---

## Build Order (Milestone 3+)

Build in this sequence — each layer depends on the one before:

1. **`services/mcp-bridge/`** — TypeScript MCP server (no dependencies on other services)
2. **Memory layer** — `StorageManager` class connecting MongoDB + Chroma + Redis
3. **`services/orchestrator/`** — Python LangGraph orchestrator using the MCP client
4. **`services/skills/`** — Individual skill servers (start with `ast-repo-map`)
5. **`services/skill-worker/`** — Worker that pulls from Redis and dispatches skills
6. **CLI connector** (`services/cli/`) — Primary interaction layer until a frontend exists. Modeled after Claude Code CLI: streaming output, session resume, workspace selection.
7. **Discord connector** — **Deferred.** Do not wire, import, or reference this in any active code path until explicitly instructed. The connector lives in `services/connectors/deferred/` and is intentionally excluded from the running stack. A frontend will exist before Discord is integrated.

When starting a component, read its spec first, then look at the existing M2 code for context on what it replaces.

---

## Next Steps: E2E Testing

**Immediate priority.** The unit tests all pass. The next job is running the full stack on RunPod and verifying the Redis round-trip, session persistence, and workspace tracking work end-to-end. The full runbook is in `docs/e2e-testing.md`.

### Starting the stack

Start in this order — each step must complete before the next:

```bash
# 1. Model server (blocks until healthy — takes ~10 min on first VRAM load)
infrastructure/local/serve-model.sh

# 2. Support services + orchestrator (MongoDB, Redis, Chroma, MCP bridge, orchestrator)
infrastructure/local/start.sh

# Verify all services are up:
infrastructure/local/status.sh
```

### Tests Claude can run autonomously (no human in the loop)

These can all be driven from the terminal without interactive input:

**1. Unit tests (no GPU needed, always safe):**
```bash
cd /workspace/Labmate
pytest tests/ -v 2>&1 | tee .data/logs/pytest.log
```

**2. Service health checks:**
```bash
redis-cli ping                                                # → PONG
mongosh --quiet --eval 'rs.status().myState'                  # → 1
curl -s http://localhost:8765/api/v2/heartbeat | head -c 80   # → {"nanosecond heartbeat":...}
curl -s http://localhost:8000/health | grep '"status"'        # → "ok"
```

**3. Redis round-trip (orchestrator end-to-end, no CLI):**
```bash
# Push a task directly
TASK_ID="e2e-$(date +%s)"
redis-cli XADD labmate:goals '*' payload \
  "{\"task_id\":\"$TASK_ID\",\"task\":\"What is 2+2? Reply in one sentence.\",\"session_id\":\"$TASK_ID\"}"

# Poll for result (up to 120 s)
for i in $(seq 1 120); do
  VAL=$(redis-cli GET "labmate:result:$TASK_ID" 2>/dev/null)
  if [ -n "$VAL" ]; then echo "$VAL"; break; fi
  sleep 1
done
```
Success: result JSON with `"ok": true`. Failure: timeout or `"ok": false`.

**4. One-shot CLI task (exercises the full CLI → Redis → orchestrator path):**
```bash
# Use python -m directly — start-cli.sh forces REPL mode
source infrastructure/local/local.env
PYTHONPATH=. python -m services.cli "Write a Python function that returns the square of a number."
```
Success: prints code output and exits with code 0.

**5. Log inspection (run alongside any test):**
```bash
tail -f .data/logs/orchestrator.log &
tail -f .data/logs/llama-server.log &
```
Look for: `task complete` (success), `task failed` (exception with traceback), `WARN` / `ERROR` lines.

### What requires human intervention

- Interactive REPL sessions (workspace picker, typing tasks)
- Session resume across invocations (need a prior session ID)
- Scenario 5 (kill-and-resume checkpoint test)

For those, follow `docs/e2e-testing.md` scenarios 1–5 with the user present.

### Diagnosing failures from logs

| Log pattern | Likely cause | Where to look |
|-------------|-------------|---------------|
| `task failed` + traceback | Exception in `run_task` or LangGraph node | `.data/logs/orchestrator.log` |
| `xreadgroup error` | Redis not running or stream not created | `.data/logs/orchestrator.log` + `redis-cli ping` |
| No `goal received` after XADD | Consumer group not joined or orchestrator not running | `.data/logs/orchestrator.log`, check pidfile |
| `MCP bridge did not become ready` | Bridge crash or missing `dist/index.js` | `.data/logs/orchestrator.log`, run `npm run build` in `services/mcp-bridge/` |
| `llama-server` 5xx or timeout | Model not loaded, VRAM OOM | `.data/logs/llama-server.log` |
| `MongoServerError` | MongoDB not in replica set or not running | `.data/logs/mongod.log`, `rs.status()` |

---

## What NOT to Do

- Do not load the model directly with `FastLanguageModel` in any M3+ code — that's the M2 pattern. Use the llama.cpp HTTP API (`llama-server` on port 8000).
- Do not modify `core/`, `tools/`, or `main.py` — M2 baseline must stay runnable.
- Do not add `console.log` to any MCP server, even for debugging. Use `console.error`.
- Do not use `asyncio.run()` inside an async function — it raises "cannot be called when another event loop is running."
- Do not import `tiktoken` anywhere in this project.
- Do not use `chromadb.PersistentClient` or `chromadb.EphemeralClient`.
