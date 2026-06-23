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

## Session Log — 2026-06-23 — READ THIS FIRST

Branch `feat/agent-event-stream`. Everything below is **committed and pushed**. The multi-intent routing saga is **CONCLUDED**: the feature was fixed, A/B-tested, then **replaced by single-intent routing** — the multi-intent decompose tier was removed entirely.

### Routing: DONE — single-intent is the only mode (multi removed)

Each message is now treated as ONE intent; the ReAct executor sequences any sub-steps within a single goal. The original decompose-into-N-goals tier was the source of nearly every routing bug, so it was fixed, evaluated head-to-head, and then deleted. Arc (all pushed):

- **Fixed 5 defects** (`f3c5ba2`): graph never halted on clarification; single-intent clarification escape; "sequential" children ran in parallel (+ dropped results + mid-chain failure not retried); outer-layer guessed-answer leak (`main.py` streamed an answer despite the halt); clarify trigger over-fired (skill-absence ≠ ambiguity).
- **decompose determinism + assess_ambiguity→clarification halt + rubric calibration** (`1ba76f0`, `e7242ae`).
- **Reflect/verify-gate latency knobs** (`925af93`): bounded verify-reflect, early-bail on non-retryable failures, lower retry cap, smaller reflect budget.
- **A/B (single vs multi)** — built a temporary `routing_mode` toggle + harness, ran 3 batches incl. N=5 big multi-deliverable. Opus verdict: **single is non-inferior on quality in every category, ~2.7× cheaper, more reliable; multi adds cost + flakiness with no benefit even on its best case.** Write-ups kept at `eval/reports/ab_routing_report*.md` (the A/B harness/data were pruned once the conclusion was written up).
- **Flipped default to single** (`932b44b`), then **REMOVED multi-intent decompose + the routing_mode toggle entirely** (`4bca4fd`): `decompose()`, `_generate_clarification()`, `ROUTING_MODE`, the chain builder are gone. `route()` is single-intent; `assess_ambiguity` owns clarification. 342 orchestrator tests pass.
- **CLI polish** (`59e2718`, `44ce134`): auto-seed a `default` workspace when none is given (zero-setup sessions; unknown `--workspace` ids are seeded too); clarification affordance ("❓ I need a bit more to proceed:") in one-shot + REPL + the live StreamRenderer (consumes `clarification_request`); readable `assess_ambiguity` reasoning line (no raw JSON). 52 CLI tests pass.

**Live-verified** on the removed-multi stack: trivial→direct answer, skill→one dispatch, compound→one execute pass delivers all parts, genuinely-ambiguous→clarifies. CLI (one-shot + REPL) renders answers and clarifications correctly.

**Env knobs** (`os.getenv`, defaults in code): `MAX_VERIFY_RETRIES=1`, `MAX_GOAL_ATTEMPTS=2`, `REFLECT_THINKING_BUDGET=1500`, `ASSESS_THINKING_BUDGET=768`, `ENABLE_DIRECT_ANSWER_FASTPATH=1`, `DIRECT_ANSWER_THINKING_BUDGET=1024`; llama-server `--parallel 4`.

### Done & pushed this session (late)
- **SearXNG self-hosting** (`6f9c9b0`) for the `web-search` skill — native (Docker-less pod: `install.sh` clones+builds into `/workspace/searxng`, JSON output on, public limiter off; `start.sh`/`stop.sh`/`status.sh` manage it on `:8080`; `local.env` exports `SEARXNG_URL`) + docker (compose bind-mount fix + `SEARXNG_URL` wired into mcp-bridge/orchestrator/skill-worker; `lm-searxng` in `run-services.sh`). Live-verified: `web-search` returns real results.
- **Bare tool names for 3 TS skills** (`14c3b58`): `web-search`, `react-doctor`, `component-doc-gen` exposed prefixed names (`web_search.search`, etc.) — same registry bug as the Python skills, missed earlier because that scan was Python-only. Renamed to bare + rebuilt `dist`. Live-verified (`web-search` dispatches with bare `search`).
- **Full-project review of the day's earlier work** → fixed all findings (local executor hardening + visible auto-fallback, null-fix completeness, approval-emit ordering, run_tests warning field, real-error surfacing) — commits `28bb351`…`2c0b4f5`, all pushed; review loop reached a clean pass.

### Environment / credentials (for tomorrow)
- The user has a **Figma token** and **Semantic Scholar key** to add to a `.env` tomorrow. Vars: `FIGMA_ACCESS_TOKEN` (Figma PAT, scope **`file_content:read`** read-only) and `SS_API_KEY` (optional; Academic Graph + Recommendations APIs; no per-endpoint scopes). `web-search` needs `SEARXNG_URL` (now self-hosted). All other external skills use the local Gemma model or anonymous free APIs (HF, PapersWithCode, OpenAlex). **No paid keys.**
- This pod: no Docker (namespace syscalls seccomp-blocked → `unshare` EPERM), so code-sandbox runs via the local subprocess fallback; web/citation skills reach the network but public APIs rate-limit (Semantic Scholar 429 without a key).

### Other next steps (lower priority)
- The skill-routing eval (`eval/run_routing_eval.py`, `eval/routing_eval*.jsonl`) is unrelated to the removed A/B and still valid — re-run on RunPod after adding skills.
- CLI nicety (deferred): thread a REPL reply back to the clarified goal (today a clarification renders distinctly but the next message starts a fresh task).

---

## Session Log — 2026-06-22 (earlier)

This session installed the full stack on a fresh container, live-verified the event stream + CLI streaming, then found and fixed four bugs (each via a haiku-implement → opus-judge → opus-project-review workflow), and ran the routing eval. Branch: `feat/agent-event-stream`.

**Bring-up (done):** `infrastructure/local/install.sh` (Node 22, MongoDB 8, Redis 7, mongosh, `hf`, all Python + per-skill deps; llama.cpp + the 18GB GGUF were already present and skipped) → `serve-model.sh` → `start.sh`. All services green; Redis round-trip `ok:true`.

**Smoke tests (done, live):** Event stream emits all 6 event types in order (`turn.start → reasoning → tool.start/done → answer.delta → answer.done → turn.done`); CLI one-shot streams live (seed `~/.labmate/identity.json` + a `--workspace` to run non-interactively).

**Four bugs fixed & committed (4 commits on `feat/agent-event-stream`):**
1. **`react_execute` "null" summary** (`coding_orchestrator.py`) — a failed skill returns `{ok:False, error:...}` with no `result`; the code did `json.dumps(None)` → the literal string `"null"`, discarding the real error. Now surfaces `skill_result["error"]`.
2. **11 skills' prefixed tool names → bare** (`ast-search, citation-check, design-critique, design-token-transform, figma-to-component, paper-rag, repo-fault-localize, repo-graph, screenshot-to-component, test-gen`, plus `code-sandbox`). Servers exposed `code_sandbox.run_python`-style names; the registry resolves the name *after* the skill-dir prefix, so the model's bare `run_python` never matched → `SkillUnavailable` → ReAct thrash to `max_steps`. Renamed `Tool(name=)`, the `call_tool` handler, and `SKILL.md` to bare names (completes the 6-skill fix from 2026-06-21). **This was the real cause of the "over-decompose / max_steps" symptom — selection was always correct; invocation was broken.**
3. **`critique` relative-import crash** (`critique_skill.py`) — `from .schemas` broke when the registry spawns `server.py` as a standalone script (no parent package) → critique never registered → the A2 verify gate silently always passed. Now a dual-safe `try: from .schemas … except ImportError: from schemas …`.
4. **code-sandbox `LocalSubprocessExecutor` + Docker→local auto-fallback** (`executor.py`, `server.py`) — this pod blocks namespace syscalls (`unshare` → EPERM), so Docker (and nsjail/bwrap/gVisor) cannot run. Added a subprocess executor with `setrlimit` (CPU/AS/NPROC/FSIZE) + wall-clock timeout. `get_executor()` honors **`CODE_SANDBOX_BACKEND=docker|local`** and otherwise auto-falls-back Docker→local with a **loud stderr warning**. Docker stays the secure default; local mode is unsandboxed (trusted code only). With this, the coding e2e now **succeeds** on this pod (`'labmate'`→`'etambal'`, `ok:true`, 0 retries, 31s).

**Routing eval (done):** `run_routing_eval.py` on the pristine 77-case seed (×3 repeats) → **overall 1.000, stability 1.000, FP-rate 0.0, all 28 skills 1.000, no misroutes**. `extend_eval.py` is a no-op (all 28 skills already covered). A harder fresh-generated adversarial set was also produced (`eval/routing_eval.generated.jsonl`) and scored — see `eval/reports/`.

**New knob:** `CODE_SANDBOX_BACKEND` (`docker` | `local` | unset=auto). Unset → prefer Docker (`client.ping()` guard), fall back to local subprocess with a loud warning. Local = NO fs/network/PID isolation; rlimits + timeout only; trusted code only.

---

## Session Log — 2026-06-20

This session took the M3 stack from "unit tests pass" to a **working, skill-aware orchestrator** running live on the pod. Branches: `fix/e2e-setup-and-redis` (pushed; the e2e + skill-selection milestone) → `feat/agent-event-stream` (current; latency/reliability fixes + the event-stream plan).

**Done & verified live (committed/pushed on `fix/e2e-setup-and-redis`):**
- **Full e2e bring-up** — installed all deps, fixed `install.sh` (missing service deps + skill deps), `start.sh` (stale-bridge rebuild), `stop.sh` (don't kill the model by default). See `docs/e2e-setup-findings.md`.
- **Pinned `redis>=5.0,<6`** — redis-py 8.x raises `TimeoutError` on empty blocking `xreadgroup` under a busy loop (silently killed goal consumption). Loop also defensively catches it.
- **Skill-aware planner + ReAct executor** (`spec_skills §2.2`) — catalog in-context, `load_skill`→`call_skill_tool`, dispatch to the `labmate:skill-tasks` worker; concurrency preserved. Plus real per-subtask reflexion + honest failure propagation.
- **100% skill selection** — `SkillRouter.select()` picks the right skill 18/18 isolated; **14/14 end-to-end** dispatch. Root fix was a deterministic bug: `SkillRunner.load_skill` activation counter never reset (`reset_activations()` now called per goal). Plus a directive that lifted recall and per-sample retries.

**Done this session on `feat/agent-event-stream` (committed, pushed):**
- **Skill tool-name fix** — 6 skills' `SKILL.md` documented tool names with a namespace prefix their servers don't expose (e.g. pdf-parse `pdf_parse.parse` vs exposed `parse`), so the model emitted unusable names → `SkillUnavailable` → reflect-retry loops. Fixed all 6 (`a11y-audit`, `ast-repo-map`, `ast-ts-refactor`, `citation-graph`, `paper-to-slides`, `pdf-parse`) to bare names. pdf-parse now executes `ok=True` 3/3.
- **`plan_tool_call` cache read** — on a repeat `load_skill` the body is omitted (progressive-disclosure dedup); now falls back to `runner.loaded[name]` so plan doesn't return None on already-loaded skills (was forcing the slow ReAct fallback).
- **Agent event stream implemented** (all 6 tasks, 216 tests passing):
  - `services/orchestrator/events.py` — `EventEmitter` class, ContextVar pattern, `XADD` to `labmate:events:<task_id>`, `extract_reasoning()` / `reasoning_summary()` helpers
  - Orchestrator wires `EventEmitter` per goal in `main._handle`; emits `turn.start` / `turn.done`
  - `skill_router.py` emits `reasoning` (node=route) + `tool.start` / `tool.done` around each skill call
  - `coding_orchestrator.py` emits `reasoning` per ReAct turn + `tool.start` / `tool.done` around `run_bash` / `call_skill_tool` + `stream_final_answer()` emits `answer.delta` / `answer.done`
  - `services/cli/event_stream.py` — `tail_events()` XREAD BLOCK consumer (async generator)
- **CLI streaming renderer implemented** (Tasks 1–8, wf1+wf2, all pass):
  - `services/cli/event_stream.py` extended: `EVENTS_PREFIX`, `event_channel()`, `EventStream` class (wraps `tail_events()` with `first(timeout)` / `events()` / `aclose()`), `FIRST_EVENT_TIMEOUT = 2.0`, `run_task_with_streaming()`
  - `services/cli/stream_renderer.py` — `StreamRenderer` pure reducer; handles `turn.start`, `reasoning`, `tool.start`/`tool.done` (flat snake_case fields), `answer.delta`, `answer.done`, `turn.done`; renders `◆ working…`, `⚙/✓/✗ tool rows`, dim italic reasoning, streaming markdown answer
  - `services/cli/redis_client.py` — `subscribe_events()` + `_redis_url`
  - `services/cli/renderer.py` — `stream_live()` drives Rich `Live` loop
  - `services/cli/repl.py` + `services/cli/main.py` — both use `run_task_with_streaming`: if first event arrives in ≤2 s, stream live then read canonical answer from `get_result()`; otherwise fall back to spinner

**Reverted (do NOT reintroduce):** `plan_tool_call` constrained-decoding (`response_format`) regressed tool-name selection; a `plan` fast-path and an LLM profiler were net-neutral/diagnostic.

**Known issues / latency state:** end-to-end is correct (14/14 dispatch) but **slow (~40–85 s/goal)**. Drivers, in order: (1) inherent ~6 s/call on the Q4 model × ~7 calls/goal; (2) **reflect-retry loops on failing skill executions** — many failures here are *environmental* (web-search/citation-graph need network, figma a key, code-sandbox Docker — none available on this pod), so they retry to exhaustion. In production with creds/network they succeed. Next latency lever (not yet done): **cap reflect-retries** on cleanly-failing skills.

## Next Steps: Live Smoke Tests for Event Stream + CLI Streaming

The event stream and CLI streaming renderer are **unit-tested but not yet live-verified**. The next session on RunPod should confirm (1) orchestrator events actually land in Redis Streams, and (2) the CLI picks them up and renders live.

Start the stack first (same order as always):
```bash
infrastructure/local/serve-model.sh   # wait until healthy
infrastructure/local/start.sh
infrastructure/local/status.sh        # verify all services green
```

### Smoke test 1: Verify event stream writes (orchestrator side)

Push a task and watch the Redis Stream for events in parallel:

```bash
# Terminal 1 — push a task and wait for result
TASK_ID="evt-$(date +%s)"
redis-cli XADD labmate:goals '*' payload \
  "{\"task_id\":\"$TASK_ID\",\"task\":\"What is 2+2? Reply in one sentence.\",\"session_id\":\"$TASK_ID\"}"

# Poll result (up to 120 s)
for i in $(seq 1 120); do
  VAL=$(redis-cli GET "labmate:result:$TASK_ID" 2>/dev/null)
  [ -n "$VAL" ] && echo "$VAL" && break
  sleep 1
done
```

```bash
# Terminal 2 — tail the event stream (run while terminal 1 is running)
redis-cli XREAD BLOCK 0 STREAMS "labmate:events:$TASK_ID" 0
```

**Expected events in order:**
1. `turn.start` — task accepted
2. One or more `reasoning` events (`node`, `summary`, `text`)
3. `tool.start` + `tool.done` pairs for each skill/bash call
4. `answer.delta` chunks (streaming final answer)
5. `answer.done` (full final answer)
6. `turn.done` with `"status": "complete"`

**Success:** events 1–6 appear and result has `"ok": true`.
**Failure modes:**
- Stream key missing (`labmate:events:<task_id>` never created) → `events.py` emitter not wiring; check `main._handle` for `EventEmitter` setup
- `turn.start` appears but no `tool.*` → `skill_router.py` or `coding_orchestrator.py` not emitting; check those emit calls
- No `answer.delta` → `stream_final_answer()` not called or failing silently; check orchestrator log

### Smoke test 2: CLI streaming integration (one-shot mode)

```bash
source infrastructure/local/local.env
PYTHONPATH=. python -m services.cli \
  "Write a Python function that returns the square of a number."
```

**What you should see on screen:**
- `◆ working…` line appears immediately (within ≤2 s of submitting)
- Tool rows appear as the orchestrator runs: `⚙ exec_run  <reasoning_why>` → `✓ exec_run  exit 0  (1.2s)`
- Reasoning blocks in dim italic text below the tool rows
- Answer text streaming in progressively (markdown)
- After `turn.done`: clean final markdown answer printed below the live frame
- Process exits with code 0

**Success:** live frame renders before the answer is complete.
**Failure:** spinner appears instead of live frame (fallback path ran) — means no event arrived within 2 s. Check event stream smoke test 1 first.

### Smoke test 3: Fallback path (regression)

Stop the orchestrator so no events are published:
```bash
pkill -f "services.orchestrator"   # or use stop.sh and skip restarting orchestrator
```

Run the CLI again:
```bash
PYTHONPATH=. python -m services.cli "What is 2+2?"
```

**Expected:** spinner (`Working…`) appears, waits 2 s with no events, then polls `get_result()`. Since the orchestrator is stopped, it will timeout after 300 s (or return `"ok": false`). The key assertion is **no crash** and **no traceback** — the fallback path runs cleanly.

Restart the orchestrator after: `infrastructure/local/start.sh`.

### Diagnosing event stream failures

| Symptom | Likely cause | Where to look |
|---------|-------------|---------------|
| No events at all | `EventEmitter` not created in `main._handle` | `orchestrator.log` — look for `turn.start` log line |
| `turn.start` only, no `tool.*` | Skill router / ReAct not emitting | `skill_router.py` lines that call `events.emit("tool.start", ...)` |
| `tool.*` events but no `answer.delta` | `stream_final_answer()` failed | `orchestrator.log` — look for `stream_final_answer failed` warning |
| CLI shows spinner instead of live frame | Events not arriving within 2 s; check stream test 1 first | `FIRST_EVENT_TIMEOUT = 2.0` in `event_stream.py` — increase if orchestrator startup is slow |
| CLI crashes on event parse | Malformed JSON in event field | `redis-cli XRANGE labmate:events:<id> - +` — inspect raw payloads |

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

## Session Log — 2026-06-21

This session implemented the full labmate-implementation-guide.md in order (A3 → C1 → A1/A2 → B1-B4 → C2 → dataset skills), using subagent-driven development (Haiku implement → Opus spec+quality review → Opus full project review).

**A3 — Sandbox bypass guard (done):**
- `services/mcp-bridge/src/tools/exec.ts` — added `SANDBOX_BYPASS_PATTERNS` (8 regexes) and exported `guardRunBash(cmd)`; guard fires before exec and returns `isError: true` with "code-sandbox" message
- `services/orchestrator/coding_orchestrator.py` — extended ReAct system prompt with SANDBOX RULE: `run_bash` is for inspection only; agent code must go through `code-sandbox` skill

**C1 — Eval harness (done):**
- `eval/extend_eval.py` — generates routing eval cases for new skills from SKILL.md frontmatter via Gemma
- `eval/run_routing_eval.py` — scores routing accuracy; reports per-cluster/per-skill breakdown + confusion list
- `eval/routing_eval.seed.jsonl` — 77 pristine seed cases (do NOT append); includes cases for dataset-search, dataset-generation, results-analysis, commit-pr, arxiv-prep, rebuttal-response
- `eval/routing_eval.jsonl` — working set (extend_eval appends here)

**A1 — Ambiguity gate (done):**
- `services/orchestrator/types.py` — added `root_goal`, `assumptions`, `ambiguity`, `blocking_question` to `State`
- `services/orchestrator/graph.py` — `assess_ambiguity` node (6th), `ambiguity_router` (`>= 0.6 → approval`), `AMBIGUITY_THRESHOLD` env var; `build_graph` wires `START → assess_ambiguity → (approval|plan)`; `run_task` seeds `root_goal`

**A2 — Critique verify gate (done):**
- `services/orchestrator/types.py` — added `last_artifact`, `verified`, `critique_score`, `critique_notes` to `State`
- `services/orchestrator/graph.py` — `classify_artifact()` helper, `execute_node` sets `last_artifact`, `verify` node (7th), `verify_router` (`< 0.90 → reflect`), `CRITIQUE_THRESHOLD` env var; `execute → verify → (reflect|check)`
- `services/skills/critique/server.py` — **created** (was missing; worker registry never picked up the skill; verify gate was silently always passing). Uses `_GemmaClient` shim (sync litellm + instructor) + `CritiqueSkill`; runs in `asyncio.to_thread`; returns `{score, verdict, notes}`

**B1-B4 — Four new skills (done, all tests pass):**
- `services/skills/arxiv-prep/` — `clean_source`, `verify_compile`, `anonymize` (diff only, no in-place edit), `package_tarball`, `extract_metadata`
- `services/skills/rebuttal-response/` — `parse_reviews`, `draft_response` (one Gemma call per concern), `coverage_audit`; fix applied: paragraph input now splits on blank lines
- `services/skills/commit-pr/` — `summarize_diff`, `write_commit`, `write_pr`; NEVER runs `git add/commit/push` — reads diff only
- `services/skills/results-analysis/` — `profile_results`, `compare_runs` (Welch t-test + bootstrap CI, seed=0), `make_figures` (Agg backend); `matplotlib.use("Agg")` precedes pyplot import

**C2 — Two-tier skill selection (done):**
- `services/orchestrator/skill_router.py` — extracted `_sample_select(task, thinking_budget)` private method; new `select()`: 3 samples at budget=0, unanimous → return immediately (3 calls total), disagreement → one tiebreak at budget=1024 (4 calls total), all None → return None with no tiebreak

**Dataset skills (done):**
- `services/skills/dataset-search/` — `search_hf_hub`, `search_papers_with_code`, `rank_candidates` (lexical, no LLM); deterministic; SKILL.md cross-references `web-search`, `citation-graph`, `dataset-generation`
- `services/skills/dataset-generation/` — `generate_from_seeds` (Gemma per seed), `format_as_jsonl`, `validate_coverage` (lexical); SKILL.md cross-references `dataset-search`

**make_nodes arity note:** `make_nodes()` now returns 7 nodes: `plan, execute_node, check, reflect, approval, assess_ambiguity, verify`. Any test that unpacks it with a fixed count must use 7 values.

---

## RunPod Task Queue — 2026-06-21

Three tasks to run in order on RunPod. Start the full stack before any of them:

```bash
infrastructure/local/serve-model.sh   # wait until model is healthy
infrastructure/local/start.sh
infrastructure/local/status.sh        # all services must be green before proceeding
```

---

### Task R1: Full skill E2E testing (priority: high)

Six new skills were added this session plus the `critique` skill gained its `server.py` (was previously inert). Re-run the full skill E2E suite to confirm all skills dispatch correctly through Redis Streams → skill worker → MCP server and return valid responses.

**New skills to test first:**
- `arxiv-prep` — call `clean_source` with a sample project dir (can be a temp dir with a dummy `.tex`)
- `rebuttal-response` — call `parse_reviews` with a short mock review text
- `commit-pr` — call `summarize_diff` with a small inline diff string
- `results-analysis` — call `profile_results` with a small CSV file
- `dataset-search` — call `search_hf_hub` with query `"emotion classification"`
- `dataset-generation` — call `generate_from_seeds` with 1–2 seed strings
- `critique` — call `critique` with a short code snippet and a task description; verify `score` is a float in `[0, 1]` and `verdict` is one of `pass/revise/fail`

**Dispatch pattern for each skill via Redis (one-shot test):**
```bash
source infrastructure/local/local.env
TASK_ID="e2e-$(date +%s)"
redis-cli XADD labmate:goals '*' payload \
  "{\"task_id\":\"$TASK_ID\",\"task\":\"<your task description>\",\"session_id\":\"$TASK_ID\"}"
# Poll result
for i in $(seq 1 120); do
  VAL=$(redis-cli GET "labmate:result:$TASK_ID" 2>/dev/null)
  [ -n "$VAL" ] && echo "$VAL" && break; sleep 1
done
```

**Or use the CLI for a cleaner test:**
```bash
source infrastructure/local/local.env
PYTHONPATH=. python -m services.cli "Search the Hugging Face Hub for empathetic dialogue datasets."
PYTHONPATH=. python -m services.cli "Generate 3 synthetic instruction-response examples for empathy training."
PYTHONPATH=. python -m services.cli "Summarize this diff and write a commit message: $(git diff HEAD | head -50)"
```

**What to verify:**
- Each skill routes correctly (check logs for `selected skill: <name>`)
- Each skill returns `{"ok": true, "result": {...}}` — not an error
- The `critique` skill returns `{"score": <float>, "verdict": "<str>", "notes": "<str>"}`
- No skill times out (if it does, check that its `server.py` is on `sys.path` in the worker)

**Also re-run the existing pytest E2E suite:**
```bash
python -m pytest tests/ -v --ignore=tests/services/skills/paper-to-slides \
  --ignore=tests/services/skills/figma-to-component \
  --ignore=tests/services/skills/design-token-transform 2>&1 | tail -20
```
Expected: no new failures beyond the pre-existing collection errors in those three dirs.

---

### Task R2: Routing evaluation + scoring (priority: high)

Extend the eval working set with generated cases for the 6 new skills, then score routing across the full catalog. This confirms new skills route at acceptable accuracy and don't regress neighbors.

**Step 1 — Generate routing cases:**
```bash
python eval/extend_eval.py \
  --skills-dir services/skills \
  --eval eval/routing_eval.jsonl \
  --per-skill 6 \
  --base-url http://localhost:8000/v1 \
  --model gemma-4-31b
```
This appends ~6 positive + disambiguation cases per new skill. The seed file is not touched.

**Step 2 — Score routing:**
```bash
python eval/run_routing_eval.py \
  --eval eval/routing_eval.jsonl \
  --skills-dir services/skills \
  --base-url http://localhost:8000/v1 \
  --model gemma-4-31b \
  --repeats 3 \
  --report eval/reports/
```

**Acceptance thresholds:**
| Check | Threshold |
|---|---|
| New skill per-skill accuracy | ≥ 0.80 |
| Neighbor regression (any existing skill) | drop ≤ 0.05 from baseline |
| Overall accuracy | ≥ 0.85 |

**Key confusion pairs to watch:**
- `dataset-search` vs `web-search` (both search, but dataset-search hits HF/PWC APIs)
- `dataset-generation` vs `dataset-search` (create vs find)
- `dataset-search` vs `citation-graph` (datasets vs paper citations)
- `arxiv-prep` vs `academic-writing` (package finished paper vs draft it)
- `commit-pr` vs bridge `git_*` tools (generate prose vs read repo state)
- `results-analysis` vs `paper-rag` (local result files vs literature retrieval)

If a skill is mis-routing, improve its SKILL.md description cross-references — the `description` field is the routing signal for `select()`. Rerun scoring after each fix. Do **not** modify `routing_eval.seed.jsonl`.

---

### Task R3: Event streaming E2E (priority: medium)

The graph now has 7 nodes (added `assess_ambiguity` and `verify` this session). Confirm events emit correctly from all nodes, including the two new ones.

**Smoke test — full event stream with new nodes:**
```bash
source infrastructure/local/local.env
TASK_ID="evt-$(date +%s)"

# Terminal 1: submit and poll result
redis-cli XADD labmate:goals '*' payload \
  "{\"task_id\":\"$TASK_ID\",\"task\":\"Write a Python function to reverse a string.\",\"session_id\":\"$TASK_ID\"}"
for i in $(seq 1 120); do
  VAL=$(redis-cli GET "labmate:result:$TASK_ID" 2>/dev/null)
  [ -n "$VAL" ] && echo "$VAL" && break; sleep 1
done

# Terminal 2: watch events (run before submitting)
redis-cli XREAD BLOCK 120000 STREAMS "labmate:events:$TASK_ID" 0
```

**Expected event sequence with new nodes:**
1. `turn.start`
2. `reasoning` from `assess_ambiguity` node (`node: "assess_ambiguity"`)
3. `reasoning` from `route` node (skill selection, `node: "route"`)
4. `tool.start` + `tool.done` for skill execution
5. `reasoning` from `verify` node if artifact is code or writing (`node: "verify"`)
6. `answer.delta` chunks → `answer.done`
7. `turn.done` with `"status": "complete"`

**Specific checks:**
- `assess_ambiguity` emits a `reasoning` event with `ambiguity` score in the summary
- If ambiguity ≥ 0.6, a `turn.blocked` or approval event should appear before planning
- `verify` emits a `reasoning` event with critique score when the artifact is code or writing
- If critique score < 0.90, a `reflect` loop occurs (look for repeated `tool.start`/`tool.done`)

**CLI streaming check:**
```bash
PYTHONPATH=. python -m services.cli \
  "Write a Python function that returns the square of a number."
```
Expected: live frame renders with tool rows including the verify step. See the existing "Smoke test 2" section below for full expected output format.

**If events are missing from new nodes:** check that `assess_ambiguity` and `verify` in `services/orchestrator/graph.py` both call `await events.emit(...)`. The emit calls should already be there — if not, add them following the pattern in the `plan` and `execute_node` functions.

---

## Eval Harness — Adding a New Skill

Every new skill under `services/skills/<name>/` with a valid `SKILL.md` frontmatter is **automatically discovered** by `extend_eval.py` and `run_routing_eval.py`. No code changes to the eval scripts are needed.

After adding a skill, run this on RunPod (requires live Gemma endpoint):

### Step 1 — Generate routing cases for new skills

```bash
# Appends ~6 cases per new skill to routing_eval.jsonl (does not touch the seed)
python eval/extend_eval.py \
  --skills-dir services/skills \
  --eval eval/routing_eval.jsonl \
  --per-skill 6 \
  --base-url http://localhost:8000/v1 \
  --model gemma-4-31b
```

`extend_eval.py` reads each `SKILL.md`, generates positive routing cases ("use this skill for X") and disambiguation cases ("prefer this skill over Y"), and appends them to the working set. The seed file (`routing_eval.seed.jsonl`) is never modified.

### Step 2 — Score routing accuracy

```bash
# Scores routing across the full eval set; saves report to eval/reports/
python eval/run_routing_eval.py \
  --eval eval/routing_eval.jsonl \
  --skills-dir services/skills \
  --base-url http://localhost:8000/v1 \
  --model gemma-4-31b \
  --repeats 3 \
  --report eval/reports/
```

The runner reports:
- **Overall accuracy** across all clusters
- **Per-cluster accuracy** (code_nav, ui_gen, academic_grounding, research_data, etc.)
- **Per-skill accuracy** for every skill in the catalog
- **Confusion list** — which tasks routed to the wrong skill

### What to check

- The new skill's per-skill accuracy should be ≥ 0.80
- No existing skill's accuracy should drop by more than 0.05 (neighbor regression)
- If a skill bleeds into a neighbor (e.g. `dataset-search` steals from `web-search`), improve the SKILL.md description cross-references (see C4 template in the implementation guide) — the description is the routing signal

### Seed file discipline

```
eval/routing_eval.seed.jsonl  ← PRISTINE — never append, never modify
eval/routing_eval.jsonl       ← working set — extend_eval appends here
```

To reset the working set to seed: `cp eval/routing_eval.seed.jsonl eval/routing_eval.jsonl`

---

## What NOT to Do

- Do not load the model directly with `FastLanguageModel` in any M3+ code — that's the M2 pattern. Use the llama.cpp HTTP API (`llama-server` on port 8000).
- Do not modify `core/`, `tools/`, or `main.py` — M2 baseline must stay runnable.
- Do not add `console.log` to any MCP server, even for debugging. Use `console.error`.
- Do not use `asyncio.run()` inside an async function — it raises "cannot be called when another event loop is running."
- Do not import `tiktoken` anywhere in this project.
- Do not use `chromadb.PersistentClient` or `chromadb.EphemeralClient`.
