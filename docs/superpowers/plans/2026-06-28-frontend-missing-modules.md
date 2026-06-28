# Frontend Missing Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the four unbuilt frontend modules — `useLabmateWS`, `AssistantTurn`, `Turn`, `Root` — so the frontend fully type-checks (`tsc` clean) and their existing vitest tests pass.

**Architecture:** TDD against existing tests. Three modules (`useLabmateWS`, `Turn`, `AssistantTurn`) have committed vitest tests that are the authoritative spec — implement until each test passes. `Root` is the app shell (no test; wired by `main.tsx`) — conditional-render by the hook's `phase`, composing the existing screens (`BootScreen`, `LoginScreen`) + layout (`ChatLayout`) + components (`Turn`, `Composer`, `ThinkingIndicator`, `ContextBar`, `SystemFooter`). Shared types live in the already-created `src/types/events.ts`.

**Tech Stack:** React 18 + TypeScript (strict), Vite, Vitest + @testing-library/react, an OpenAI-compatible ws_gateway WebSocket protocol, optional Electron `window.electronAPI`.

## Global Constraints

- All four live under `services/frontend/src/`. Run from `services/frontend`.
- Verify each module two ways: `npx vitest run <test>` (where a test exists) AND `npx tsc --noEmit -p tsconfig.json` (whole-frontend type-check).
- The **existing test files are the spec** — do NOT modify them to make code pass; implement to satisfy them. (Adding missing *types* to `src/types/events.ts` is allowed when a test requires a field/type.)
- Reuse existing components/libs — `LabmateMark`, `ReasoningBlock`, `ToolCallRow`, `ToolCallPanel`, `ArtifactCard`, `ChatLayout`, `Composer`, `ThinkingIndicator`, `ContextBar`, `SystemFooter`, `BootScreen`, `LoginScreen`, `lib/markdown` (`Markdown`). Do NOT rebuild them.
- stdout-sacred etc. are backend rules; N/A here. Keep imports type-only where possible (`import type`).
- The text harness / backend is untouched — only `services/frontend/**`.
- The CI `frontend-typecheck` job stays `continue-on-error` for now; flipping it to blocking + re-adding the pre-commit `tsc` hook is a post-merge follow-up (noted at the end), because those configs live on the `chore/ci-and-precommit` branch.

## WS protocol (authoritative — implement the hook to this)

Client→server frames: `{type:'auth',token}`, `{type:'send',sessionId,mode,text}`, `{type:'tool.result',toolRequestId,result,error?}`, `{type:'debug.set',sessionId,enabled}`, `{type:'session.new',mode}`, `{type:'session.open',sessionId}`.
Server→client frames: `auth.ok{user}`, `auth.error{reason}`, `boot.plan{subsystems}`, `boot.update{id,state,detail?,message?}`, `boot.ready{sessionBootstrap:{sessions,activeSessionId,agentStatus}}`, `turn.created{turn}`, `answer.delta{turnId,text}`, `reasoning.done{turnId,reasoning}`, `artifact.created{turnId,artifact}`, `context.update{window}`, `agent.status{status}`, `tool.request{turnId,toolRequestId,name,args}`, `turn.done{turnId,status}`, `session.updated{session}`.
> Confirm exact frame keys against `services/frontend/src/hooks/useLabmateWS.test.ts` (the test mocks these frames) and `services/cli/ws_client.py` / `services/ws_gateway/` before/while implementing.

---

### Task 1: `useLabmateWS` hook + any types its test needs

**Files:**
- Create: `services/frontend/src/hooks/useLabmateWS.ts`
- Modify (only if its test requires fields not present): `services/frontend/src/types/events.ts`
- Test (exists — the spec): `services/frontend/src/hooks/useLabmateWS.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export type LabmateWSState =
    | { phase: 'idle' }
    | { phase: 'connecting' }
    | { phase: 'authenticating' }
    | { phase: 'booting'; subsystems: Subsystem[] }
    | { phase: 'ready'; subsystems: Subsystem[]; agentStatus: AgentStatus; sessions: Session[]; turns: Turn[]; contextWindow?: ContextWindow; authError?: string }
    | { phase: 'error'; authError?: string };

  export function useLabmateWS(
    wsUrl: string, token: string | null, reconnectKey?: number
  ): {
    state: LabmateWSState;
    send: (text: string, sessionId: string) => void;
    newSession?: (mode: string) => void;
    openSession?: (sessionId: string) => void;
    setDebug: (sessionId: string, enabled: boolean) => void;
  };
  ```

- [ ] **Step 1: Read the test (the spec) and run it red**

Read `src/hooks/useLabmateWS.test.ts` fully — it mocks a WebSocket and asserts every behavior below. Run it to confirm red:
Run: `cd services/frontend && npx vitest run src/hooks/useLabmateWS.test.ts`
Expected: FAIL (module `./useLabmateWS` not found).

- [ ] **Step 2: Add any missing `events.ts` types the test references**

If the test imports/asserts types or fields not in `src/types/events.ts` (e.g. a `Mode = 'chat'|'code'|'paper'` union, `Session.updatedAt`), add them to `src/types/events.ts` minimally to match the test. (Most already exist from the prior commit.)

- [ ] **Step 3: Implement the hook**

Create `src/hooks/useLabmateWS.ts` satisfying the test. Required behaviors (each asserted by the test):
1. `token === null` → stay `{phase:'idle'}`, do NOT open a socket.
2. token provided → open `new WebSocket(wsUrl)`, phase `connecting` → on open send `{type:'auth',token}`, phase `authenticating`.
3. `auth.error` → `{phase:'error',authError:reason}`. `auth.ok` → proceed.
4. `boot.plan{subsystems}` → `{phase:'booting',subsystems}`. `boot.update{id,state,...}` → patch the matching subsystem in place. `boot.ready{sessionBootstrap}` → `{phase:'ready', sessions, agentStatus, subsystems, turns:[]}`.
5. `turn.created{turn}` → append to `turns`. `answer.delta{turnId,text}` → append `text` to the matching turn's text (chunk accumulation: "hel"+"lo"="hello").
6. `reasoning.done{turnId,reasoning}` → set `turn.reasoning`. `artifact.created{turnId,artifact}` → push to `turn.artifacts` (supports multiple).
7. `session.updated{session}` → upsert by id (no duplicates on re-emit). `context.update{window}` → set `contextWindow`. `agent.status{status}` → set `agentStatus`. `turn.done{turnId,status}` → mark the turn's status.
8. `tool.request{turnId,toolRequestId,name,args}` → call `window.electronAPI.executeTool(name,args)` (async); reply `{type:'tool.result',toolRequestId,result,error?}` (error string if `electronAPI` missing or the call throws).
9. `send(text,sessionId)` → `{type:'send',sessionId,mode:'chat',text}`. `setDebug(sessionId,enabled)` → `{type:'debug.set',sessionId,enabled}`. `newSession`/`openSession` → `{type:'session.new',mode}` / `{type:'session.open',sessionId}`.
10. Changing `reconnectKey` → close the old socket and open a new one (effect dep). Clean up the socket on unmount.

Implementation shape: a `useReducer` over the frames (a pure `reduce(state, frame)` is cleanest + matches the test's frame-by-frame assertions), a `useEffect` keyed on `[wsUrl, token, reconnectKey]` managing the socket lifecycle, and `useRef` for the live socket so `send`/`setDebug` can write to it.

- [ ] **Step 4: Run the test + tsc**

Run: `cd services/frontend && npx vitest run src/hooks/useLabmateWS.test.ts && npx tsc --noEmit -p tsconfig.json`
Expected: the hook test PASSES; tsc shows no NEW errors for `useLabmateWS` (other unbuilt modules may still error — that's later tasks).

- [ ] **Step 5: Commit**

```bash
git add services/frontend/src/hooks/useLabmateWS.ts services/frontend/src/types/events.ts
git commit -m "feat(frontend): useLabmateWS hook (ws_gateway connection/state)"
```

---

### Task 2: `AssistantTurn` component

**Files:**
- Create: `services/frontend/src/components/AssistantTurn.tsx`
- Test (exists — the spec): `services/frontend/src/components/AssistantTurn.test.tsx`

**Interfaces:**
- Consumes: `Turn`, `Artifact`, `ToolCall`, `Reasoning` (events.ts); `ToolCallRow`, `ArtifactCard`, `ReasoningBlock`, `LabmateMark`, `Markdown`.
- Produces:
  ```ts
  export interface AssistantTurnProps { turn: Turn; hideToolCalls?: boolean; onPreviewArtifact?: (a: Artifact) => void }
  export function AssistantTurn(props: AssistantTurnProps): JSX.Element
  ```

- [ ] **Step 1: Read the test + run red**

Read `src/components/AssistantTurn.test.tsx`. Run: `cd services/frontend && npx vitest run src/components/AssistantTurn.test.tsx` → FAIL (module not found).

- [ ] **Step 2: Implement to satisfy the test**

Behaviors asserted:
- `hideToolCalls` falsy + `turn.toolCalls?.length` → render a `ToolCallRow` per tool call inline; NO summary pill.
- `hideToolCalls` true + tool calls → render a summary pill with EXACT text `"{n} tool call{s} → panel"` (singular "1 tool call → panel", plural "2 tool calls → panel"); do NOT render `ToolCallRow`s.
- No tool calls → render nothing for the tool section (guard `turn.toolCalls?.length`).
- Render `LabmateMark` avatar; render `turn.text` via `Markdown` (bold `**x**` → `<strong>`).
- `turn.reasoning` present → render `ReasoningBlock`.
- `turn.artifacts` → an `ArtifactCard` per artifact, passing `onPreviewArtifact`.

- [ ] **Step 3: Run test + tsc**

Run: `cd services/frontend && npx vitest run src/components/AssistantTurn.test.tsx && npx tsc --noEmit -p tsconfig.json`
Expected: AssistantTurn test PASSES; no new tsc errors for AssistantTurn.

- [ ] **Step 4: Commit**

```bash
git add services/frontend/src/components/AssistantTurn.tsx
git commit -m "feat(frontend): AssistantTurn component (tool rows / summary pill / artifacts)"
```

---

### Task 3: `Turn` component

**Files:**
- Create: `services/frontend/src/components/Turn.tsx`
- Test (exists — the spec): `services/frontend/src/components/Turn.test.tsx`

**Interfaces:**
- Consumes: `Turn`, `Artifact` (events.ts); `AssistantTurn` (Task 2), `LabmateMark`, `Markdown`.
- Produces:
  ```ts
  export interface TurnProps { turn: Turn; onPreviewArtifact?: (a: Artifact) => void }
  export function Turn(props: TurnProps): JSX.Element
  ```

- [ ] **Step 1: Read the test + run red**

Read `src/components/Turn.test.tsx`. Run: `cd services/frontend && npx vitest run src/components/Turn.test.tsx` → FAIL.

- [ ] **Step 2: Implement to satisfy the test**

Behaviors asserted:
- `turn.role === 'user'` → render `turn.text` right-aligned (container has `justify-end`); NO `LabmateMark`.
- `turn.role === 'assistant'` (and others) → delegate to `<AssistantTurn turn={turn} onPreviewArtifact={...} />` (which renders the avatar, markdown, reasoning, tools, artifacts).
- The reasoning expand/collapse is provided by `ReasoningBlock` (via AssistantTurn) — Turn just needs to render the assistant path so the test's reasoning-expand assertion passes.

- [ ] **Step 3: Run test + tsc**

Run: `cd services/frontend && npx vitest run src/components/Turn.test.tsx && npx tsc --noEmit -p tsconfig.json`
Expected: Turn test PASSES; no new tsc errors for Turn.

- [ ] **Step 4: Commit**

```bash
git add services/frontend/src/components/Turn.tsx
git commit -m "feat(frontend): Turn component (user/assistant dispatch)"
```

---

### Task 4: `Root` app shell

**Files:**
- Create: `services/frontend/src/Root.tsx`
- Reference (do not modify unless needed): `src/main.tsx`, `src/layouts/ChatLayout.tsx`, `src/screens/{BootScreen,LoginScreen}.tsx`, `src/components/{Composer,ThinkingIndicator,ContextBar,SystemFooter}.tsx`

**Interfaces:**
- Consumes: `useLabmateWS` (Task 1) + the existing screens/layout/components + `Turn` (Task 3).
- Produces: `export function Root(): JSX.Element` (default-importable if `main.tsx` uses a default import — match `main.tsx`'s import style exactly).

- [ ] **Step 1: Confirm main.tsx's import contract + run tsc red**

Read `src/main.tsx` — match Root's export (named vs default) and any props it passes. Run: `cd services/frontend && npx tsc --noEmit -p tsconfig.json` → still errors on `./Root` (and any other not-yet-done module).

- [ ] **Step 2: Implement the shell**

`Root` resolves the token (from `window.electronAPI` config/token if present, else null), calls `useLabmateWS(wsUrl, token, reconnectKey)`, and conditional-renders by `state.phase`:
- `idle` / no token → `LoginScreen` (on submit, set the token → triggers connect).
- `connecting` | `authenticating` | `booting` → `BootScreen` (pass `state.subsystems`).
- `error` → an error view (reuse `BootScreen`/`LoginScreen` with `state.authError`, or a minimal inline error).
- `ready` → `ChatLayout` composing: session list (from `state.sessions`), the turn list rendering `<Turn>` per `state.turns`, `Composer` (calls `send`), `ThinkingIndicator` (from `state.turns`/events), `ContextBar` (`state.contextWindow`), `SystemFooter` (`state.agentStatus`).
- Wire `onPreviewArtifact` to a right-panel preview (use `FilePreview` if present) or a no-op stub if out of scope for first pass.

Keep it a thin orchestration layer — no business logic beyond wiring. Reuse existing components' real prop types (read each before wiring; fix prop mismatches by passing what they require).

- [ ] **Step 3: Type-check (no Root test exists; tsc + a render smoke is the gate)**

Run: `cd services/frontend && npx tsc --noEmit -p tsconfig.json`
Expected: **0 errors** across the whole frontend (all four modules now exist). If a smoke test is cheap, add `src/Root.test.tsx` that renders `<Root/>` with a mocked WebSocket + asserts it shows the login/boot screen without throwing.

- [ ] **Step 4: Commit**

```bash
git add services/frontend/src/Root.tsx services/frontend/src/Root.test.tsx 2>/dev/null
git commit -m "feat(frontend): Root app shell (phase routing + chat layout wiring)"
```

---

### Task 5: Whole-frontend green gate

- [ ] **Step 1:** `cd services/frontend && npx tsc --noEmit -p tsconfig.json` — Expected: **0 errors**.
- [ ] **Step 2:** `cd services/frontend && npx vitest run` — Expected: the previously-missing-module tests (useLabmateWS, Turn, AssistantTurn) PASS; no regressions in the other component tests.
- [ ] **Step 3:** Confirm scope: `git diff --stat <BASE>..HEAD -- services/frontend` shows only the 4 new modules (+ optional Root.test + events.ts type additions). No backend files touched.

---

## Post-merge follow-up (NOT part of this plan's tasks)

Once this branch **and** `chore/ci-and-precommit` (PR #15) are both in `main`, flip the frontend type-check to a real gate:
- `.github/workflows/ci.yml`: remove `continue-on-error: true` from the `frontend-typecheck` job.
- `.pre-commit-config.yaml`: re-add the `frontend-typecheck` local hook (`cd services/frontend && npx --no-install tsc --noEmit -p tsconfig.json`).

## Self-Review

- **Spec coverage:** hook (T1), AssistantTurn (T2), Turn (T3), Root (T4), green gate (T5). The three tested modules are TDD'd against their committed tests; Root is type+smoke gated. ✓
- **Order:** useLabmateWS → AssistantTurn → Turn → Root (each depends on the prior). ✓
- **Tests are the spec:** tasks forbid editing tests to pass; only events.ts type additions are allowed. The Explore-mapped behaviors are restated per task so an implementer reading one task has the full contract. ✓
- **No backend impact:** all changes under services/frontend/. ✓
- **Honesty:** Root has no unit test, so its gate is tsc-0-errors + an optional render smoke — stated explicitly, not hidden.
