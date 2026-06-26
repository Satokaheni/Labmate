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

## Implementation Workflow

This workflow applies to every implementation task in this repo. Never deviate from it.

**Per-task loop:**
1. **Haiku implements** — dispatch a fresh Haiku subagent with the full task spec. It writes code, tests, and commits.
2. **Opus judges** — dispatch a fresh Opus subagent to review the commit. If React code was touched, also run `react-doctor` before the judge.
3. If Opus finds issues → Haiku fixes → Opus judges again. Repeat until Opus gives a **pass** verdict.
4. Mark the task complete and move to the next.

**Full-project judge (after all tasks):**
- Dispatch Opus as a full-project reviewer across all commits on the branch.
- If issues found → run the per-task loop on the affected task → full-project judge again.
- Repeat until the full-project Opus judge gives a **pass** verdict.

---

## Harness Robustness — Single-Intent Hardening (2026-06-25)

Branch `feat/harness-robustness` (off `994df92`). Eight features added to harden the single-intent ReAct harness, derived from a comparison with the `hermes-agent` and `openclaw` harnesses. All built TDD/BDD via the Implementation Workflow above (39 tasks, haiku-implement → opus-judge, then per-plan + whole-branch opus review). Suite: orchestrator + memory **684 passed**. Plans live in `docs/superpowers/plans/2026-06-25-*.md`.

| Feature | New module | Wires into | Env knobs (default) |
|---|---|---|---|
| Error classification | `services/orchestrator/error_classifier.py` (`ErrorClass`, `classify_error`) | `graph.py` `execute_node` — replaced the `_NONRETRYABLE_ERROR_MARKERS` substring test; terminal classes skip retry, rate-limited gets bounded backoff | `MAX_RATE_LIMIT_RETRIES=1`, `RATE_LIMIT_BACKOFF_SECONDS=2.0` |
| Tool-loop detection | `services/orchestrator/loop_detection.py` (`LoopDetector`) | `coding_orchestrator.py` `react_execute` — breaks early on repeated/cycling tool calls; emits `loop.detected` | `LOOP_REPEAT_LIMIT=2` |
| Stateful reflection | `collect_prior_reflections()` in `graph.py` | `reflect` node — feeds prior per-goal reflections into the diagnosis prompt (reflection messages now tagged with `goal_id`) | — (uses `REFLECT_THINKING_BUDGET`) |
| Conditional gates | `services/orchestrator/task_complexity.py` (`classify_complexity`, `conditional_gates_enabled`) | `graph.py` `assess_ambiguity` + `ambiguity_router` + `verify_router` — skips ambiguity/verify gates for trivial tasks | `ENABLE_CONDITIONAL_GATES=0` (**OFF by default**), `TRIVIAL_MAX_WORDS=12` |
| Iteration budget | `services/orchestrator/iteration_budget.py` (`IterationBudget`) | `coding_orchestrator.py` `react_execute` — replaced the `range(max_steps)` cap with consume/refund + one-shot grace call + absolute turn cap | `LABMATE_MAX_ITERATIONS` (default = `max_steps`, 6) |
| Prefix-cache stability | `services/orchestrator/prompt_assembler.py` (`PromptAssembler`) | `coding_orchestrator.py` `react_execute` — builds a byte-stable system+tools prefix once per goal so llama.cpp reuses the cached prefix | — |
| Endpoint failover | `services/orchestrator/model_client.py` (`acompletion_with_failover`, `AllEndpointsExhausted`, `resolve_bases`) | `coding_orchestrator.py` — routes architect/editor/react/aggregate/stream model calls; fails over on 5xx/conn/timeout, 4xx is terminal | `LABMATE_FALLBACK_BASES=""`, `LABMATE_MODEL_MAX_ATTEMPTS_PER_BASE=2`, `LABMATE_MODEL_BACKOFF_BASE_S=0.5`, `LABMATE_MODEL_BACKOFF_MAX_S=4.0` |
| Message repair | `services/orchestrator/message_repair.py` (`sanitize_messages`, `message_repair_enabled`) | `coding_orchestrator.py` `_maybe_repair` — drops orphaned tool results and merges illegal adjacent same-role runs right before each model call; idempotent/safe no-op when off | `ENABLE_MESSAGE_REPAIR=0` (**OFF by default**) |
| BDD foundation | `tests/conftest.py` `fake_model` (respx HTTP-seam mock) | pytest-bdd layer: `tests/services/orchestrator/features/*.feature` + `test_*_bdd.py`; `bdd` marker in `pytest.ini` | — |
| Wall-clock + no-progress breaker | `services/orchestrator/progress_breaker.py` (`ProgressBreaker`, `ProgressStep`) | `coding_orchestrator.py` `_run_react_loop` — per-turn wall-clock deadline (injectable clock) + idle breaker that trips after N no-progress turns; both layered on top of `IterationBudget` step counting | `LABMATE_GOAL_DEADLINE_S=600` (0 disables), `LABMATE_NOPROGRESS_LIMIT=5` (0 disables) |

**Notes for testing:**
- **Conditional gates are OFF by default** — export `ENABLE_CONDITIONAL_GATES=1` to exercise them.
- New additive `State` fields: `error_class` (error-classification); `complexity`, `skip_ambiguity`, `skip_verify` (conditional-gates). No removals.
- Error-classification (skill-failure retry policy) and endpoint-failover (transport-error retry) deliberately use **separate** classifiers — the whole-branch review confirmed they are distinct concerns, not a duplication to consolidate.
- BDD step defs run async orchestrator code via a shared async-run helper in `tests/conftest.py`; pytest-bdd scenarios are tagged `@mocked` and use the `fake_model` fixture (no GPU).

### Sequencing & latency (merged from `perf/latency-reduction`, PR #11)

`react_execute` is now a **dispatcher** keyed on `SEQUENCING_MODE`; the harness features above (LoopDetector, PromptAssembler, IterationBudget, failover) live in the extracted **`_run_react_loop`** (so the table rows that say "`react_execute`" now mean `_run_react_loop`). Modes:
- `_run_skill_first(goal)` — deterministic single-skill fast-path; returns `None` when no skill matches.
- `_run_react_loop(goal, max_steps)` — the bounded multi-tool ReAct loop (carries all the harness-robustness features).
- `_replan_loop(goal)` — **opt-in** (`SEQUENCING_MODE=replan`): a planner emits the single next sub-goal (or `done`) and runs each via skill-first with a `_run_react_loop` fallback; the planner owns the completion decision (honest completion). A compound gate (`_is_compound`) runs single-step goals once so simple tasks pay no sequencing tax. Kept opt-in for A/B until its activation-cap bug (below) is fixed.

**Default is `skill_first`** (the well-tested harness path); `replan` and `react` are opt-in via `SEQUENCING_MODE` for A/B evaluation (`eval/seq_ab/`).

Latency/sequencing knobs (defaults): `SEQUENCING_MODE=skill_first`, `MAX_SEQ_STEPS=5`, `REPLAN_COMPOUND_GATE=1`, `ASSESS_THINKING_BUDGET=384` (lighter ambiguity judgement), `CRITIQUE_ARTIFACT_TYPES=""` (auto critique-gate **OFF**; set `writing` or `code,writing` to re-enable), `SKILL_CALL_TIMEOUT=135` (must exceed the worker's `CALL_TIMEOUT`). `test-gen` gained a `run_tests` tool (run an existing suite — do not call `generate` to re-run tests). A/B harness: `bash eval/seq_ab/run_mode.sh <skill_first|react|replan>`.

**Known bug to chase (from the perf branch):** in `replan` mode, `SkillRunner.load_skill` can hit its `max_chain` activation cap mid-chain because `reset_activations()` runs once per goal, not per sub-step — replan runs many sub-steps. Fix candidate: call `reset_activations()` per sub-step inside `_replan_loop`. `skill_first` is unaffected (≤1 skill/goal).

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

### 6. Semantic codegraph search (run when `services/codegraph_embedder/` is changed)

The orchestrator spawns the codegraph MCP server automatically — no separate start needed.

```bash
# Confirm orchestrator picked up the tool at startup
grep "codegraph semantic search ready" .data/logs/orchestrator.log

# Check Chroma collection is populated (~2794 nodes for current codebase)
curl -s http://localhost:8765/api/v1/collections/code_symbols | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('count', d))"

# Semantic query via agent (golden-path test)
redis-cli XADD labmate:goals '*' payload \
  '{"task_id":"cg-test","task":"Find the function that handles WebSocket authentication","session_id":"cg-test"}'
# Expected: result references ws_gateway/server.py near the auth handshake

# Incremental update — touch a file, wait 5 s, check the log
touch services/orchestrator/main.py
sleep 6
grep "incremental_update" .data/logs/codegraph-embedder.log | tail -3
```

`full_index` is skipped on restart if `code_symbols` already has documents. To force a full re-index, delete the collection from Chroma first.

### 7. Log locations
```
.data/logs/orchestrator.log        ← task complete/failed, exceptions
.data/logs/llama-server.log        ← model load, VRAM, 5xx
.data/logs/ws-gateway.log          ← auth failures, event relay errors
.data/logs/codegraph-embedder.log  ← indexer startup, incremental updates
```

| Log pattern | Likely cause |
|-------------|-------------|
| `task failed` + traceback | Exception in `run_task` or LangGraph node |
| `xreadgroup error` | Redis not running or stream not created |
| No `goal received` after XADD | Orchestrator not running or consumer group missing |
| `MCP bridge did not become ready` | Bridge crash or missing `dist/index.js` — run `npm run build` in `services/mcp-bridge/` |
| `codegraph MCP did not become ready` | `.codegraph/codegraph.db` missing or embed model not loaded — check codegraph-embedder.log |
| `llama-server` 5xx / timeout | Model not loaded or VRAM OOM |
| ws_gateway `auth_failed` | JWT credentials wrong or `ADMIN_EMAIL`/`ADMIN_PASSWORD` not seeded |

### 7. Harness-robustness feature checks (the 8 features above)

These are unit/BDD-covered (`PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q` → all green). To exercise them **live** on RunPod, watch `.data/logs/orchestrator.log` while pushing tasks:

```bash
# Conditional gates — OFF by default; enable, then a trivial task should skip the ambiguity + verify gates
ENABLE_CONDITIONAL_GATES=1 infrastructure/local/start.sh   # (or export before starting the orchestrator)
PYTHONPATH=. python -m services.cli "What is 2+2? Reply in one sentence."
#   log: assess_ambiguity skipped (trivial) + verify skipped → faster turn. An ambiguous task ("improve it") still gates.

# Error classification — a skill failing for an environmental/terminal reason must NOT retry to exhaustion
PYTHONPATH=. python -m services.cli "Run this code in the sandbox: print(1)"   # if Docker absent → TERMINAL_DEPENDENCY
#   log: classify_error → terminal class → finalized WITHOUT MAX_GOAL_ATTEMPTS retry loop (fast fail, not 2x reflect).

# Endpoint failover — point primary at a dead port with a working fallback; confirm failover, not a hard error
GEMMA_BASE="http://localhost:9999/v1" LABMATE_FALLBACK_BASES="http://localhost:8000/v1" \
  PYTHONPATH=. python -m services.cli "Say hello."
#   log: primary endpoint conn-refused → failover to fallback → success. All-dead → AllEndpointsExhausted (terminal).

# Iteration budget — a long multi-step task should get a grace call near the cap, not a hard mid-step cut
#   log: "budget exhausted" with a final grace turn; cheap read-only tool turns are refunded.

# Tool-loop detection — if the Q4 model repeats the same tool+args, the loop breaks early
#   log: loop.detected (reason=repeat|cycle) instead of thrashing to the iteration cap.

# Stateful reflection — a goal that fails twice: the 2nd reflect prompt includes the 1st reflection
#   log (reflect node): prior-reflection text present + "do not repeat" instruction on attempt 2.

# Sequencing mode — DEFAULT is skill_first (one skill/goal). The mode is read by the ORCHESTRATOR
# at startup (process-wide), NOT by the CLI — to change it, restart the orchestrator under the env
# var, then push a task. For the proper comparison use the A/B harness in §8 (it restarts per mode).
SEQUENCING_MODE=replan infrastructure/local/start.sh    # restart orchestrator in replan (opt-in) mode
PYTHONPATH=. python -m services.cli "Review /workspace/ab_buggy.py for bugs, then fix the code."
#   skill_first: ONE skill dispatch then finish (honest 'partial' if the skill can't edit code).
#   replan:      planner emits sub-goals (review → fix), multiple steps — watch for the load_skill
#                activation-cap bug mid-chain (see Harness Robustness → known bug).
```

Knobs to tune live: `LOOP_REPEAT_LIMIT`, `TRIVIAL_MAX_WORDS`, `LABMATE_MAX_ITERATIONS`, `MAX_RATE_LIMIT_RETRIES`, `LABMATE_MODEL_MAX_ATTEMPTS_PER_BASE`, `SEQUENCING_MODE`. See the Harness Robustness table for defaults.

### 8. Sequencing A/B test (skill_first vs react vs replan)

`SEQUENCING_MODE` is process-wide (read once at orchestrator import), so each mode needs its own orchestrator restart. The harness in `eval/seq_ab/` automates this: it restarts the orchestrator under a mode, runs a fixed 5-case set (3 compound + 2 controls) through Redis, and records per case the skill sequence, `ok`, llm-call count, and wall-time to `eval/seq_ab/results-<mode>.json`.

```bash
# Run each mode (each call restarts the orchestrator under that mode, then runs the 5 cases):
bash eval/seq_ab/run_mode.sh skill_first   # baseline / current default
bash eval/seq_ab/run_mode.sh react
bash eval/seq_ab/run_mode.sh replan        # opt-in planner loop
# → eval/seq_ab/results-{skill_first,react,replan}.json
```

The 5 cases (`eval/seq_ab/run_seq_ab.py`): c1 test-gen→review→fix, c2 review→fix, c3 bug→test (compound); c4 single review, c5 trivial (controls). Fixtures (`/workspace/ab_*.py`) are reset before each case. Judge the three result files with a **cross-family** model (NOT Gemma/Qwen — self-grading bias) on **completion** (did it actually do the work) and **honesty** (did it claim a success it didn't achieve).

**What to look for:**
- `skill_first`: 1 skill/goal — fast, but on compound tasks may run a read-only skill (test-gen/code-review) and stop, sometimes claiming completion it didn't perform.
- `replan`: sequences sub-goals (review→fix) for honest multi-step completion — but watch the **`load_skill` activation-cap bug** that caps compound completion (fix = call `reset_activations()` per sub-step in `_replan_loop`).
- Controls (c4/c5) should tie across modes; if `replan` over-sequences a control, tune `REPLAN_COMPOUND_GATE` / `_is_compound`.
- The harness-robustness features (loop-detect, budget, prefix, failover) run inside **every** mode's ReAct fallback, so this A/B also stresses them under real load — the first live exercise of the replan↔harness interaction.

> `run_mode.sh` hardcodes `/workspace/Labmate` and writes fixtures under `/workspace/` — **RunPod-only**. On a different host, adjust the paths or run `run_seq_ab.py` directly after starting the orchestrator with the desired `SEQUENCING_MODE`.

---

## What NOT to Do

- Do not load the model with `FastLanguageModel` — use the llama.cpp HTTP API
- Do not modify `core/`, `tools/`, or the legacy `main.py`
- Do not add `console.log` to any MCP server (use `console.error`)
- Do not use `asyncio.run()` inside an async function
- Do not import `tiktoken` anywhere in this project
- Do not use `chromadb.PersistentClient` or `chromadb.EphemeralClient`
- Do not use `asyncio.run()` inside an async context
