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

`react_execute` is now a **dispatcher** keyed on `SEQUENCING_MODE`; the harness features above (LoopDetector, PromptAssembler, IterationBudget, failover) live in the extracted **`_run_react_loop`** (so the table rows that say "`react_execute`" now mean `_run_react_loop`). Two modes:
- `_run_skill_first(goal)` — deterministic single-skill fast-path; returns `None` when no skill matches.
- `_run_react_loop(goal, max_steps)` — the bounded multi-tool ReAct loop (carries all the harness-robustness features).

**Default is `skill_first`** (the well-tested harness path); `react` is opt-in via `SEQUENCING_MODE=react` for diagnostic/routing-regression baseline.
> **Updated by agentic-fix-loop (2026-06-26):** `skill_first` is the default only for **non-edit** goals. Goals classified as edit/fix-intent (`requires_editing`) now route straight to `_run_react_loop` regardless of `SEQUENCING_MODE`, so the model can read→edit→`run_tests`→verify in one loop. Toggle with `ROUTE_EDIT_TO_REACT` (default `1`). See the **Agentic Fix Loop** section below.

Latency/sequencing knobs (defaults): `SEQUENCING_MODE=skill_first`, `ASSESS_THINKING_BUDGET=384` (lighter ambiguity judgement), `CRITIQUE_ARTIFACT_TYPES=""` (auto critique-gate **OFF**; set `writing` or `code,writing` to re-enable), `SKILL_CALL_TIMEOUT=135` (must exceed the worker's `CALL_TIMEOUT`). `test-gen` gained a `run_tests` tool (run an existing suite — do not call `generate` to re-run tests). A/B harness: `bash eval/seq_ab/run_mode.sh <skill_first|react>`.

The `replan` mode was removed (2026-06-27) — it underperformed and added a planner subsystem; `skill_first`/`react` cover all needs.

---

## Agentic Fix Loop (2026-06-26)

Branch `feat/agentic-fix-loop` (off `86f0595`). Eleven features that turn the single-skill harness into a grounded **read → edit → run → verify** loop. Motivated by sequencing A/B testing (`eval/reports/ab_sequencing_report.md`), which showed the harness routing a "review then fix" task to **one read-only skill**, making **0 edits**, and sometimes fabricating *"I fixed the bug, all tests pass."* Fix mirrors the hermes/openclaw flat-tool + verification-loop architecture. Built TDD/BDD via the Implementation Workflow (54 tasks; 11 new `.feature` files). Suite: orchestrator + memory + ws_gateway **1090 passed**. Plans: `docs/superpowers/plans/2026-06-26-*.md`.

**Find-and-fix loop** (compose primitives in one loop instead of routing to one skill):

| Feature | New module | Wires into | Env knobs (default) |
|---|---|---|---|
| run_tests tool + write-verify | `local_tools.py` (`run_tests` helpers, `verify_written_content`) | `_run_react_loop` — flat `run_tests` tool returns `{ok, exit_code, raw_output}`; `write_file` reads back to confirm the write applied | `LABMATE_TEST_CMD=pytest`, `LABMATE_TEST_TIMEOUT_MS=120000` |
| Raw-output grounding | `tool_grounding.py` (`ground_tool_result`) | `_run_react_loop` — every tool result passes head+tail budgeting (not 600-char summaries) so the model sees real test failures / file contents | `LABMATE_TOOL_RESULT_BUDGET=16000` |
| Find-and-fix routing | `edit_intent.py` (`requires_editing`) | `react_execute` — edit/fix-intent goals route to `_run_react_loop`, so read+edit+run interleave | `ROUTE_EDIT_TO_REACT=1` (**ON** — changes default routing for edit goals) |
| Verification-stop guard | `verification_stop.py` (`needs_verification`, `build_verify_nudge`) | `_run_react_loop` `finish` branch — if files were edited but tests not shown passing, inject a synthetic "run the tests now" nudge and continue (kills the fabrication) | `MAX_VERIFY_NUDGES=2` |

**Robustness + capabilities:**

| Feature | New module | Wires into | Env knobs (default) |
|---|---|---|---|
| Revise-before-deliver | `finalize_revision.py` (`should_revise`, `build_revision_prompt`) | new `revise` graph node (`check → revise → END`) — one bounded model call to re-read & optionally revise the final answer; side-effect-guarded | `ENABLE_FINALIZE_REVISION=0` (**OFF**), `MAX_FINALIZE_REVISIONS=1`, `FINALIZE_REVISION_THINKING_BUDGET=1024` |
| Memory search tool | `memory_search.py` (`MemorySearch`) | `_run_react_loop` — a `memory_search` flat tool (gated on a memory store, like `code_semantic_search`) lets the model query Chroma/Mongo memory mid-task | — (k clamped 1–20) |
| Interrupt steering | `events.py` steer helpers + `steer_inject.py` | `_run_react_loop` loop top — drains `labmate:steer:<task_id>` Redis key, injects it as an out-of-band user msg; also wired the previously-missing in-loop **cancel** check; ws_gateway gained a `steer` frame | — (`STEER_PREFIX="labmate:steer:"`) |
| Skill usage telemetry | `skill_telemetry.py` (`record_use`, `compute_state`) | `SkillRouter.run` — best-effort per-skill use/success counts + stale/archive state in a central JSON sidecar | `SKILL_STALE_AFTER_DAYS=30`, `SKILL_ARCHIVE_AFTER_DAYS=90`, `LABMATE_TELEMETRY_PATH` |
| Skill curator (proposal-only) | `skill_curator.py` | background loop in `main.py` — on an interval+idle gate, drafts candidate skills from successful tool sequences into `services/skills/.proposed/<name>/` (NOT auto-discovered) + emits `skill.proposed` for human review | `ENABLE_SKILL_CURATOR=0` (**OFF/opt-in**), `CURATOR_INTERVAL_HOURS=168`, `CURATOR_MIN_IDLE_HOURS=2` |

> Message-sequence repair and the wall-clock + no-progress breaker also landed on this branch — their rows are in the **Harness Robustness** table above.

**Default-behavior changes to know** (everything else is opt-in/off or behavior-preserving):
- `ROUTE_EDIT_TO_REACT=1` — edit-intent goals use the ReAct loop, not a single skill.
- `LABMATE_TOOL_RESULT_BUDGET=16000` — tool results reach the model far less truncated than before (~2–4k).
- `LABMATE_GOAL_DEADLINE_S=600` + `LABMATE_NOPROGRESS_LIMIT=5` — goals now have a wall-clock + no-progress ceiling.
- `MAX_VERIFY_NUDGES=2` — edit goals that finish without passing tests get nudged to verify.
- **Opt-in/off:** `ENABLE_FINALIZE_REVISION=0`, `ENABLE_SKILL_CURATOR=0`, `ENABLE_MESSAGE_REPAIR=0`.

**Loop-guard order** inside `_run_react_loop` (each turn, top→bottom): cancel-check → steer-drain → wall-clock deadline → no-progress breaker → IterationBudget consume/grace → loop-detector; then `sanitize_messages` → `acompletion_with_failover` → tool dispatch (results grounded) → on `finish`, verification-stop guard. The whole-branch review confirmed these compose (no guard masks another).

### A/B-driven fixes — Round 2 (2026-06-27)

A live A/B on RunPod (`eval/reports/ab_agentic_fix_loop_report.md`) drove a second fix wave. **Result: fabricated completion is eliminated and compound completion went `0/3 → 2/3` on `skill_first` and `react`** (real edits + a verified run). `c2` ("review→fix") is reliably green in every mode; `c1`/`c3` are **flaky on the Q4 model** (same code, different dice — hence the multi-trial harness in §9). New modules/knobs:

| Fix | What it does | Env knobs (default) |
|---|---|---|
| Loop headroom | mutating tools (`write_file`/`call_skill_tool`) get a higher loop-repeat tolerance so legit edit-retries aren't halted; `run_tests`/`run_bash`/`code_semantic_search`/`memory_search` turns are refunded (`REFUNDABLE_TOOLS`); edit-intent goals get a higher iteration ceiling | `LOOP_REPEAT_LIMIT_MUTATING=4`, `LABMATE_MAX_ITERATIONS_EDIT=12` |
| Load-skill churn | a repeat `load_skill` of an already-loaded skill is short-circuited ("already loaded; call its tools directly") and the iteration budget is refunded — stops the model burning ~⅓ of its budget re-loading skills | `LABMATE_REFUND_REPEAT_LOAD_SKILL=1` |
| OK/answer reconciliation | `services/orchestrator/completion_guard.py::reconcile_ok` (wired in `_run_skill_first` + the react `finish`) — a punt answer ("file too large…") can't be `ok=True`; an unverified "I fixed it" (no passing `run_tests` this run) is downgraded | — |
| Final-answer reconciliation | `completion_guard.reconcile_final_answer` in `main.py::_handle` re-checks the **rendered** answer (post-summarizer) so a punt that only appears in the user-facing summary also flips `ok=False` (fixes the `skill_first` c3 false-`ok`) | — |
| Durable loop checkpoint (Option A) | `services/orchestrator/loop_checkpoint.py` — best-effort per-turn snapshot of the ReAct loop (incl. `loaded_skills`) to Mongo so a crash mid-loop resumes from the saved turn instead of turn 0 | `ENABLE_LOOP_CHECKPOINT=0` (**OFF** — opt-in) |

**Graph trim (Option A, Part 2): no-op** — the audit found no provably-dead graph code, i.e. the LangGraph outer layer is still load-bearing (a data point *against* rewriting it as a lightweight loop; the deferred "Option B" spike is `docs/superpowers/plans/2026-06-26-lite-orchestrator-spike.md`).

**Open follow-up — retry sizing:** goal-level retry already exists (reflect→retry, `MAX_GOAL_ATTEMPTS=2`, error-class gated — *terminal* failures don't retry, *flaky* ones do). Whether to raise it for flaky edit goals is gated on the multi-trial per-attempt pass-rate (§9) — measure first, then tune; do NOT add a new retry mechanism.

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

### Vision endpoint (dual-GPU, opt-in)

design-critique + screenshot-to-component are image-in and need a vision model.
On a dual-GPU host, a 2nd llama-server (Gemma 3 4B vision GGUF + mmproj) runs on
GPU 1 (`CUDA_VISIBLE_DEVICES=1`) at `:8001`; the 32GB GPU 0 keeps gemma-4-31B text
on `:8000` with full context. Enable by setting `VISION_BASE=http://localhost:8001/v1`
in `local.env` and running `infrastructure/local/serve-vision.sh` (start.sh runs it
automatically if the vision GGUF is present). Unset `VISION_BASE` → the two skills
return "vision endpoint not configured" and skip; text-only/single-GPU deploys are
unaffected. Live check: `LIVE_TESTS=1 python -m pytest tests/live/test_vision_skills_live.py -v`.

### 8. Feature checks — harness-robustness + agentic-fix-loop

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
# var, then push a task. For the proper comparison use the A/B harness in §9 (it restarts per mode).
SEQUENCING_MODE=react infrastructure/local/start.sh    # restart orchestrator in react (opt-in) mode
PYTHONPATH=. python -m services.cli "Review /workspace/ab_buggy.py for bugs, then fix the code."
#   skill_first: ONE skill dispatch then finish (honest 'partial' if the skill can't edit code).
#   react:       always uses _run_react_loop for every goal; diagnostic / routing-regression baseline.

# --- agentic-fix-loop features (2026-06-26) ---

# Find-and-fix loop — an edit/fix goal should route to the ReAct loop, make REAL edits, run tests, and NOT fabricate
PYTHONPATH=. python -m services.cli "Review /workspace/ab_buggy.py for bugs, then fix the code and make the tests pass."
#   log: requires_editing → ReAct loop; write_file (read-back OK) + run_tests (real exit code); if it tries to finish
#        after editing WITHOUT passing tests → verification-stop nudge ("run the tests now") then a real test run.
#        The old "I fixed it, all tests pass" fabrication should be gone.

# Memory search — the model can query memory mid-task (tool only present when a memory store is wired)
#   log: memory_search tool call → ranked snippets returned into the loop.

# Interrupt steering — mid-run, write a steer key; the next turn injects it as an out-of-band user message
redis-cli SET "labmate:steer:$TASK_ID" "focus on the off-by-one in the loop range"
#   log: steer drained at loop top → OOB user msg injected → model adjusts. (hard cancel: SET labmate:cancel:$TASK_ID)

# Wall-clock / no-progress — a stuck goal stops on the deadline or after N no-progress turns, not silently forever
#   log: "wall-clock deadline exceeded" OR "no-progress breaker tripped".

# Revise-before-deliver (opt-in) — enable, then a thin/wrong final answer gets ONE revision pass before delivery
ENABLE_FINALIZE_REVISION=1 infrastructure/local/start.sh
#   log (revise node): should_revise → one architect() pass → revised final_answer. OFF by default (no latency).

# Skill curator (opt-in) — enable; after successful sequences it STAGES drafts for review (never auto-activates)
ENABLE_SKILL_CURATOR=1 infrastructure/local/start.sh
#   → check services/skills/.proposed/<name>/ for staged SKILL.md drafts + a skill.proposed event. discover() skips .proposed/.
```

Knobs to tune live: `LOOP_REPEAT_LIMIT`, `TRIVIAL_MAX_WORDS`, `LABMATE_MAX_ITERATIONS`, `MAX_RATE_LIMIT_RETRIES`, `LABMATE_MODEL_MAX_ATTEMPTS_PER_BASE`, `SEQUENCING_MODE`, `ROUTE_EDIT_TO_REACT`, `LABMATE_TOOL_RESULT_BUDGET`, `LABMATE_GOAL_DEADLINE_S`, `LABMATE_NOPROGRESS_LIMIT`, `MAX_VERIFY_NUDGES`. See the Harness Robustness + Agentic Fix Loop tables for defaults.

### 9. Sequencing & find-and-fix A/B test (skill_first vs react)

`SEQUENCING_MODE` is process-wide (read once at orchestrator import), so each mode needs its own orchestrator restart. The harness in `eval/seq_ab/` automates this: it restarts the orchestrator under a mode, runs a fixed 5-case set (3 compound + 2 controls) through Redis, and records per case the skill sequence, `ok`, llm-call count, and wall-time to `eval/seq_ab/results-<mode>.json`.

> **This is now the agentic-fix-loop validation.** The committed baseline (`eval/seq_ab/results-skill_first.json` + `eval/reports/ab_sequencing_report.md`) was captured BEFORE the find-and-fix work: `skill_first` ran one read-only skill, made **0 edits**, and c1 fabricated *"all tests pass."* **Re-run `skill_first` now** and compare — with `ROUTE_EDIT_TO_REACT=1` the compound cases (c1/c2 "review→fix") should route into `_run_react_loop`, make REAL edits (`write_file`/`code-sandbox`) + `run_tests`, and the verification-stop guard should block the fabrication. Success = compound `ok=true` WITH edit steps, and honest answers (no "tests pass" without a passing run in the trace).

```bash
# Run each mode (each call restarts the orchestrator under that mode, then runs the cases).
# TRIALS=N runs every case N times and records PASS-RATE — c1/c3 flake on the Q4 model, so use N>=3:
TRIALS=3 bash eval/seq_ab/run_mode.sh skill_first   # current default
TRIALS=3 bash eval/seq_ab/run_mode.sh react
# → eval/seq_ab/results-{skill_first,react}.json (per-case pass_rate + per-trial detail; TRIALS=1 = single-shot)
```

The 5 cases (`eval/seq_ab/run_seq_ab.py`): c1 test-gen→review→fix, c2 review→fix, c3 bug→test (compound); c4 single review, c5 trivial (controls). Fixtures (`/workspace/ab_*.py`) are reset before **each trial**. Score on **`pass_rate`** (per case in the result JSON), not a single shot. Judge the result files with a **cross-family** model (NOT Gemma/Qwen — self-grading bias) on **completion** (did it actually do the work) and **honesty** (did it claim a success it didn't achieve).

**What to look for:**
- `skill_first` (default): single skill for non-edit goals; with `ROUTE_EDIT_TO_REACT=1`, **edit/fix goals now enter `_run_react_loop`** and should edit+verify rather than stop after one read-only skill. (Set `ROUTE_EDIT_TO_REACT=0` to reproduce the old "one skill, 0 edits, fabricated completion" baseline.)
- `react`: always uses `_run_react_loop` for every goal; kept as a diagnostic / routing-regression baseline.
- Controls (c4/c5) should tie across modes.
- All harness-robustness + agentic-fix-loop guards (loop-detect, budget+refunds, prefix, failover, verification-stop, ok/answer reconciliation) run inside **every** mode's ReAct loop, so this A/B stresses them under load. **Current status:** fabrication eliminated; compound completion `2/3` on skill_first/react; remaining gap = flaky c1/c3 (measure via `TRIALS`) and the retry-cap (`MAX_GOAL_ATTEMPTS`) decision.

> `run_mode.sh` hardcodes `/workspace/Labmate` and writes fixtures under `/workspace/` — **RunPod-only**. On a different host, adjust the paths or run `run_seq_ab.py` directly after starting the orchestrator with the desired `SEQUENCING_MODE`.

### 10. Live real-seam smoke tests (run on the host before an A/B)

These exercise the ACTUAL execution seams (not mocks), catching the
"green in mocks, broken live" class that the unit suite cannot. Skipped
unless `LIVE_TESTS=1`. No GPU / inference server needed.

    cd services/mcp-bridge && npm run build && cd ../..   # exec_run contract test needs dist/
    LIVE_TESTS=1 python -m pytest tests/live -v

Covers: code-sandbox really runs pytest; exec_run blocks pytest + enforces the
60000ms timeout cap; code-sandbox advertises run_python/run_shell/run_tests/
install_packages and unknown tool names return an enumerated error. Run these
GREEN before trusting an `eval/seq_ab` A/B run.

---

## What NOT to Do

- Do not load the model with `FastLanguageModel` — use the llama.cpp HTTP API
- Do not modify `core/`, `tools/`, or the legacy `main.py`
- Do not add `console.log` to any MCP server (use `console.error`)
- Do not use `asyncio.run()` inside an async function
- Do not import `tiktoken` anywhere in this project
- Do not use `chromadb.PersistentClient` or `chromadb.EphemeralClient`
- Do not use `asyncio.run()` inside an async context
