# Conversation Continuity — Unified Store (Tier 2 activation) Implementation Plan

> **For agentic workers:** Repo uses the CLAUDE.md Implementation Workflow (Haiku implements a task from its spec + tests + commits → Opus judges → fix until pass → next). Branch: `fix/frontend-session-bugs` (PR #26). Checkbox (`- [ ]`) steps.

**Goal:** Give the model multi-turn memory by activating the existing rolling-summary compactor (`services/memory/context_manager.py`) — reading conversation from the SINGLE `chat_turns` store (the ws_gateway's durable, immutable turn record) and injecting the assembled summary + recent turns into the ReAct loop, so a follow-up ("is it NP-complete?") sees prior turns instead of "Which problem?".

**Architecture:** Unify onto ONE conversation store — `chat_turns` (append-only, immutable; already written by `MongoSessionStore`). The memory engine reads it **non-destructively**: recent turns come verbatim from `chat_turns` after a Redis `summarized_through` **seq watermark**; older turns are folded into the Redis rolling `summary:{session_id}`. The destructive `db.messages` path (delete/strip) is retired — `chat_turns` holds final-answer-lean turns (no `role:'tool'` bloat), so nothing needs stripping. `build_context()`'s summary+recent are injected into `_run_react_loop`'s `messages` **after** the byte-stable PromptAssembler system prefix (prefix-cache safe), **before** the goal.

**Tech Stack:** Python 3.11 + Motor (async Mongo) + `redis.asyncio`; pytest + pytest-asyncio; the orchestrator's `StorageManager.context_manager` (already wired to Mongo+Redis).

## Global Constraints
- **One store:** `chat_turns` is the single source of truth for conversation turns. Do NOT reintroduce `db.messages` writes for conversation.
- **Immutable / non-destructive:** never delete or mutate `chat_turns` from the memory engine (the UI replays it). Compaction advances a watermark + updates the Redis summary only.
- **Prefix-cache safe:** the PromptAssembler `system_message()` stays byte-identical at index 0; injected memory goes AFTER it, BEFORE the goal.
- **Best-effort:** any memory read/summary failure logs to stderr and degrades to "no continuity this turn" — never breaks the ReAct loop. No stdout/`print`.
- **Field mapping:** `chat_turns` is camelCase (`sessionId`, `text`, `createdAt`) — shared with the TS frontend; the memory engine must query those names, mapping `text`→its internal `content`.
- **Single-model latency:** summary generation is a full Gemma call — it stays threshold-gated (existing `FULL_THRESH` auto-compact trigger), NEVER per-turn.
- Tests in `tests/` mirroring `services/` (+ `services/memory/tests/`); `@pytest.mark.asyncio`; assert structure not literal LLM text; fake async Motor collections per the existing pattern.

## File Map
- `services/ws_gateway/mongo_session_store.py` + `sessions.py` — add a monotonic per-session `seq` to each turn in `add_turn` (both stores).
- `services/memory/context_manager.py` — repoint reads to `chat_turns`; watermark-based non-destructive `full_compact`; retire `microcompact`/`clear_tool_results`.
- `services/orchestrator/main.py` — reconcile the auto-compact call sites (remove the `microcompact` branch; `build_context`/`full_compact` now read `chat_turns`).
- `services/orchestrator/coding_orchestrator.py` — inject `build_context` memory-delta into `_run_react_loop` messages; thread `session_id` + context_manager.
- Tests: `services/memory/tests/test_context_manager.py`, `tests/services/ws_gateway/test_mongo_session_store.py`, `tests/services/orchestrator/` (loop injection).

---

### Task 1: Monotonic `seq` on `chat_turns`
**Files:** `services/ws_gateway/mongo_session_store.py`, `services/ws_gateway/sessions.py`; test `tests/services/ws_gateway/test_mongo_session_store.py`.
The memory engine orders + watermarks by a stable per-session `seq` (createdAt is second-precision → ties). `add_turn` assigns `seq` = the count of existing turns for that session (0-based, monotonic) on the inserted turn doc.
- [ ] Test: two `add_turn`s → turns carry `seq` 0 then 1; `turns()` returns them seq-ordered.
- [ ] Impl: in `MongoSessionStore.add_turn`, compute `seq` from the current turn count (before insert) and set it on the (copied) turn doc; same in `InMemorySessionStore.add_turn`. Keep `turnCount`/`updatedAt` behavior.
- [ ] Run `pytest tests/services/ws_gateway/ -q` → green. Commit.

### Task 2: `context_manager` reads `chat_turns` (non-destructive reads)
**Files:** `services/memory/context_manager.py`; test `services/memory/tests/test_context_manager.py`.
Repoint the READ methods from `db["messages"]`/`db.messages` to `db["chat_turns"]` with camelCase fields, mapping `text`→`content`, filtering `sessionId`, ordering by `seq`.
- `_recent_turns(session_id, budget)`: read `chat_turns` where `sessionId==session_id` AND `seq > watermark` (watermark from `redis.get(f"summarized_through:{session_id}")`, default -1), sorted `seq` asc, newest-retained trim to budget; format `f"{role.upper()}: {text}"`.
- `last_activity_seconds`: read newest `chat_turns.createdAt` for the session.
- Retire `microcompact` and `clear_tool_results` → make them no-ops returning 0 (they targeted `role:'tool'` + destructive `db.messages` strips; `chat_turns` has neither). Leave the methods present (callers exist) but no-op with a docstring noting retirement.
- [ ] Test: seed a fake `chat_turns` collection; `_recent_turns` returns only turns after the watermark, seq-ordered, mapping `text`→content; `microcompact`/`clear_tool_results` return 0 and mutate nothing.
- [ ] Impl + run `pytest services/memory/tests/ -q` → green. Commit.

### Task 3: `full_compact` — watermark-based, non-destructive
**Files:** `services/memory/context_manager.py`; test `services/memory/tests/test_context_manager.py`.
Replace the delete-based compaction with a watermark advance over `chat_turns`.
- Read `chat_turns` for the session sorted `seq` asc. If `count <= _KEEP_RECENT`, return zero (nothing to do).
- `to_compact` = turns with `seq > watermark` AND `seq <= (max_seq - _KEEP_RECENT)` (i.e. everything older than the recent tail that isn't already summarized).
- Summarize `to_compact` via `_parallel_summarize` (anchor-preserved). **Merge** into the existing `summary:{session_id}` (use the existing `_MERGE_PROMPT` when a prior summary exists, else store as anchor+summary) — iterative, not replace-from-scratch.
- Advance watermark: `redis.set(f"summarized_through:{session_id}", <max seq in to_compact>)`. **Do NOT delete `chat_turns`.**
- Keep the reflections extraction + `compact.quality` emit (report `turns_compacted=len(to_compact)`). On failure, roll back the summary + watermark (mirror the current rollback).
- [ ] Test: seed 40 fake `chat_turns`; `full_compact` (fake `llm_fn`) writes a merged summary, advances `summarized_through`, deletes NOTHING; a second `full_compact` only summarizes newly-eligible turns (watermark respected) and merges.
- [ ] Impl + run `pytest services/memory/tests/ -q` → green. Commit.

### Task 4: Reconcile `main.py` auto-compact call sites
**Files:** `services/orchestrator/main.py`; extend the existing orchestrator tests.
The build-context fill probe (~627) + `full_compact` (~634) now operate on `chat_turns` (no code change needed beyond confirming behavior). Remove the `microcompact` branch (~650-654) since microcompact is retired (or leave it calling the no-op — but prefer removing the dead branch). Keep the `FULL_THRESH` trigger.
- [ ] Confirm/adjust: the probe still gates `full_compact` on `total_tokens >= FULL_THRESH`; drop the `MICRO_THRESH`/`microcompact` branch. Run the orchestrator suite → green. Commit.

### Task 5: Inject continuity into the ReAct loop (the payoff)
**Files:** `services/orchestrator/coding_orchestrator.py`; test `tests/services/orchestrator/`.
In `_run_react_loop` (messages seeded at ~530), after `assembler.system_message()` and BEFORE the `{"role":"user","content": goal}`, insert a memory message when continuity exists.
- Thread `session_id` + the context_manager (via `self`/storage) into `_run_react_loop` (it currently takes `goal, max_steps`). Use the orchestrator's `storage.context_manager`.
- Call `build_context(session_id, current_task=goal, system_prompt="", agent_instructions="")`; build the injected block from `ctx.summary_buffer` + `ctx.anchor_buffer` + `ctx.recent_turns` (NOT system_prompt — already in the prefix). If all empty, inject nothing (behavior-identical to today).
- Inject as a single message: `{"role":"user","content": f"CONVERSATION SO FAR (context — the user's new message is below):\n{block}"}` placed at index 1 (after the frozen system dict, before the goal). Prefix stays byte-stable.
- Best-effort: wrap in try/except → on any failure, proceed with no injection (log stderr).
- [ ] Test: with a fake context_manager returning a summary + recent, `_run_react_loop`'s assembled messages contain the memory block between the system message and the goal, and the system message (index 0) is unchanged; with empty context, messages are exactly `[system, goal]` (no injection).
- [ ] Impl + run `pytest tests/services/orchestrator/ -q` → green. Commit.

## Live verification (host, after all tasks)
Ask "what is the traveling salesman problem?" then "is it NP or NP-complete?" in the same chat → the second answer references TSP (no "Which problem?"). A long chat (>15 turns) still recalls an early fact (summary kicks in at `FULL_THRESH`); `chat_turns` count only grows (never deleted); reopening a chat still replays full history (UI unaffected).

## Self-review (coverage)
Feed gap → Tasks 1-3 (chat_turns seq + non-destructive reads + watermark summary). Consume gap → Task 5 (inject). Call-site reconcile → Task 4. RAG/embeddings explicitly OUT of scope (see [[memory-subsystem-dormant]]).
