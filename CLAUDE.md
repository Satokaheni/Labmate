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
| Discord connector | `research/llm-harness-research/specs/spec_integrations.md` |

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
6. **Discord connector** — Only after the orchestrator loop is stable

When starting a component, read its spec first, then look at the existing M2 code for context on what it replaces.

---

## What NOT to Do

- Do not load the model directly with `FastLanguageModel` in any M3+ code — that's the M2 pattern. Use the llama.cpp HTTP API (`llama-server` on port 8000).
- Do not modify `core/`, `tools/`, or `main.py` — M2 baseline must stay runnable.
- Do not add `console.log` to any MCP server, even for debugging. Use `console.error`.
- Do not use `asyncio.run()` inside an async function — it raises "cannot be called when another event loop is running."
- Do not import `tiktoken` anywhere in this project.
- Do not use `chromadb.PersistentClient` or `chromadb.EphemeralClient`.
