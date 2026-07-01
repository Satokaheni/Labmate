# Frontend Session Persistence & Chat-UX Fixes — Implementation Plan

> **For agentic workers:** This repo uses the CLAUDE.md **Implementation Workflow** (Haiku implements a task from its spec, writes code + tests + commits → Opus judges the commit → fix until pass → next task). Steps use checkbox (`- [ ]`) syntax for tracking. Each task below is a self-contained, independently reviewable deliverable with TDD tests.

**Goal:** Make chat sessions durable and fully manageable (reopen with complete history, rename, delete, most-recent-first ordering) and fix four chat-UX defects (boot animation reset-loop, auto-scroll gap, no interrupt, no manual compact).

**Architecture:** Two groups on one branch (`fix/frontend-session-bugs`). **Group A** replaces the volatile `InMemorySessionStore` with a durable Motor-backed `MongoSessionStore` and captures the *complete* assistant turn in the ws_gateway relay, so `session.open` replays full conversations across restarts; rename/delete/ordering ride the same store + sidebar. **Group B** is four independent frontend fixes, three of which just wire the renderer to already-complete backend plumbing (`cancel`, `compact`) or fix a local React effect (animation, scroll).

**Tech Stack:** Python 3 + Motor (async MongoDB) + FastAPI/WebSocket (ws_gateway); React + TypeScript + Vite + Electron (frontend); pytest + pytest-asyncio (backend tests); vitest (frontend tests).

## Global Constraints

- MongoDB access is **Motor async** (`motor.motor_asyncio.AsyncIOMotorClient`), mirroring `services/ws_gateway/user_store.py::MongoUserStore` (lazy import so tests don't need Motor). Connection string from `config.mongo_url` (env `MONGO_URI`, default `mongodb://localhost:27017/labmate`).
- **Dedicated collections `chat_sessions` + `chat_turns`** — NOT the bare `sessions` name (the orchestrator's `WorkspaceManager` already owns a `sessions` collection with a different shape keyed on `session_id`).
- **No transactional outbox** for these writes (session/turn data is not projected to Chroma/Redis) — plain Motor `insert_one`/`update_one`/`find`. (CLAUDE.md rule #7 applies only to Chroma/Redis-projected writes.)
- **Persistence is best-effort**: a Mongo error must NEVER break the live WebSocket stream or the ws loop — wrap writes in try/except → `logging`/stderr, never `print`/stdout.
- **InMemory fallback**: if `MONGO_URI` is unset or the Motor client can't be constructed at init, fall back to `InMemorySessionStore` with a single stderr warning, so tests and no-Mongo dev runs still work. The seam is `services/ws_gateway/server.py` `store = session_store or <default>`.
- Backend tests: `@pytest.mark.asyncio`, pytest + pytest-asyncio only; assert structure, not literal LLM text; Motor async cursor mocks must support `.find().sort()` chaining (return self). Tests live in `tests/` mirroring `services/`.
- Frontend tests: vitest, colocated `*.test.ts(x)`.
- Do NOT modify `core/`, `tools/`, or legacy `main.py`. Do NOT add `console.log`/`print` to stdout in any relay/MCP path.
- Keep frontend text sizes small (existing look); don't restyle unrelated UI.
- Per-task: Haiku implements + commits; Opus judges; if React touched, run `react-doctor` before the judge.

---

## File Structure

**Group A (session persistence & management):**
- Create `services/ws_gateway/mongo_session_store.py` — `MongoSessionStore` (async, Motor). One responsibility: durable session + turn CRUD.
- Modify `services/ws_gateway/sessions.py` — make `InMemorySessionStore` methods **async** (matching the new interface) + add `async delete()`; the sync HTTP router becomes `async def` with `await`.
- Modify `services/ws_gateway/server.py` — swap the store seam + `await` all store calls; assemble & persist the assistant turn in `_relay_task`; add `session.delete` handler.
- Modify `services/ws_gateway/boot.py` — `await session_store.list()` for the bootstrap.
- Modify `services/frontend/src/hooks/useLabmateWS.ts` — `session.deleted` frame + reducer; re-sort on `session.updated`; clean `session.history` set; `deleteSession` export.
- Modify `services/frontend/src/components/chat/ChatScreen.tsx` — sidebar rename/delete affordance.
- Tests: `tests/services/ws_gateway/test_mongo_session_store.py`, extend `test_server*.py`; frontend `useLabmateWS.test.ts`.

**Group B (chat UX — independent):**
- Modify `services/frontend/src/screens/RegressionPlot.tsx` — progress via ref, mount-once effect.
- Modify `services/frontend/src/components/chat/ChatScreen.tsx` — `scrollSignal` + artifacts; cancel button; `/compact` parse in Composer `submit()`.
- Modify `services/frontend/src/hooks/useLabmateWS.ts` — `cancel(turnId)` + `compact(sessionId)` exports.
- Tests: `RegressionPlot.test.tsx`, `useLabmateWS.test.ts`, `ChatScreen.test.tsx`.

---

# GROUP A — Durable session persistence & management

### Task A1: `MongoSessionStore` + async store interface + seam swap + fallback

**Files:**
- Create: `services/ws_gateway/mongo_session_store.py`
- Modify: `services/ws_gateway/sessions.py` (InMemorySessionStore → async methods + `delete`; router → async/await)
- Modify: `services/ws_gateway/server.py` (store seam ~line 374; `await` every store call at ~140,157,158,287–293,297,305; boot pass-through)
- Modify: `services/ws_gateway/boot.py` (line ~106 `await session_store.list()`)
- Test: `tests/services/ws_gateway/test_mongo_session_store.py`; update `tests/services/ws_gateway/test_sessions*.py` for async

**Interfaces (Produces — the async store contract both stores implement):**
```python
async def create(self, *, title: str, mode: str, session_id: str | None = None, updated_at: str | None = None) -> dict
async def list(self) -> list[dict]                 # sorted updatedAt desc
async def get(self, sid: str) -> dict | None
async def rename(self, sid: str, title: str) -> dict | None
async def delete(self, sid: str) -> bool           # NEW — removes session + its turns
async def turns(self, sid: str) -> list[dict]      # ordered createdAt asc
async def add_turn(self, sid: str, turn: dict) -> None
async def set_debug(self, sid: str, enabled: bool) -> None
async def get_debug(self, sid: str) -> bool
```
Session doc shape (unchanged fields + `debug`): `{id, title, mode, turnCount, contextTokens, createdAt, updatedAt, debug}` in `chat_sessions`. Turn doc: `{sessionId, id, role, text, reasoning, toolCalls, createdAt, status}` in `chat_turns`.

- [ ] **Step 1 (test):** `test_mongo_session_store.py` — construct `MongoSessionStore` with a **fake async Motor collection** (an object whose `insert_one`/`update_one`/`find_one`/`delete_many` are `AsyncMock`, and `find()` returns a cursor whose `.sort()` returns self and which is async-iterable). Assert: `create` writes a `chat_sessions` doc + returns it; `add_turn` appends to `chat_turns` and bumps `turnCount`/`updatedAt`; `turns` returns them createdAt-asc; `list` sorts updatedAt desc; `rename` updates title; `delete` removes the session AND its turns and returns True (False for missing); `get`/`get_debug` round-trip. Include a `store_factory` returning `(store, fake_db)` fixture.
- [ ] **Step 2:** Run → FAIL (module missing).
- [ ] **Step 3 (impl):** Implement `MongoSessionStore(mongo_url, db_name='labmate')` with a lazy `import motor.motor_asyncio` (mirror `MongoUserStore`), `self._sessions = client[db_name]['chat_sessions']`, `self._turns = client[db_name]['chat_turns']`. Implement the 9 async methods with plain Motor calls; `_now_iso()` copied from `sessions.py`. Ensure indexes are created lazily/best-effort (`id` unique on chat_sessions; `(sessionId, createdAt)` on chat_turns) guarded by try/except.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5 (async interface):** Convert `InMemorySessionStore` methods to `async def` (bodies unchanged, just awaitable), add `async delete(sid)` (pop from `_sessions`, `_turns`, `_debug`; return bool). Update `build_sessions_router` endpoints to `async def` + `await store.…`.
- [ ] **Step 6 (seam + call sites):** In `server.py`, add a `_default_session_store(config)` helper: try `MongoSessionStore(config.mongo_url)`; on failure or missing url, log a stderr warning and return `InMemorySessionStore()`. Set `store = session_store or _default_session_store(config)`. Add `await` to every store call (create/get/add_turn/list/rename/turns/set_debug) in `server.py` + `boot.py`. Update affected `test_server*.py` mocks to `AsyncMock`.
- [ ] **Step 7:** Run `pytest tests/services/ws_gateway/ -q` → all PASS.
- [ ] **Step 8:** Commit `feat(ws_gateway): durable MongoSessionStore + async store interface with InMemory fallback`.

**Acceptance:** All ws_gateway tests green; store interface is uniformly async; no-Mongo path falls back to InMemory with a warning (not a crash).

---

### Task A2: Assemble & persist the complete assistant turn in `_relay_task`

**Files:**
- Modify: `services/ws_gateway/server.py` (`_relay_task`, lines ~35–110; it needs the `store` + `session_id` + `assistant_turn_id`)
- Test: `tests/services/ws_gateway/test_relay_persist.py`

**Interfaces (Consumes):** the async `store.add_turn(sid, turn)` from A1. `_relay_task` must receive `store` and `session_id` (thread them from the `session.new`/send path that already knows both).

- [ ] **Step 1 (test):** Drive `_relay_task` with a fake event source yielding `answer.delta`(x2), `reasoning.*`, `tool.start`+`tool.done`, then `turn.done` (status=complete). Use an `AsyncMock` store. Assert: on `turn.done`, `store.add_turn` is called once with a turn `{id: assistant_turn_id, role:'assistant', text:<concatenated deltas>, reasoning:<assembled>, toolCalls:[{id,name,args,result,status}], status:'complete'}`; the relay STILL forwards every frame to the client (client streaming unaffected); a raised store error does NOT propagate (best-effort).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3 (impl):** In `_relay_task`, accumulate `answer_chunks`, reuse the existing reasoning accumulation, and collect `tool_calls` from `tool.start`/`tool.done`. On the `turn.done` branch, build the assembled assistant turn and `await store.add_turn(session_id, turn)` inside try/except (log on failure). Persist only when `session_id` is truthy. Do not persist on cancel/error status other than recording status.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(ws_gateway): persist the assembled assistant turn on turn.done`.

**Acceptance:** After a completed turn, `store.turns(sid)` contains the full user + assistant exchange; reopening replays complete conversations. Live streaming byte-path unchanged.

---

### Task A3: Blank-on-switch fix — `session.history` sets that session's turns cleanly

**Files:**
- Modify: `services/frontend/src/hooks/useLabmateWS.ts` (`session.history` reducer, lines ~248–258)
- Test: `services/frontend/src/hooks/useLabmateWS.test.ts`

**Interfaces (Consumes):** `session.history` frame `{sessionId, turns}` (unchanged).

- [ ] **Step 1 (test):** Reducer test: state has session A's turns; dispatch `session.history` for B with B's turns. Assert result `turns` contains B's turns (deduped by id) and NO A turns leak into B's view; dispatching the same history twice is idempotent (no dupes).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3 (impl):** On `session.history`, keep only turns whose `sessionId` is absent or equals `frame.sessionId`, then merge `frame.turns` deduped by id (per the [session.history] block). This removes the stale-turn flash when switching.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `fix(frontend): clear stale turns when switching sessions`.

**Acceptance:** Clicking an old chat shows exactly that chat's turns; combined with A2, the full conversation (with assistant replies) renders.

---

### Task A4: Rename + delete a chat

**Files:**
- Modify: `services/ws_gateway/server.py` (add `elif mtype == 'session.delete'` after the `session.rename` handler ~line 299)
- Modify: `services/frontend/src/hooks/useLabmateWS.ts` (`session.deleted` frame + reducer; `deleteSession` export; keep existing `session.rename` send)
- Modify: `services/frontend/src/components/chat/ChatScreen.tsx` (sidebar per-item rename [inline] + delete [confirm] affordance near ~line 963)
- Test: `tests/services/ws_gateway/test_server_session.py` (delete handler); `useLabmateWS.test.ts` (reducer + deleteSession)

**Interfaces (Produces):** WS in `{type:'session.delete', sessionId}`; WS out `{type:'session.deleted', sessionId}`. Hook export `deleteSession(sessionId)`.

- [ ] **Step 1 (test, backend):** `session.delete` for an existing sid → `store.delete` called, `session.deleted` frame sent; if it was active, `active_session_id` cleared. Missing sid → no crash, no frame.
- [ ] **Step 2 (test, frontend):** reducer `session.deleted` removes the session from `sessions`; if it was active, `activeSessionId` becomes `sessions[0]?.id ?? null`. `deleteSession` sends the frame when socket open.
- [ ] **Step 3:** Run → FAIL.
- [ ] **Step 4 (impl):** Backend handler (mirror `session.rename`) → `await store.delete(sid)` + send `session.deleted`. Frontend: add frame type, reducer branch, `deleteSession` export. Sidebar: a small hover affordance per session — rename (reuses existing `session.rename` send) and delete (with a confirm) — matching current sidebar styling/size.
- [ ] **Step 5:** Run both suites → PASS. Run `react-doctor` (ChatScreen touched).
- [ ] **Step 6:** Commit `feat(sessions): rename + delete chats (store.delete + session.delete frame + sidebar UI)`.

**Acceptance:** A chat can be renamed and deleted from the sidebar; deleting the active chat selects the next most-recent.

---

### Task A5: Most-recent-active ordering

**Files:**
- Modify: `services/frontend/src/hooks/useLabmateWS.ts` (`session.updated` reducer, lines ~232–246)
- Test: `useLabmateWS.test.ts`

- [ ] **Step 1 (test):** With sessions `[A,B,C]`, dispatch `session.updated` for C → result order is `[C,A,B]` (touched session moves to front; existing entry updated in place, not duplicated). A brand-new session id is inserted at front.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3 (impl):** On `session.updated`, remove any existing entry for that id and `unshift` the updated session (front = most recent). `store.list()` already returns updatedAt-desc for the boot list.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `fix(frontend): order chats most-recent-active first`.

**Acceptance:** Sending in / renaming a chat floats it to the top of the sidebar without a reload.

---

# GROUP B — Chat-UX fixes (independent; disjoint files where possible)

### Task B1: Boot animation no longer resets/refits on progress ticks

**Files:**
- Modify: `services/frontend/src/screens/RegressionPlot.tsx`
- Test: `services/frontend/src/screens/RegressionPlot.test.tsx`

**Root cause:** the RAF effect closes over `progress` with `[progress]` deps ([RegressionPlot.tsx:59](services/frontend/src/screens/RegressionPlot.tsx:59)); every `boot.update` re-runs it → `apply(0)` → the fit restarts.

- [ ] **Step 1 (test):** Render with `progress=0`; rerender with `progress=0.5`. Assert the effect's RAF loop is set up **once** (spy `requestAnimationFrame`/`cancelAnimationFrame`: no cancel+restart on the progress change) and the initial `apply(0)` fires once, not per rerender. (Assert structurally, e.g. cancelAnimationFrame not called on rerender.)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3 (impl):** Add `const progressRef = useRef(progress); progressRef.current = progress;` (updated every render, no effect dep). In `apply`, read `progressRef.current` instead of the closed-over `progress`. Change the effect deps to `[]` (mount once). The fit animates once; live progress still feeds `curvePath` via the ref.
- [ ] **Step 4:** Run → PASS. Run `react-doctor`.
- [ ] **Step 5:** Commit `fix(frontend): boot regression animation fits once instead of resetting on each progress tick`.

**Acceptance:** On the loading screen the curve fits once and settles; boot-status ticks no longer reset it.

---

### Task B2: Auto-scroll also follows streamed artifacts

**Files:**
- Modify: `services/frontend/src/components/chat/ChatScreen.tsx` (`scrollSignal`, lines ~1628–1630)
- Test: `services/frontend/src/components/chat/ChatScreen.test.tsx` (or a focused signal test)

- [ ] **Step 1 (test):** A test that the scroll effect fires when the last turn gains an artifact — assert `scrollSignal` changes when `lastTurn.artifacts.length` goes 0→1 (extract the signal into a tiny pure helper `scrollSignalFor(turns)` to make it unit-testable, or assert via a render that `scrollTop` is set).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3 (impl):** Add `${lastTurn?.artifacts?.length ?? 0}:` to the `scrollSignal` string so artifact frames trigger the near-bottom auto-scroll (keep the existing stick-to-bottom 80px heuristic).
- [ ] **Step 4:** Run → PASS. Run `react-doctor`.
- [ ] **Step 5:** Commit `fix(frontend): auto-scroll when the assistant streams an artifact`.

**Acceptance:** While near the bottom, the view follows new text, tool calls, AND artifacts; scrolling up still detaches.

---

### Task B3: Interrupt / stop a running task

**Files:**
- Modify: `services/frontend/src/hooks/useLabmateWS.ts` (`cancel(turnId)` export)
- Modify: `services/frontend/src/components/chat/ChatScreen.tsx` (stop button while a turn is streaming)
- Test: `useLabmateWS.test.ts` (cancel send); `ChatScreen.test.tsx` (button visible while streaming, calls cancel)

**Backend already complete:** `cancel` mtype → `write_cancel(labmate:cancel:{task_id})` → orchestrator `is_cancelled()` at the ReAct loop top. Frontend only.

- [ ] **Step 1 (test):** `cancel(turnId)` sends `{type:'cancel', turnId}` when the socket is open. ChatScreen shows a Stop control while the latest assistant turn `status === 'streaming'` (or agentStatus busy) and hides it otherwise; clicking calls `cancel` with that turn id.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3 (impl):** Add `cancel` to the hook + return object (mirror `setDebug`). Thread it to ChatScreen; render a compact Stop button in the thinking/streaming indicator that calls `cancel(streamingTurn.id)`.
- [ ] **Step 4:** Run → PASS. Run `react-doctor`.
- [ ] **Step 5:** Commit `feat(frontend): stop button to interrupt a running task`.

**Acceptance:** Sending a task shows a Stop control; clicking it cancels the run (orchestrator halts at the next loop check, `turn.done` status error).

---

### Task B4: Manual compact via `/compact`

**Files:**
- Modify: `services/frontend/src/hooks/useLabmateWS.ts` (`compact(sessionId)` export)
- Modify: `services/frontend/src/components/chat/ChatScreen.tsx` (Composer `submit()` slash parse, line ~1287; surface `compact.done` result briefly)
- Test: `useLabmateWS.test.ts` (compact send); `ChatScreen.test.tsx` (`/compact` routes to compact, not onSend)

**Backend already complete:** `compact` mtype → goals queue → `compact.done` frame. Frontend only.

- [ ] **Step 1 (test):** `compact(sessionId)` sends `{type:'compact', sessionId}` when open. In the Composer, `submit()` with text `"/compact"` calls the compact path (not `onSend`) and clears the input; any other text still calls `onSend`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3 (impl):** Add `compact` to the hook + return object. Thread an `onCompact` prop to the Composer; in `submit()`, if `t === '/compact'` call `onCompact()` (else `onSend(t)`). Optionally surface the `compact.done` result as a transient system line (reuse existing status/toast styling; no new heavy UI).
- [ ] **Step 4:** Run → PASS. Run `react-doctor`.
- [ ] **Step 5:** Commit `feat(frontend): /compact command triggers manual context compaction`.

**Acceptance:** Typing `/compact` triggers backend compaction and reports done; normal messages are unaffected.

---

## Sequencing & parallelism

- **Group A is sequential:** A1 (foundation) → A2 → A3 → A4 → A5. A1 must land first (everything awaits the async store).
- **Group B is independent** of A and mostly of each other. B1 (RegressionPlot) and B3/B4/B2 (ChatScreen + hook) can run after A-frontend tasks to avoid `ChatScreen.tsx`/`useLabmateWS.ts` merge churn. **Serialize committers that touch the same file** (ChatScreen.tsx is touched by A3/A4, B2, B3, B4; useLabmateWS.ts by A3/A4/A5, B3, B4) — one committing agent at a time per shared file (lesson: parallel committers on a shared file collide).
- Suggested order: A1 → A2 → A3 → A4 → A5 → B1 → B2 → B3 → B4.

## Live verification (after all tasks, on the host)

1. Start stack (`serve-model.sh`, `start.sh`, `status.sh` green); ensure MongoDB up.
2. New chat, send a task, watch it stream; **click Stop** mid-run → run halts.
3. Send another; when done, **start a second chat**, then **click back** → full conversation (user + assistant) renders.
4. **Restart ws_gateway**, reconnect → old chats still listed and reopen with history (durability).
5. **Rename** and **delete** a chat from the sidebar; sending floats a chat to the top.
6. Loading screen on next boot: curve **fits once** (no reset loop).
7. Type **`/compact`** → compaction runs, reports done.

## Self-review notes (coverage)

- #1 reopen-loses-replies → A1+A2+A3. #2 context-strip → symptom of #1 (no separate task). #4 rename/delete → A4. #5 ordering → A5. Boot animation → B1. Auto-scroll → B2. Interrupt → B3. Compact → B4. All backlog items mapped to a task.
