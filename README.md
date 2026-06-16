# Labmate

A local, autonomous polyglot agent for high-end software engineering and professional academic writing. Runs entirely on your own GPU — no cloud API required.

**Architecture: Brain → Nervous System → Hands**

```
┌─────────────────────────────────────────────────────────────┐
│  BRAIN                                                      │
│  Gemma 4 MoE (4-bit) via vLLM  ·  Qwen2.5-Coder-32B       │
│  Python orchestrator · LangGraph state machine              │
│                        │                                    │
│  NERVOUS SYSTEM         ▼                                   │
│  Python MCP client ──► TypeScript MCP server                │
│  stdio JSON-RPC 2.0                                         │
│                        │                                    │
│  HANDS                  ▼                                   │
│  TypeScript skills · Rust skills · Python skills            │
│  AST analysis · Academic writing · Critique & reflexion     │
│                                                             │
│  MEMORY                                                     │
│  MongoDB (sessions) · Chroma (vectors) · Redis (queues)     │
└─────────────────────────────────────────────────────────────┘
```

**Target hardware:** RunPod RTX A6000 (48 GB VRAM) or equivalent local GPU.

---

## Current State

| Milestone | Status | What it does |
|-----------|--------|--------------|
| M1 — Basic inference | ✅ Done | Unsloth model load, single-turn generation |
| M2 — Active agency | ✅ Done | Regex tool loop, AgentMemory, Codegraph |
| M3 — MCP bridge | 🔨 Next | TypeScript MCP server + Python MCP client |
| M4 — Full memory | ⬜ Pending | MongoDB + Chroma + Redis, hybrid RAG |
| M5 — Skills | ⬜ Pending | Polyglot skill framework, SKILL.md discovery |
| M6 — vLLM serving | ⬜ Pending | Replace Unsloth direct load with vLLM API |
| M7 — Discord | ⬜ Pending | Discord bot connector, edit-based streaming |

---

## Prerequisites

**Host (GPU machine / RunPod pod):**
- NVIDIA driver ≥ 525.60.11
- CUDA 12.x
- Python 3.11+
- Node.js 20+
- Docker + Docker CLI

**Verify GPU:**
```bash
./infrastructure/scripts/gpu-check.sh
```

---

## Quick Start

### 1. Start support services (MongoDB, Chroma, Redis)
```bash
./infrastructure/scripts/run-services.sh --infra-only
```

### 2. Start the inference server (host process, not in Docker)
```bash
# Quantize the model first (one-time):
python scripts/quantize.py

# Then serve:
vllm serve google/gemma-4-9b-it \
  --quantization bitsandbytes \
  --tool-call-parser gemma4 \
  --enable-auto-tool-choice \
  --port 8000
```

### 3. Start app services (once images are built)
```bash
./infrastructure/scripts/run-services.sh
```

### 4. Run the current M2 agent (while M3 is in progress)
```bash
pip install -r requirements.txt
python main.py
```

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
├── services/                      # M3+ (to be built)
│   ├── mcp-bridge/                # TypeScript MCP server
│   ├── orchestrator/              # Python M3+ orchestrator (LangGraph)
│   ├── skill-worker/              # Skill execution worker
│   └── skills/                    # Individual SKILL.md skill definitions
│
├── infrastructure/
│   ├── docker-compose.yml         # Support services (mongo, chroma, redis)
│   └── scripts/
│       ├── run-services.sh        # Start all Docker services (no Compose needed)
│       ├── gpu-check.sh           # Verify host GPU setup
│       ├── compose-up.sh          # Docker Compose alternative
│       ├── setup-cluster.sh       # Optional k3d cluster setup
│       └── teardown.sh            # Tear down k3d cluster
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

# Watch orchestrator logs
docker logs -f lm-orchestrator
```

---

## Infrastructure

```bash
# Start everything
./infrastructure/scripts/run-services.sh

# Infra only (mongo + chroma + redis)
./infrastructure/scripts/run-services.sh --infra-only

# Scale skill workers
./infrastructure/scripts/run-services.sh --workers 4

# Status
./infrastructure/scripts/run-services.sh --status

# Stop everything
./infrastructure/scripts/run-services.sh --stop
```
