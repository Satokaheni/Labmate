# Local-Claw Bootstrap Design
**Date:** 2026-06-15  
**Status:** Approved — ready for implementation planning  
**Scope:** Phase 1 scaffold (`assistant.py`, `setup-infra.sh`, spec expansion)

---

## Purpose & Context

This document is the implementation blueprint for the **bootstrap phase** of Local-Claw. It is written to be consumed directly by the scaffold AI assistant (`assistant.py`) — every section includes explicit interfaces, code stubs, and step-by-step sequences. Vague descriptions are intentional gaps to be flagged, not filled with assumptions.

The bootstrap phase has one goal: **get the orchestrator, MCP bridge, and skills operational so `assistant.py` can be retired and replaced by the full Local-Claw harness.**

The implementation order is:
1. `assistant.py` — the scaffold CLI that reads these specs and builds everything else
2. `setup-infra.sh` — fixed infrastructure script
3. Spec expansion — concrete, LLM-implementable specs for every component
4. Implementation of orchestrator + MCP + skills (driven by the scaffold CLI using these specs)

---

## Section 1: `assistant.py` — Scaffold CLI

### 1.1 Invocation

```bash
python assistant.py                          # default: qwen model, new session
python assistant.py --model gemma           # use Gemma 4B
python assistant.py --model qwen            # use Qwen2.5-Coder-32B (default)
python assistant.py --session .sessions/2026-06-15-10-30.md  # resume session
```

### 1.2 Model Registry

```python
MODELS = {
    "gemma": {
        "name": "unsloth/gemma-4b-it",
        "max_seq_length": 4096,
        "chat_template": "gemma",
    },
    "qwen": {
        "name": "unsloth/Qwen2.5-Coder-32B-Instruct-bnb-4bit",
        "max_seq_length": 32768,
        "chat_template": "qwen-2.5",
    },
}
DEFAULT_MODEL = "qwen"
```

### 1.3 Rich TUI Layout

Print-based (not `Live`/`Layout`) for SSH/RunPod reliability.

```
┌─ LOCAL-CLAW SCAFFOLD ──────────────── model: Qwen2.5-Coder-32B | VRAM: 19.2/32.0 GB ─┐
│  Session: .sessions/2026-06-15-10-30.md                                                │
└────────────────────────────────────────────────────────────────────────────────────────┘

╭─ You ─────────────────────────────────╮
│ How should I structure the MCP server? │
╰───────────────────────────────────────╯

╭─ Qwen2.5-Coder-32B ───────────────────╮
│ [TOOL: read_file("specs/spec_mcp_server.md")]  │
╰───────────────────────────────────────╯

╭─ Tool: read_file ──────────────────────╮
│ <file contents rendered here>          │
╰───────────────────────────────────────╯

╭─ Qwen2.5-Coder-32B ────────────────────╮
│ Based on the spec, here's the structure │
│ ```typescript                           │
│ ...                                     │
│ ```                                     │
╰────────────────────────────────────────╯

You: _
```

**Panel styles:**
| Panel | Border color | Content rendering |
|---|---|---|
| User input | `dim blue` | Plain text |
| Model response | `green` | `rich.Markdown` (auto syntax-highlights code fences) |
| Tool call | `yellow` | Tool name + query in header, result as plain text body |
| Error | `red` | Exception message |

### 1.4 Conversation History & Sliding Window

```python
MAX_WINDOW = 30  # exchanges (60 messages: 30 user + 30 model)

history: list[dict] = []  # {"role": "user"|"assistant", "content": str}

def add_to_history(role: str, content: str) -> None:
    history.append({"role": role, "content": content})
    # Trim oldest pair when window exceeded
    while len(history) > MAX_WINDOW * 2:
        history.pop(0)
        history.pop(0)
```

Dropped messages are written to the session markdown file before removal — nothing is lost, only evicted from the active prompt window.

### 1.5 Tool System

The model emits tool calls inline using the format:
```
[TOOL: tool_name("argument")]
```

The assistant parses **all** tool calls in a response before rendering, executes them sequentially, and injects results back as context before the next generation step.

**Supported tools:**

```python
TOOLS = {
    "search_code": search_code,      # codegraph search in ./project
    "recall_memory": recall_memory,  # AgentMemory GET /search
    "read_file": read_file,          # read any file path relative to cwd
}
```

**Tool signatures:**
```python
def search_code(query: str) -> str:
    # subprocess: codegraph search <query> in ./project
    # returns: stdout or "No structural matches found."

def recall_memory(query: str) -> str:
    # GET http://localhost:3111/search?q=<query>
    # returns: joined content strings or "No relevant memories found."

def read_file(path: str) -> str:
    # reads file at path, returns contents as string
    # max 8000 chars — truncate with notice if larger
    # returns: file contents or "File not found: <path>"
```

**System prompt injected before every conversation:**
```
You are Local-Claw, a coding assistant helping build the Local-Claw orchestrator system.

You have access to three tools. Use them to read specs, search code, and recall past decisions.

TOOL USE FORMAT — output exactly this syntax, one per line:
[TOOL: search_code("query")]
[TOOL: recall_memory("query")]
[TOOL: read_file("path")]

The specs for what you are building live in specs/. Always read the relevant spec before implementing anything.
The project being built lives in project/.
```

### 1.6 Session Persistence

**Session file format** (`.sessions/YYYY-MM-DD-HH-MM.md`):
```markdown
# Session: 2026-06-15 10:30
Model: Qwen2.5-Coder-32B

## Exchange 1
**You:** How should I structure the MCP server?
**Assistant:** [full response]

## Exchange 2
...
```

**Startup sequence:**
1. If `--session <path>` provided → load file → inject as system context: `"Previous session:\n\n{contents}"` (truncated to 4000 chars if large)
2. Else → query AgentMemory: `recall_memory("recent session local-claw")` → inject top results as brief context block
3. Display header panel with model + VRAM info

**Shutdown sequence** (on `exit`, `quit`, `Ctrl+C`):
1. Flush remaining history pairs to session file
2. POST session summary to AgentMemory: `POST /remember` with `{"content": "<session summary>"}`
3. Print: `Session saved to: .sessions/2026-06-15-10-30.md`

### 1.7 VRAM Reporting

```python
import torch

def get_vram_info() -> str:
    if not torch.cuda.is_available():
        return "CPU"
    used = torch.cuda.memory_allocated() / 1e9
    total = torch.cuda.get_device_properties(0).total_memory / 1e9
    return f"{used:.1f}/{total:.1f} GB"
```

Called once after model load and displayed in the header panel.

---

## Section 2: `setup-infra.sh` — Fixed Infrastructure Script

### 2.1 Problems in Current Script
- `set -e` only — pipe failures and unbound vars silently pass
- `apt-get` assumed — RunPod images vary
- No Python venv — pip installs pollute system Python
- No version pinning for npm packages
- Paths hardcoded to `/workspace/Work/local-claw` instead of script-relative
- No service verification after install
- Unsloth pinned to volatile `git+` HEAD

### 2.2 Target Behavior

```bash
# Derives root from script location
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Guards: skip if already installed
command -v node >/dev/null 2>&1 || install_node
command -v codegraph >/dev/null 2>&1 || npm install -g codegraph@latest
...

# Creates venv
python3 -m venv "$ROOT/venv"
source "$ROOT/venv/bin/activate"
pip install -r "$ROOT/requirements.txt"

# Initializes codegraph in project dir
mkdir -p "$ROOT/project"
(cd "$ROOT/project" && codegraph init)

# Starts agentmemory as background process, saves PID
agentmemory &
echo $! > "$ROOT/.agentmemory.pid"

# Verification block
verify_service "AgentMemory" "http://localhost:3111/health"
verify_service "Codegraph"   "codegraph status"
```

### 2.3 `requirements.txt` (generated alongside script)

```
# ML
torch>=2.2.0
xformers
bitsandbytes>=0.43.0
unsloth @ git+https://github.com/unslothai/unsloth.git@v2024.11

# CLI
rich>=13.7.0
requests>=2.31.0
```

---

## Section 3: Spec Expansion Plan

All specs must be written as **agent-implementable blueprints**: explicit function signatures, JSON schemas, code stubs, and failure modes. The scaffold assistant will `read_file` these specs to drive implementation. Vague prose is not acceptable.

### 3.1 Specs to Expand (Existing)

#### `specs/orchestrator.md`
**Add:**
- Prompt templates for each layer (Planner, Executor/ToT, Monitor) as literal strings with `{placeholder}` variables
- Formal TypeScript interface for the State Object
- Retry budget: max 3 REJECTED cycles before aborting with a human-readable failure report
- Token budget per layer call (Planner: 512 tokens, Executor: 1024, Monitor: 256)
- Explicit data flow: what Python object is passed from Planner → Executor → Monitor

#### `specs/skills_framework.md`
**Add:**
- MCP server tool registration format (TypeScript `server.setRequestHandler` pattern)
- Python client: how to spawn subprocess, write to stdin, read stdout JSON-RPC
- JSON Schema for every tool's `input` and `output` — the table rows are the index, the schemas are the contract
- Error propagation: what the Python client does when a tool returns an error code

#### `specs/skills_coding.md`, `skills_writing.md`, `skills_critique.md`, `skills_system.md`
**Expand each table row into a full function spec:**
```markdown
### tool_name
**Runtime:** TypeScript | Rust | Python
**Input:** `{ param: type, ... }`
**Output:** `{ field: type, ... }`
**Logic:** Step-by-step what it does
**Failure modes:** What it returns on each error condition
**Dependencies:** Libraries/binaries required
```

### 3.2 New Specs Required

#### `specs/spec_mcp_server.md` *(NEW)*
The TypeScript MCP server that hosts all skill tools.
- File: `mcp/server/index.ts`
- Transport: stdio JSON-RPC (no HTTP, no auth for local use)
- Tool registration: one file per skill suite (`coding.ts`, `writing.ts`, `critique.ts`, `system.ts`)
- Entry point: spawned as subprocess by Python client
- Must include: full `package.json`, `tsconfig.json`, and the `index.ts` skeleton with one registered tool as example

#### `specs/spec_mcp_client.md` *(NEW)*
The Python class that wraps subprocess communication with the MCP server.
```python
class MCPClient:
    def __init__(self, server_path: str): ...
    def call_tool(self, name: str, args: dict) -> dict: ...
    def list_tools(self) -> list[str]: ...
    def close(self) -> None: ...
```
- Spawns `node mcp/server/index.js` as a subprocess
- Writes JSON-RPC requests to stdin, reads responses from stdout
- Timeout: 30s per tool call
- On timeout: return `{"error": "timeout", "tool": name}`

#### `specs/spec_curator.md` *(NEW — highest priority)*
The Tiered Context Manager. Referenced across all other specs but never defined.
Three tiers:
1. **Working Memory** — current conversation window (Python list, max 30 exchanges)
2. **Semantic Memory** — AgentMemory (past sessions, decisions, notes)
3. **Structural Memory** — Codegraph (AST-level code index)

Must specify:
- When each tier is queried (trigger conditions)
- How results from all three are merged into a single context block
- The `context_prune` algorithm: priority scoring per chunk, target token budget
- Interface: `class Curator: def get_context(self, query: str, token_budget: int) -> str`

#### `specs/spec_state_machine.md` *(NEW)*
Goal Tree persistence and session resume.
- `checkpoint.json` schema (extends the State Object in `orchestrator.md`)
- Written on every task status change (`pending` → `active` → `completed`/`failed`)
- Loaded on resume: restores `global_goal`, `goal_tree`, `negative_constraints`
- Location: `.checkpoints/<session_id>.json`

#### `specs/spec_project_structure.md` *(NEW)*
Canonical file layout for the production build. Every other spec references paths — this is the single source of truth.
```
local-claw/
├── main.py                  # production entrypoint (replaces assistant.py)
├── assistant.py             # scaffold CLI (retired after milestone 3)
├── setup-infra.sh
├── requirements.txt
├── config/
│   └── settings.py
├── core/
│   ├── orchestrator.py      # Plan-Execute-Monitor loop
│   ├── prompt_manager.py    # prompt templates per layer
│   └── state.py             # State Object + checkpoint I/O
├── mcp/
│   ├── client.py            # MCPClient (Python)
│   └── server/
│       ├── package.json
│       ├── tsconfig.json
│       ├── index.ts         # MCP server entrypoint
│       └── skills/
│           ├── coding.ts
│           ├── writing.ts
│           ├── critique.ts
│           └── system.ts
├── tools/
│   ├── memory_tool.py       # AgentMemory wrapper
│   └── code_tool.py         # Codegraph wrapper
├── specs/                   # All spec files (read by assistant.py)
├── docs/
│   └── superpowers/specs/   # Design docs
├── project/                 # The codebase being worked on
└── .sessions/               # Scaffold CLI session logs
```

---

## Implementation Order for Milestone 3

The scaffold assistant (`assistant.py`) should implement these in order, reading the relevant spec before each step:

1. `setup-infra.sh` — fix first, verify services are running
2. `specs/spec_project_structure.md` — establish canonical paths
3. `specs/spec_mcp_server.md` → `mcp/server/index.ts` (+ package.json, tsconfig.json)
4. `specs/spec_mcp_client.md` → `mcp/client.py`
5. `specs/orchestrator.md` (expanded) → `core/orchestrator.py`, `core/state.py`, `core/prompt_manager.py`
6. `specs/spec_curator.md` → integrated into `core/orchestrator.py`
7. `specs/spec_state_machine.md` → `core/state.py`
8. Skill implementations (coding → writing → critique → system), one suite at a time
9. `specs/skills_framework.md` → wire all skills into MCP server
10. Integration test: full Plan → Execute → Monitor loop with one real task

---

## Success Criteria for Bootstrap Phase

- [ ] `assistant.py` runs on RunPod A6000, loads Qwen2.5-Coder-32B in 4-bit, renders Rich TUI
- [ ] Session persists across container restarts via `.sessions/` + AgentMemory
- [ ] `setup-infra.sh` completes without errors on a fresh RunPod instance and verifies all services
- [ ] All new specs written and readable by the scaffold assistant
- [ ] Orchestrator executes a full Plan → Execute → Monitor loop for a real coding task
- [ ] `assistant.py` is retired and replaced by `main.py` using the full skill harness
