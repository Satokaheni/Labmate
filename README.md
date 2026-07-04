# Labmate

A local, autonomous polyglot agent for high-end software engineering and professional academic writing. Runs entirely on your own GPU — no cloud API required.

**Architecture: Brain → Nervous System → Hands**

```
┌─────────────────────────────────────────────────────────────┐
│  BRAIN                                                      │
│  Gemma 4 (4-bit GGUF) via llama.cpp (llama-server)          │
│  Python orchestrator · LangGraph state machine               │
│                        │                                    │
│  NERVOUS SYSTEM         ▼                                   │
│  Python MCP client ──► TypeScript MCP server                │
│  stdio JSON-RPC 2.0                                         │
│                        │                                    │
│  HANDS                  ▼                                   │
│  TypeScript skills · Rust skills · Python skills            │
│  AST analysis · Academic writing · Critique & reflexion     │
│                                                             │
│  STATE                                                       │
│  SQLite LocalStore (sessions, turns, auth, checkpoints)      │
│  — one file, one co-located process: services.local.main    │
└─────────────────────────────────────────────────────────────┘
```

Everything above runs as a **single co-located process** (`services.local.main` —
gateway + orchestrator on one asyncio loop) plus the `llama-server` inference
process. There is no MongoDB, Redis, or Chroma, and no Docker — all state lives
in one SQLite database file under `.data/`.

**Target hardware:** Any host with an NVIDIA GPU (RTX A6000 48 GB or equivalent).
RunPod works too, but nothing here requires it.

---

## Current State

| Milestone | Status | What it does |
|-----------|--------|--------------|
| M1 — Basic inference | ✅ Done | Unsloth model load, single-turn generation |
| M2 — Active agency | ✅ Done | Regex tool loop, AgentMemory, Codegraph |
| M3 — MCP bridge | ✅ Done | TypeScript MCP server + Python MCP client |
| M4 — Local state | ✅ Done | SQLite LocalStore (sessions, turns, auth, checkpoints) — replaced MongoDB/Chroma/Redis |
| M5 — Skills | ✅ Done | Polyglot skill framework, SKILL.md discovery |
| M6 — llama.cpp serving | ✅ Done | `llama-server` OpenAI-compatible API (see the vLLM-vs-CUDA note in `infrastructure/local/INSTALL.md`) |
| M7 — Discord | ⬜ Deferred | Discord bot connector, edit-based streaming (do not wire until explicitly instructed) |

---

## Prerequisites

**Host (GPU machine):**
- NVIDIA driver ≥ 525.60.11
- CUDA 12.x
- Python 3.11+
- Node.js 20+

No Docker, no container runtime — everything runs as native host processes.

**Verify GPU:**
```bash
nvidia-smi
```

---

## Quick Start

### 1. One-time install (system + Python + llama.cpp + GGUF model)
```bash
infrastructure/local/install.sh
```

### 2. Start the inference server
```bash
infrastructure/local/serve-model.sh   # Gemma 4 via llama.cpp on :8000, waits until healthy
```

### 3. Start the local harness (one process: gateway + orchestrator, SQLite state)
```bash
infrastructure/local/start.sh
infrastructure/local/status.sh        # all green before testing
```

### 4. Run the CLI
```bash
source infrastructure/local/local.env
PYTHONPATH=. python -m services.cli "Write a Python function that returns the square of a number."
```

See `infrastructure/local/README.md` and `infrastructure/local/INSTALL.md` for
full details, ports, and the auth model (bootstrap admin + admin-created users).

---

## Project Structure

```
labmate/
├── main.py                        # M2 entry point (CLI chat loop)
├── config/
│   └── settings.py                # Model name, URLs, constants
├── core/
│   ├── orchestrator.py            # M2 agent loop (Unsloth + regex tools)
│   └── prompt_manager.py          # System prompt
├── tools/                         # M2 tool implementations
│   ├── memory_tool.py             # AgentMemory HTTP client
│   └── code_tool.py               # Codegraph search
│
├── services/
│   ├── mcp-bridge/                # TypeScript MCP server
│   ├── orchestrator/              # Python orchestrator (LangGraph)
│   ├── local/                     # services.local.main — single co-located
│   │                              #   process: gateway + orchestrator, one loop
│   ├── ws_gateway/                # FastAPI + WebSocket gateway (auth, SQLite stores)
│   ├── cli/                       # WebSocket CLI client
│   ├── frontend/                  # Electron frontend
│   ├── skill-worker/              # Skill execution worker
│   └── skills/                    # Individual SKILL.md skill definitions
│
├── infrastructure/
│   ├── local/                     # LIVE stack: install/start/stop/serve-model,
│   │                              #   native host processes, no Docker, no Mongo/
│   │                              #   Redis/Chroma — SQLite state under .data/
│   └── docker/                    # Legacy Docker Compose stack (superseded by
│                                   #   infrastructure/local/; kept for reference)
│
├── research/
│   └── llm-harness-research/
│       ├── results/               # 17 deep-research JSON files
│       └── specs/                 # 9 engineering spec documents
│           ├── spec_orchestrator.md
│           ├── spec_mcp_bridge.md
│           ├── spec_memory.md
│           ├── spec_inference.md
│           ├── spec_skills.md
│           ├── spec_testing.md
│           ├── spec_writing_skills.md
│           ├── spec_infrastructure.md
│           └── spec_integrations.md
│
├── specs/                         # Legacy M1-M2 specs (reference only)
└── docs/                          # Legacy M1-M2 docs (reference only)
```

---

## Specs

Each component has a dedicated engineering spec in `research/llm-harness-research/specs/`:

| Spec | Covers |
|------|--------|
| `spec_orchestrator.md` | ReAct+Plan loop, LangGraph StateGraph, Goal Tree, parallel fan-out |
| `spec_mcp_bridge.md` | TypeScript MCP server, Python MCP client, stdio JSON-RPC |
| `spec_memory.md` | Three-tier memory, hybrid RAG, transactional outbox |
| `spec_inference.md` | vLLM serving, Gemma 4 tool parser, quantization |
| `spec_skills.md` | SKILL.md format, SkillRunner, SkillRegistry, AST tools |
| `spec_testing.md` | Three-layer test pyramid, pytest-bdd, LLM-as-judge |
| `spec_writing_skills.md` | IMRaD pipeline, citation validation, Critique+Reflexion |
| `spec_infrastructure.md` | Docker setup, host inference, service discovery |
| `spec_integrations.md` | Discord connector, slash commands, streaming |

---

## Adding a Skill

1. Create a `SKILL.md` in `services/skills/<skill-name>/`:

```yaml
---
name: my-skill
description: One sentence describing what this skill does.
trigger: keywords that cause the orchestrator to load this skill
tools:
  - name: my_tool
    description: What this tool does
    inputSchema:
      type: object
      properties:
        input: { type: string }
      required: [input]
model: any
license: MIT
---

# My Skill

Detailed instructions the LLM reads when this skill is activated.
```

2. Implement the skill as a TypeScript/Python/Rust MCP server in the same directory.
3. The `SkillRunner` discovers it automatically on next restart.

---

## Development

```bash
# Run the full test suite (mocked, no GPU required)
pytest -m mocked

# Run live tests (requires inference server running)
LIVE_TESTS=1 pytest -m live

# TypeScript MCP server (development)
cd services/mcp-bridge && npm run dev

# Watch the local harness logs (gateway + orchestrator, single co-located process)
tail -f .data/logs/local.log
```

---

## Infrastructure

The whole stack is two processes: `llama-server` (inference) and
`services.local.main` (gateway + orchestrator, one asyncio loop, SQLite state).
No Docker, no MongoDB, no Redis, no Chroma.

```bash
# One-time install (system + Python + llama.cpp + GGUF)
infrastructure/local/install.sh

# Start the model
infrastructure/local/serve-model.sh

# Start the harness
infrastructure/local/start.sh

# Status
infrastructure/local/status.sh

# Stop everything (data preserved under .data/)
infrastructure/local/stop.sh
```

See `infrastructure/local/README.md` for what runs, and `INSTALL.md` for the
full from-scratch setup (including the llama.cpp-vs-vLLM CUDA gotcha).
The legacy `infrastructure/docker/` Compose stack is superseded and kept only
for reference.
