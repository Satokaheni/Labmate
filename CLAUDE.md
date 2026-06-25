# Labmate — Agent Coding Guide

This file is for any AI coding agent helping implement Labmate. Read it fully before touching any file.

---

## What This Project Is

Labmate is a local autonomous agent: Brain (LLM) → Nervous System (MCP bridge) → Hands (skills). It runs on a single GPU host. The LLM inference server runs directly on the host; all support services run natively via shell scripts (no Docker — RunPod blocks namespace syscalls).

**Primary model:** Gemma 4 31B 4-bit served via llama.cpp (`llama-server`) with an OpenAI-compatible HTTP API on port 8000. `QWEN_BASE` defaults to `GEMMA_BASE` — both roles run on the same model.

**CRITICAL SECURITY CONSTRAINT:** Discord connector is deferred — do NOT wire, import, or reference it in any active code path until explicitly instructed. Lives in `services/connectors/deferred/`.

---

## Architecture Map

```
                ┌──── SERVER (RunPod / your host) ────────────────────────────┐
                │                                                              │
                │  llama-server  :8000  (llama.cpp, OpenAI-compatible HTTP)   │
                │       │                                                      │
                │  services/orchestrator/     ← Python, asyncio               │
                │       │ stdin/stdout JSON-RPC 2.0                           │
                │  services/mcp-bridge/       ← TypeScript MCP server         │
                │       │ child process                                        │
                │  services/skills/<name>/    ← TypeScript / Rust / Python    │
                │                                                              │
                │  Memory / queues:                                            │
                │    MongoDB  :27017  (sessions, messages, outbox)             │
                │    Chroma   :8765   (vector embeddings)                      │
                │    Redis    :6379   (task queues via Streams, event cache)   │
                │                                                              │
                │  services/ws_gateway/  :8787  ← FastAPI + WebSocket gateway │
                │                                                              │
                └──────────────────┬───────────────────────────────────────────┘
                                   │  WebSocket  ws://<host>:8787/ws
                          ┌────────┴────────────┐
                          │   CLIENT (Mac)       │
                          │  services/cli/       │
                          │  services/frontend/  │
                          └─────────────────────┘
```

---

## Critical Rules

### 1. stdout is sacred in MCP servers
Never call `console.log()`, `print()`, or write to stdout in any MCP server. stdout carries JSON-RPC 2.0. Use `console.error()` / `logging` to stderr.

### 2. anyio cancel scope — Python MCP client
`ClientSession` must enter AND exit in the same asyncio task. One owning task holds the session for its full lifetime — never return a session from an async-with block.

### 3. Gemma tokenizer — never tiktoken
```python
# CORRECT
from transformers import AutoTokenizer
tokenizer = AutoTokenizer.from_pretrained("google/gemma-4-9b-it")
token_count = len(tokenizer.encode(text))
```

### 4. Chroma — always client-server mode
```python
# CORRECT
client = chromadb.AsyncHttpClient(host="chroma", port=8000)
```

### 5. Redis — Streams for queues, not BRPOP
Use `XADD` / `XREADGROUP` / `XACK`. Pin `redis>=5.0,<6` — version 8.x breaks blocking `xreadgroup` on empty streams.

### 6. llama.cpp — every request must set thinking_budget_tokens
Post-April-2026 builds default to `INT_MAX` when unset, causing non-deterministic hangs.
```python
# Planning / coding / writing
extra_body={"thinking_budget_tokens": 2048}

# Tool selection only
extra_body={"thinking_budget_tokens": 0}
```
Also required on every `litellm.acompletion` call: `api_key="not-needed"` (prevents OpenAI SDK credential error).

### 7. MongoDB transactional outbox
Never write to MongoDB and Chroma/Redis in two separate calls. Write document + outbox marker atomically in one MongoDB write; background OutboxWorker projects to Chroma/Redis.

### 8. LangGraph checkpointer
Use `AsyncMongoDBSaver` from `langgraph-checkpoint-mongodb`. Never use `MemorySaver`.

---

## Service URLs

Always read from environment variables. Never hardcode.

```python
INFERENCE_URL = os.getenv("GEMMA_BASE",   "http://localhost:8000/v1")
MONGO_URI     = os.getenv("MONGO_URI",    "mongodb://localhost:27017/labmate")
CHROMA_URL    = os.getenv("CHROMA_URL",   "http://localhost:8765")
REDIS_URL     = os.getenv("REDIS_URL",    "redis://localhost:6379/0")
```

---

## File Naming Conventions

| Language | Convention | Example |
|----------|-----------|---------|
| Python files | `snake_case.py` | `context_manager.py` |
| TypeScript files | `camelCase.ts` | `skillRegistry.ts` |
| TypeScript types | PascalCase | `ToolCallResult` |
| Python classes | PascalCase | `ContextManager` |
| Python functions | `snake_case` | `build_context()` |
| Skill names | `kebab-case` | `ast-repo-map` |
| Docker containers | `lm-<name>` | `lm-mongodb` |

---

## Testing Rules

- Tests live in `tests/` mirroring `services/` structure
- `@pytest.mark.asyncio` on all async tests
- `pytest` + `pytest-asyncio` — no other test runners
- Assert structure, not literal text — LLM output is non-deterministic
- Motor async cursor chains must support `.find().sort().skip()` — all three return `self` in mocks

---

## Spec Reference

| Component | Spec file |
|-----------|-----------|
| Orchestrator loop, LangGraph | `research/llm-harness-research/specs/spec_orchestrator.md` |
| TypeScript MCP server | `research/llm-harness-research/specs/spec_mcp_bridge.md` |
| MongoDB + Chroma + Redis | `research/llm-harness-research/specs/spec_memory.md` |
| llama.cpp serving | `research/llm-harness-research/specs/spec_inference.md` |
| SKILL.md format, SkillRunner | `research/llm-harness-research/specs/spec_skills.md` |
| Testing strategy | `research/llm-harness-research/specs/spec_testing.md` |
| Discord connector (**deferred**) | `research/llm-harness-research/specs/spec_integrations.md` |

---

## Build Order

1. `services/mcp-bridge/` — TypeScript MCP server
2. Memory layer — `StorageManager` (MongoDB + Chroma + Redis)
3. `services/orchestrator/` — Python orchestrator
4. `services/skills/` — individual skill servers
5. `services/skill-worker/` — Redis consumer that dispatches skills
6. `services/cli/` — WebSocket CLI client
7. `services/frontend/` — Electron frontend
8. Discord connector — **deferred; do not implement until explicitly instructed**

---

## Live E2E Verification

Run these after any change to confirm the stack still works. Start services in order:

```bash
infrastructure/local/serve-model.sh   # wait until model healthy
infrastructure/local/start.sh
infrastructure/local/status.sh        # all services must be green before testing
```

### 1. Service health checks
```bash
redis-cli ping                                               # → PONG
mongosh --quiet --eval 'rs.status().myState'                # → 1
curl -s http://localhost:8765/api/v2/heartbeat | head -c 80 # → {"nanosecond heartbeat":...}
curl -s http://localhost:8000/health | grep '"status"'      # → "ok"
curl -fsS http://localhost:8787/healthz                     # → {"ok":true}
```

### 2. Redis round-trip (no CLI, no GPU needed for the push)
```bash
TASK_ID="e2e-$(date +%s)"
redis-cli XADD labmate:goals '*' payload \
  "{\"task_id\":\"$TASK_ID\",\"task\":\"What is 2+2? Reply in one sentence.\",\"session_id\":\"$TASK_ID\"}"
for i in $(seq 1 120); do
  VAL=$(redis-cli GET "labmate:result:$TASK_ID" 2>/dev/null)
  [ -n "$VAL" ] && echo "$VAL" && break; sleep 1
done
```
Success: `{"ok": true, ...}`. Failure: timeout or `"ok": false`.

### 3. Unit tests
```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/ -v 2>&1 | tail -20
```

### 4. One-shot CLI smoke test
```bash
source infrastructure/local/local.env
PYTHONPATH=. python -m services.cli "Write a Python function that returns the square of a number."
```
Success: answer streams live and process exits 0.

### 5. Skill routing eval (run when any skill is added or modified)
```bash
# Generate routing cases for new/changed skills (appends to working set, never touches seed)
python eval/extend_eval.py \
  --skills-dir services/skills \
  --eval eval/routing_eval.jsonl \
  --per-skill 6 \
  --base-url http://localhost:8000/v1 \
  --model gemma-4-31b

# Score routing accuracy across the full catalog
python eval/run_routing_eval.py \
  --eval eval/routing_eval.jsonl \
  --skills-dir services/skills \
  --base-url http://localhost:8000/v1 \
  --model gemma-4-31b \
  --repeats 3 \
  --report eval/reports/
```
Acceptance: new skill ≥ 0.80, no existing skill drops > 0.05. If a skill mis-routes, improve its `SKILL.md` description — that's the routing signal. Never modify `eval/routing_eval.seed.jsonl`.

### 6. Log locations
```
.data/logs/orchestrator.log   ← task complete/failed, exceptions
.data/logs/llama-server.log   ← model load, VRAM, 5xx
.data/logs/ws-gateway.log     ← auth failures, event relay errors
```

| Log pattern | Likely cause |
|-------------|-------------|
| `task failed` + traceback | Exception in `run_task` or LangGraph node |
| `xreadgroup error` | Redis not running or stream not created |
| No `goal received` after XADD | Orchestrator not running or consumer group missing |
| `MCP bridge did not become ready` | Bridge crash or missing `dist/index.js` — run `npm run build` in `services/mcp-bridge/` |
| `llama-server` 5xx / timeout | Model not loaded or VRAM OOM |
| ws_gateway `auth_failed` | JWT credentials wrong or `ADMIN_EMAIL`/`ADMIN_PASSWORD` not seeded |

---

## What NOT to Do

- Do not load the model with `FastLanguageModel` — use the llama.cpp HTTP API
- Do not modify `core/`, `tools/`, or the legacy `main.py`
- Do not add `console.log` to any MCP server (use `console.error`)
- Do not use `asyncio.run()` inside an async function
- Do not import `tiktoken` anywhere in this project
- Do not use `chromadb.PersistentClient` or `chromadb.EphemeralClient`
- Do not use `asyncio.run()` inside an async context
