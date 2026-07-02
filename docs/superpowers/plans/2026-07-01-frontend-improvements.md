# Frontend UX Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three frontend UX issues (chat-list ordering on select, right-column default visibility, a hover reflow glitch) and build the new-session welcome view from the `~/Downloads/new_session/` design, fitted to live code.

**Architecture:** All changes are client-only, in `services/frontend/src`. Item #1 changes the `useLabmateWS` reducer to order sessions by the authoritative `updatedAt` field instead of frame-arrival. Item #2 mirrors the existing `lm.sidebarCollapsed` localStorage pattern for the right column. Item #3 removes a conditional-mount reflow in `SessionItem`. Item #4 renders a centered `NewSessionWelcome` (reusing `LabmateMark` + the live `Composer`, driven by pure copy helpers) as the `turns.length === 0` state of the center column, with an additive `seed` prop on `Composer` for starter-chip prefill.

**Tech Stack:** React + TypeScript, Vitest + React Testing Library (`vitest run`), Vite. No new dependencies.

## Global Constraints

- Test runner: `vitest run` from `services/frontend/` (script: `npm test`). Run a single file with `npx vitest run src/path/to/file.test.tsx`.
- TypeScript file naming: `camelCase.ts(x)`; React components PascalCase; follow existing patterns in `src/components/chat/`.
- All `localStorage` access MUST be wrapped in try/catch (jsdom + privacy-mode safe), matching `ChatScreen.tsx:1813-1829`. Reuse the `lm.*` key namespace.
- Assert structure/behavior, not pixel geometry (jsdom has no layout engine — never assert computed heights).
- **Testing reality (verified):** `ChatScreen.test.tsx` (148 lines) tests only *exported pure helpers* (`scrollSignalFor`, `findStreamingTurn`, `isCompactCommand`). Nothing renders `<ChatScreen>` — a full-component harness would need to mock `window.electronAPI` (used in `useEffect`s) and the `useWorkspace` hook, which the codebase deliberately avoids. So the house pattern is: **export the unit under test and test it in isolation** (a pure helper, or a small presentational component with simple props). This plan follows that pattern — it does NOT build a `<ChatScreen>` render harness. `useLabmateWS.test.ts` is the exception: it already has a `renderHook` + WS-mock harness (Task 3 reuses it).
- `Turn.status` values in fixtures are `'complete'` / `'streaming'` (per existing tests + `@/types/events`), never `'done'`.
- Ship the four items as separate commits, in the order below (ascending risk). Each task is independently reviewable. **Task 4 depends on Task 2** (it extends the `showRight` gate Task 2 wires).
- **Item #4 = the `~/Downloads/new_session/` design (centered welcome: logo → greeting → subtext → composer → starter chips).** Live code is the source of truth — where the prototype conflicts with live code, the design yields. The reconciliations are baked into Task 4's steps; do not re-derive them from the prototype.
- There must NEVER be two `Composer` instances mounted at once. Task 4 builds ONE `Composer` element and reuses it in the mutually-exclusive welcome/thread branches.
- Reuse live components, do not re-inline: the logo badge is `LabmateMark variant="tile"` (`components/LabmateMark.tsx`); the composer is the ChatScreen-local `Composer` (`ChatScreen.tsx:1358`).

---

### Task 1: Eliminate the SessionItem hover size glitch (#3)

**Files:**
- Modify: `services/frontend/src/components/chat/ChatScreen.tsx` (export the `SessionItem` component ~line 934; fix its hover rendering ~lines 996-1057)
- Create: `services/frontend/src/components/chat/SessionItem.test.tsx` (new isolated test file — `SessionItem` has simple props and needs no `ChatScreen`/electron harness)

**Interfaces:**
- Consumes: existing `SessionItem` props `{ session, active, onOpen, onRename, onDelete }` — unchanged; `session` is a `Session` from `@/types/events`.
- Produces: `export function SessionItem(...)` (newly exported so it can be unit-tested). Behavioral contract: the meta line ("`<mode> · N turns`") is present in the DOM regardless of hover state; rename/delete actions become interactive on hover without altering layout.

**Root cause (verified):** `SessionItem` renders the meta line only when NOT hovering (`{!isHovering && meta && ...}`, `ChatScreen.tsx:1053`) and mounts the action buttons only when hovering (`{isHovering && ...}`, `:1012`). Removing the meta line on hover shrinks the row by one text line, then it grows back on mouse-leave — the size jump.

- [ ] **Step 1: Export SessionItem so it can be tested in isolation**

In `ChatScreen.tsx`, change `function SessionItem(props: {` (line 934) to `export function SessionItem(props: {`. No other change in this step.

- [ ] **Step 2: Write the failing test**

Create `services/frontend/src/components/chat/SessionItem.test.tsx`:

```tsx
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Session } from '@/types/events';
import { SessionItem } from './ChatScreen';

const s: Session = {
  id: 's-1', title: 'My chat', mode: 'chat', turnCount: 3, contextTokens: 0,
  createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z',
};

describe('SessionItem', () => {
  it('keeps its meta line present on hover (no reflow)', () => {
    render(<SessionItem session={s} active={false} onOpen={() => {}} onRename={() => {}} onDelete={() => {}} />);

    // meta line present before hover
    expect(screen.getByText(/3 turns/)).toBeInTheDocument();

    const row = screen.getByRole('button', { name: /My chat/ });
    fireEvent.mouseEnter(row);

    // actions reachable on hover...
    expect(screen.getByTitle('Rename')).toBeInTheDocument();
    expect(screen.getByTitle('Delete')).toBeInTheDocument();
    // ...and the meta line is STILL present (this is what prevents the height change)
    expect(screen.getByText(/3 turns/)).toBeInTheDocument();

    fireEvent.mouseLeave(row);
    expect(screen.getByText(/3 turns/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd services/frontend && npx vitest run src/components/chat/SessionItem.test.tsx`
Expected: FAIL — the `/3 turns/` assertion after `mouseEnter` fails because the meta line is currently removed on hover (the buttons ARE present, so those assertions pass; the meta assertion is the one that fails).

- [ ] **Step 4: Implement — always render the meta line; toggle action visibility, not presence**

In `SessionItem` (`ChatScreen.tsx`):

1. Change the actions from conditional mount to always-mounted with visibility toggled, so their footprint never changes layout. Replace the `{isHovering && (<span ...actions>)}` block with an always-rendered container:

```tsx
<span
  aria-hidden={!isHovering}
  style={{
    display: 'flex',
    gap: 4,
    marginLeft: 8,
    flexShrink: 0,
    visibility: isHovering ? 'visible' : 'hidden',
    pointerEvents: isHovering ? 'auto' : 'none',
  }}
>
  <button
    type="button"
    onClick={(e) => { e.stopPropagation(); setIsRenaming(true); }}
    style={{ background: 'none', border: 'none', color: '#6b727d', cursor: 'pointer', fontSize: 11, padding: 0 }}
    title="Rename"
    tabIndex={isHovering ? 0 : -1}
  >
    ✎
  </button>
  <button
    type="button"
    onClick={(e) => { e.stopPropagation(); handleDelete(); }}
    style={{ background: 'none', border: 'none', color: '#6b727d', cursor: 'pointer', fontSize: 11, padding: 0 }}
    title="Delete"
    tabIndex={isHovering ? 0 : -1}
  >
    ✕
  </button>
</span>
```

2. Always render the meta line — drop the `!isHovering` guard so height is constant:

```tsx
{meta && (
  <span style={{ display: 'block', fontSize: 11, color: '#5e6671', marginTop: 4, fontFamily: "'IBM Plex Mono'" }}>{meta}</span>
)}
```

(Keep the `isRenaming` branch and the rest of `SessionItem` unchanged.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd services/frontend && npx vitest run src/components/chat/SessionItem.test.tsx`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/frontend/src/components/chat/ChatScreen.tsx services/frontend/src/components/chat/SessionItem.test.tsx
git commit -m "fix(frontend): stop chat-row height jump on hover

Always render the session meta line and keep the rename/delete actions
mounted (visibility-toggled) so hovering reveals them without reflowing
the row. Fixes the ~half-second size glitch in the left column."
```

---

### Task 2: Right column hidden by default, persisted (#2)

**Files:**
- Create: `services/frontend/src/components/chat/rightViewStore.ts` (tiny pure persistence helper — exported so it is unit-testable without a `ChatScreen` render harness)
- Create: `services/frontend/src/components/chat/rightViewStore.test.ts`
- Modify: `services/frontend/src/components/chat/ChatScreen.tsx` (the `rightView` state init ~line 1804; the `onSkills`/`onFiles` handlers ~lines 1910-1911; the in-conversation `setRightView(...)` calls ~lines 1967, 1971)

**Interfaces:**
- Consumes: existing `rightView: 'skills' | 'files' | null` state and `showRight = debug || rightView !== null` (`:1896`) — unchanged types.
- Produces:
  - `export type RightView = 'skills' | 'files' | null;`
  - `export function readRightView(): RightView` — reads `localStorage['lm.rightView']`; returns `null` (hidden) when unset/invalid/unavailable.
  - `export function writeRightView(v: RightView): void` — writes the key (removes it when `null`); try/catch-guarded.

**Why a separate module:** the persistence contract (default hidden + round-trip) is the testable part, and there is no `<ChatScreen>` render harness (see Global Constraints). Extracting these two pure functions lets us unit-test the contract directly, matching the codebase's "test exported helpers" style, while the `ChatScreen` wiring becomes a trivial, review-only change. Reference pattern: `sidebarCollapsed` (`ChatScreen.tsx:1813-1829`).

- [ ] **Step 1: Write the failing tests**

Create `services/frontend/src/components/chat/rightViewStore.test.ts`:

```ts
import { afterEach, describe, expect, it } from 'vitest';
import { readRightView, writeRightView } from './rightViewStore';

afterEach(() => {
  try { localStorage.removeItem('lm.rightView'); } catch { /* ignore */ }
});

describe('rightViewStore', () => {
  it('defaults to hidden (null) when nothing is stored', () => {
    expect(readRightView()).toBeNull();
  });

  it('round-trips skills and files', () => {
    writeRightView('skills');
    expect(localStorage.getItem('lm.rightView')).toBe('skills');
    expect(readRightView()).toBe('skills');

    writeRightView('files');
    expect(readRightView()).toBe('files');
  });

  it('writing null clears the stored value (back to hidden)', () => {
    writeRightView('skills');
    writeRightView(null);
    expect(localStorage.getItem('lm.rightView')).toBeNull();
    expect(readRightView()).toBeNull();
  });

  it('ignores an invalid stored value', () => {
    try { localStorage.setItem('lm.rightView', 'garbage'); } catch { /* ignore */ }
    expect(readRightView()).toBeNull();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/frontend && npx vitest run src/components/chat/rightViewStore.test.ts`
Expected: FAIL — module `./rightViewStore` does not exist.

- [ ] **Step 3: Implement the helper**

Create `services/frontend/src/components/chat/rightViewStore.ts`:

```ts
export type RightView = 'skills' | 'files' | null;

const KEY = 'lm.rightView';

/** Read the persisted right-column view. Defaults to null (hidden) when unset/invalid/unavailable. */
export function readRightView(): RightView {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'skills' || v === 'files' ? v : null;
  } catch {
    return null;
  }
}

/** Persist the right-column view; removing the key when hidden. try/catch-guarded (jsdom/privacy-safe). */
export function writeRightView(v: RightView): void {
  try {
    if (v === null) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, v);
  } catch {
    /* ignore */
  }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/frontend && npx vitest run src/components/chat/rightViewStore.test.ts`
Expected: PASS

- [ ] **Step 5: Wire the helper into ChatScreen**

In `ChatScreen.tsx`:

1. Import at the top with the other `./` imports:

```tsx
import { readRightView, writeRightView, type RightView } from './rightViewStore';
```

2. Change the state initializer (`:1804`) — default is now the persisted value (hidden when unset):

```tsx
const [rightView, setRightView] = useState<RightView>(() => readRightView());
```

3. Add a persisting setter near the other handlers, and use it everywhere `rightView` changes:

```tsx
const showRightView = (v: RightView) => { writeRightView(v); setRightView(v); };
```

4. Update the two TopBar handlers (`:1910-1911`) to persist on toggle:

```tsx
onSkills={() => showRightView(rightView === 'skills' ? null : 'skills')}
onFiles={() => showRightView(rightView === 'files' ? null : 'files')}
```

5. Replace the two in-conversation `setRightView('skills')` (`~:1967`) and `setRightView('files')` (`~:1971`) calls with `showRightView('skills')` / `showRightView('files')` so those paths persist too.

- [ ] **Step 6: Run the helper test + typecheck the wiring**

Run: `cd services/frontend && npx vitest run src/components/chat/rightViewStore.test.ts && npx tsc --noEmit`
Expected: tests PASS; `tsc` clean (confirms the `RightView` type + handler signatures line up).

- [ ] **Step 7: Commit**

```bash
git add services/frontend/src/components/chat/rightViewStore.ts services/frontend/src/components/chat/rightViewStore.test.ts services/frontend/src/components/chat/ChatScreen.tsx
git commit -m "feat(frontend): hide right column by default, persist last view

rightView now initializes from localStorage['lm.rightView'] (default
hidden) and writes back on every open/close, mirroring the left
sidebar's lm.sidebarCollapsed pattern."
```

---

### Task 3: Order the chat list by real activity, not on select (#1)

**Files:**
- Modify: `services/frontend/src/hooks/useLabmateWS.ts` (the `session.updated` reducer branch, lines 234-241)
- Test: `services/frontend/src/hooks/useLabmateWS.test.ts` (rewrite the existing ordering test at line 268; add an "open does not reorder" case)

**Interfaces:**
- Consumes: `Session` type from `@/types/events` (has `updatedAt?: string`, `id: string`).
- Produces: reducer contract — after any `session.updated`, `state.sessions` is upserted and ordered by `updatedAt` descending, tiebroken by `id` descending for stability. `activeSessionId` is unaffected by this branch.

**Root cause (verified):** the reducer unshifts to front on every `session.updated` (`useLabmateWS.ts:237-238`), and `session.open` emits `session.updated` (`server.py:391`) even though open does not change `updatedAt` (bumped only on create/rename/add_turn — `sessions.py` / `mongo_session_store.py`). So opening reorders incorrectly.

- [ ] **Step 1: Rewrite the failing test to encode updatedAt-ordering**

In `useLabmateWS.test.ts`, replace the body of the test at line 268 ("session.updated moves an existing session to the front...") with ordering-by-`updatedAt`. Keep the three-session setup (A/B/C with ascending `updatedAt`), but change the assertions and rename the test:

```ts
it('session.updated orders sessions by updatedAt desc; re-opening (same updatedAt) does not reorder', () => {
  const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
  act(() => mockWs.onopen?.());
  emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
  emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });

  const sessA = { id: 's-a', title: 'Session A', mode: 'chat' as const, turnCount: 0, contextTokens: 0, createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z' };
  const sessB = { id: 's-b', title: 'Session B', mode: 'chat' as const, turnCount: 0, contextTokens: 0, createdAt: '2026-01-01T00:00:01Z', updatedAt: '2026-01-01T00:00:01Z' };
  const sessC = { id: 's-c', title: 'Session C', mode: 'chat' as const, turnCount: 0, contextTokens: 0, createdAt: '2026-01-01T00:00:02Z', updatedAt: '2026-01-01T00:00:02Z' };

  emit({ type: 'session.updated', session: sessA });
  emit({ type: 'session.updated', session: sessB });
  emit({ type: 'session.updated', session: sessC });
  // Ordered by updatedAt desc: C(02) > B(01) > A(00)
  expect(result.current.state.sessions.map((s) => s.id)).toEqual(['s-c', 's-b', 's-a']);

  // Re-OPEN session A: backend re-emits session.updated with A's UNCHANGED updatedAt.
  // This must NOT move A to the front (the reported bug).
  emit({ type: 'session.updated', session: { ...sessA } });
  expect(result.current.state.sessions.map((s) => s.id)).toEqual(['s-c', 's-b', 's-a']);

  // Real ACTIVITY on A bumps updatedAt on the backend → A floats to the top.
  emit({ type: 'session.updated', session: { ...sessA, updatedAt: '2026-01-01T00:00:03Z' } });
  expect(result.current.state.sessions.map((s) => s.id)).toEqual(['s-a', 's-c', 's-b']);

  // Rename (existing id, unchanged updatedAt) replaces in place without reordering.
  emit({ type: 'session.updated', session: { ...sessC, title: 'Session C Renamed' } });
  const c = result.current.state.sessions.find((s) => s.id === 's-c');
  expect(c?.title).toBe('Session C Renamed');
  expect(result.current.state.sessions.map((s) => s.id)).toEqual(['s-a', 's-c', 's-b']);
});
```

Also keep/adjust the adjacent test at line 243 ("SESSION_UPDATED with unknown id adds session...") — with a single session it still ends up present; verify it passes unchanged (one session sorts to a one-element list).

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd services/frontend && npx vitest run src/hooks/useLabmateWS.test.ts -t "orders sessions by updatedAt"`
Expected: FAIL — with the current blind-unshift, re-opening A moves it to the front, so the "does not reorder" assertion fails.

- [ ] **Step 3: Implement — upsert + sort by updatedAt desc**

In `useLabmateWS.ts`, replace the `session.updated` branch (lines 234-241):

```ts
if (frame.type === 'session.updated') {
  if (state.phase === 'ready') {
    // Upsert the session, then order by real activity (updatedAt desc) rather
    // than frame-arrival. Opening a chat re-emits session.updated with an
    // UNCHANGED updatedAt, so re-sorting keeps its position; only real activity
    // (add_turn/rename) bumps updatedAt on the backend and floats it to the top.
    const others = state.sessions.filter((s) => s.id !== frame.session.id);
    const upserted = [...others, frame.session];
    upserted.sort((a, b) => {
      const at = a.updatedAt ?? '';
      const bt = b.updatedAt ?? '';
      if (at !== bt) return at < bt ? 1 : -1; // desc by ISO timestamp (lexicographic works for ISO)
      return a.id < b.id ? 1 : a.id > b.id ? -1 : 0; // stable tiebreak
    });
    return { ...state, sessions: upserted };
  }
  return state;
}
```

> ISO-8601 UTC timestamps (`...Z`) sort correctly as strings, so no `Date` parsing is needed. The tiebreak keeps equal-timestamp ordering deterministic across renders.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd services/frontend && npx vitest run src/hooks/useLabmateWS.test.ts -t "orders sessions by updatedAt"`
Expected: PASS

- [ ] **Step 5: Run the full hook test file**

Run: `cd services/frontend && npx vitest run src/hooks/useLabmateWS.test.ts`
Expected: all PASS. If the "unknown id adds session" test asserted a specific multi-session order via unshift, adjust it to the sorted order.

- [ ] **Step 6: Commit**

```bash
git add services/frontend/src/hooks/useLabmateWS.ts services/frontend/src/hooks/useLabmateWS.test.ts
git commit -m "fix(frontend): order chat list by updatedAt, not on select

session.updated now upserts + sorts by updatedAt desc instead of blindly
unshifting to the front. Opening a chat (which re-emits session.updated
with an unchanged updatedAt) no longer moves it to the top; only real
activity does."
```

---

### Task 4: New-session welcome view (#4) — the `~/Downloads/new_session/` design, fitted to live code

Reference: `~/Downloads/new_session/README.md` (+ `Labmate.dc.html`). Build the centered welcome —
logo → greeting → mode subtext → centered composer → three mode starter chips — as the
`turns.length === 0` state of the center column. **Live code wins on every conflict** (see the
reconciliation table in Global Constraints / spec Item #4). Split into 4 sub-tasks; each ends green
and commits.

**Files (whole task):**
- Create: `services/frontend/src/components/chat/newSessionContent.ts` (+ `.test.ts`) — pure copy/greeting helpers
- Create: `services/frontend/src/components/chat/NewSessionWelcome.tsx` (+ `.test.tsx`) — presentational welcome
- Modify: `services/frontend/src/styles/tokens.css` — add the `heroin` entrance keyframe
- Modify: `services/frontend/src/components/chat/ChatScreen.tsx` — export `Mode` (4A Step 1) + `Composer` (4C); add `seed` prop to `Composer`; render welcome/thread branch; gate `showRight`; top-bar title
- Test: `services/frontend/src/components/chat/Composer.seed.test.tsx`

**Sub-task order matters:** 4A exports `Mode` (needed by 4A/4B), 4B needs 4A's helpers, 4C adds the seedable `Composer`, 4D wires everything. Run them in order.

---

#### Task 4A: Pure content helpers (greeting + mode copy)

**Interfaces — Produces:**
- `export type WelcomeStarter = { icon: string; label: string; prompt: string }`
- `export function greetingFor(now: Date, name?: string): string` — `Good {morning|afternoon|evening}`, with `, ${name}` appended only when `name` is a non-empty string.
- `export function welcomeCopyFor(mode: Mode): { subtext: string; starters: WelcomeStarter[] }` — copy transcribed verbatim from the handoff.

- [ ] **Step 1: Export the `Mode` type from ChatScreen (single source for all Task 4 files)**

In `ChatScreen.tsx`, change `type Mode = 'chat' | 'paper' | 'code';` (line 34) to
`export type Mode = 'chat' | 'paper' | 'code';`. `Mode` is imported **type-only** by
`newSessionContent.ts`, `NewSessionWelcome.tsx` — no runtime import cycle. No other change here.

- [ ] **Step 2: Write the failing tests**

Create `services/frontend/src/components/chat/newSessionContent.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import { greetingFor, welcomeCopyFor } from './newSessionContent';

describe('greetingFor', () => {
  it('picks the time-of-day word by hour', () => {
    expect(greetingFor(new Date('2026-07-01T09:00:00'))).toBe('Good morning');
    expect(greetingFor(new Date('2026-07-01T14:00:00'))).toBe('Good afternoon');
    expect(greetingFor(new Date('2026-07-01T20:00:00'))).toBe('Good evening');
    expect(greetingFor(new Date('2026-07-01T03:00:00'))).toBe('Good evening'); // pre-dawn = evening
  });
  it('appends a name only when given', () => {
    expect(greetingFor(new Date('2026-07-01T09:00:00'), 'Jordan')).toBe('Good morning, Jordan');
    expect(greetingFor(new Date('2026-07-01T09:00:00'), '')).toBe('Good morning');
  });
});

describe('welcomeCopyFor', () => {
  it('returns mode-specific subtext and exactly three starters', () => {
    for (const mode of ['chat', 'paper', 'code'] as const) {
      const c = welcomeCopyFor(mode);
      expect(c.subtext.length).toBeGreaterThan(0);
      expect(c.starters).toHaveLength(3);
      for (const s of c.starters) {
        expect(s.icon).toBeTruthy();
        expect(s.label).toBeTruthy();
        expect(s.prompt).toBeTruthy();
      }
    }
  });
  it('uses the code starters for code mode', () => {
    expect(welcomeCopyFor('code').starters.map((s) => s.label)).toEqual([
      'Scaffold a service', 'Map the repo', 'Explain a diff',
    ]);
  });
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd services/frontend && npx vitest run src/components/chat/newSessionContent.test.ts`
Expected: FAIL — module `./newSessionContent` does not exist.

- [ ] **Step 4: Implement the helpers**

Create `services/frontend/src/components/chat/newSessionContent.ts` (import `Mode` type-only from `./ChatScreen` — exported in Step 1):

```ts
import type { Mode } from './ChatScreen';

export type WelcomeStarter = { icon: string; label: string; prompt: string };

export function greetingFor(now: Date, name?: string): string {
  const h = now.getHours();
  const tod = h >= 5 && h < 12 ? 'morning' : h >= 12 && h < 17 ? 'afternoon' : 'evening';
  const base = `Good ${tod}`;
  return name ? `${base}, ${name}` : base;
}

const COPY: Record<Mode, { subtext: string; starters: WelcomeStarter[] }> = {
  code: {
    subtext: 'Start a coding session, or paste a spec to pick up a milestone.',
    starters: [
      { icon: '⌘', label: 'Scaffold a service', prompt: 'Scaffold a service' },
      { icon: '⌗', label: 'Map the repo', prompt: 'Map the repo' },
      { icon: '▣', label: 'Explain a diff', prompt: 'Explain a diff' },
    ],
  },
  paper: {
    subtext: 'Draft a section, tighten a claim, or paste an outline to begin.',
    starters: [
      { icon: '📄', label: 'Draft a Results section', prompt: 'Draft a Results section' },
      { icon: '✎', label: 'Tighten my abstract', prompt: 'Tighten my abstract' },
      { icon: '✓', label: 'Check citations vs data', prompt: 'Check citations vs data' },
    ],
  },
  chat: {
    subtext: 'Ask about the codebase, a serving flag, or a past decision.',
    starters: [
      { icon: '💬', label: 'Summarize a paper', prompt: 'Summarize a paper' },
      { icon: '⚡', label: 'Explain a serving flag', prompt: 'Explain a serving flag' },
      { icon: '◈', label: 'Recall a past decision', prompt: 'Recall a past decision' },
    ],
  },
};

export function welcomeCopyFor(mode: Mode): { subtext: string; starters: WelcomeStarter[] } {
  return COPY[mode];
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/frontend && npx vitest run src/components/chat/newSessionContent.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/frontend/src/components/chat/newSessionContent.ts services/frontend/src/components/chat/newSessionContent.test.ts
git commit -m "feat(frontend): new-session welcome copy + greeting helpers"
```

---

#### Task 4B: The `NewSessionWelcome` presentational component

**Interfaces:**
- Consumes: `greetingFor`/`welcomeCopyFor` (4A); `LabmateMark` (`components/LabmateMark.tsx`, reused for the badge); `Mode` (exported in Task 4A Step 1; import type-only).
- Produces: `export function NewSessionWelcome(props: NewSessionWelcomeProps)` where
  `NewSessionWelcomeProps = { mode: Mode; greeting: string; onStarter: (prompt: string) => void; composer: ReactNode }`.
  Presentational only — renders badge + greeting + subtext + the `composer` slot + starter chips.
  Knows nothing about turns/sessions/WS. Renders NO `Composer` of its own — it renders the
  `composer` node it is handed (this is how the "exactly one Composer" invariant holds).

- [ ] **Step 1: Add the entrance keyframe to tokens.css**

In `services/frontend/src/styles/tokens.css`, near the existing `@keyframes orbitspin` (line ~62) and `.orbit-spin-slow` (~138), add:

```css
@keyframes heroin {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}
.lm-heroin { animation: heroin 0.5s ease both; }
```

- [ ] **Step 2: Write the failing test**

Create `services/frontend/src/components/chat/NewSessionWelcome.test.tsx`:

```tsx
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { NewSessionWelcome } from './NewSessionWelcome';

describe('NewSessionWelcome', () => {
  it('renders the badge, greeting, subtext, composer slot, and 3 starter chips', () => {
    render(
      <NewSessionWelcome
        mode="code"
        greeting="Good evening"
        onStarter={() => {}}
        composer={<textarea data-testid="stub-composer" />}
      />,
    );
    expect(screen.getByTestId('orbit-mark')).toBeInTheDocument(); // from LabmateMark
    expect(screen.getByText('Good evening')).toBeInTheDocument();
    expect(screen.getByText(/pick up a milestone/)).toBeInTheDocument(); // code subtext
    expect(screen.getByTestId('stub-composer')).toBeInTheDocument();
    expect(screen.getByText('Scaffold a service')).toBeInTheDocument();
    expect(screen.getAllByTestId('starter-chip')).toHaveLength(3);
  });

  it('calls onStarter with the starter prompt when a chip is clicked', () => {
    const onStarter = vi.fn();
    render(
      <NewSessionWelcome mode="code" greeting="Good evening" onStarter={onStarter}
        composer={<textarea />} />,
    );
    fireEvent.click(screen.getByText('Map the repo'));
    expect(onStarter).toHaveBeenCalledWith('Map the repo');
  });
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd services/frontend && npx vitest run src/components/chat/NewSessionWelcome.test.tsx`
Expected: FAIL — module `./NewSessionWelcome` does not exist.

- [ ] **Step 4: Implement the component** (design tokens from the handoff; badge reuses `LabmateMark`)

Create `services/frontend/src/components/chat/NewSessionWelcome.tsx`:

```tsx
import type { ReactNode } from 'react';
import { LabmateMark } from '../LabmateMark';
import type { Mode } from './ChatScreen';
import { welcomeCopyFor } from './newSessionContent';

export interface NewSessionWelcomeProps {
  mode: Mode;
  greeting: string;
  onStarter: (prompt: string) => void;
  composer: ReactNode;
}

export function NewSessionWelcome({ mode, greeting, onStarter, composer }: NewSessionWelcomeProps) {
  const { subtext, starters } = welcomeCopyFor(mode);
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: 0, padding: '24px 24px 60px' }}>
      <div className="lm-heroin" style={{ width: '100%', maxWidth: 640, display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        {/* Logo badge — reuse the shared mark (tile variant = gradient tile + orbital SVG) */}
        <div style={{ marginBottom: 24 }}>
          <LabmateMark size={62} variant="tile" spin="slow" />
        </div>

        <div style={{ fontSize: 28, fontWeight: 600, letterSpacing: '-0.02em', color: '#f0f2f5', textAlign: 'center', marginBottom: 9 }}>
          {greeting}
        </div>
        <div style={{ fontSize: 15, lineHeight: 1.5, color: '#7e8693', textAlign: 'center', marginBottom: 30, maxWidth: 460 }}>
          {subtext}
        </div>

        {/* Centered composer — the SAME <Composer> element ChatScreen uses when docked (passed in) */}
        <div style={{ width: '100%' }}>{composer}</div>

        {/* Starter chips */}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 9, justifyContent: 'center', marginTop: 22 }}>
          {starters.map((st) => (
            <button
              key={st.label}
              type="button"
              data-testid="starter-chip"
              onClick={() => onStarter(st.prompt)}
              style={{ display: 'flex', alignItems: 'center', gap: 8, border: '1px solid #20242c', background: '#13161c', borderRadius: 9, padding: '9px 13px', cursor: 'pointer', fontSize: 13, color: '#c7ccd3' }}
            >
              <span style={{ fontSize: 13, opacity: 0.85 }}>{st.icon}</span>
              {st.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
```

> Chip hover (`border-color:#2f3a48; background:#161a21`) is a visual nicety — add via a shared CSS class in `tokens.css` if you want it; not required for the test. Inline styles don't do `:hover`.

- [ ] **Step 5: Run to verify it passes**

Run: `cd services/frontend && npx vitest run src/components/chat/NewSessionWelcome.test.tsx`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/frontend/src/components/chat/NewSessionWelcome.tsx services/frontend/src/components/chat/NewSessionWelcome.test.tsx services/frontend/src/styles/tokens.css
git commit -m "feat(frontend): NewSessionWelcome centered welcome component"
```

---

#### Task 4C: Add an optional `seed` prop to the ChatScreen `Composer`

This is the ONE change to existing live-composer code — additive and backward-compatible — so a
starter chip can prefill the composer text. The `Composer` (`ChatScreen.tsx:1358`) owns its `text`
state; a `seed` with a changing `nonce` writes into it.

**Interfaces — Produces:** `Composer` gains an optional prop `seed?: { text: string; nonce: number } | null`.
When `nonce` changes, the composer's text becomes `seed.text`. Omitting `seed` (or `null`) is a no-op.
The `| null` matters: ChatScreen's seed state is `{...} | null` (Task 4D), so the prop must accept
`null` directly. `Composer` is newly exported for isolation testing.

- [ ] **Step 1: Export `Composer`**

In `ChatScreen.tsx`, change `function Composer(props: {` (line 1358) to `export function Composer(props: {`, and add `seed?: { text: string; nonce: number } | null;` to that inline props type.

- [ ] **Step 2: Write the failing test**

Create `services/frontend/src/components/chat/Composer.seed.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Composer } from './ChatScreen';

const base = {
  mode: 'chat' as const, budget: 1000, sessionId: 's-1',
  onSend: () => {}, onCompact: () => {}, isStreaming: false, onStop: () => {},
};

describe('Composer seed', () => {
  it('is empty with no seed', () => {
    render(<Composer {...base} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('');
  });

  it('prefills the textarea when a seed with a new nonce arrives', () => {
    const { rerender } = render(<Composer {...base} seed={{ text: 'Map the repo', nonce: 1 }} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Map the repo');
    rerender(<Composer {...base} seed={{ text: 'Explain a diff', nonce: 2 }} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Explain a diff');
  });
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd services/frontend && npx vitest run src/components/chat/Composer.seed.test.tsx`
Expected: FAIL — `Composer` is not exported / the seed prop does nothing (the prefill assertion fails).

- [ ] **Step 4: Implement the seed effect**

In `Composer` (`ChatScreen.tsx:1358+`), destructure `seed` from props, and add an effect near the existing auto-grow `useEffect` (which is around line 1373). Guard on the nonce so it only fires on a NEW seed (not on every render), and focus the textarea:

```tsx
const lastSeedNonce = useRef<number | null>(null);
useEffect(() => {
  if (!seed || seed.nonce === lastSeedNonce.current) return;
  lastSeedNonce.current = seed.nonce;
  setText(seed.text);
  requestAnimationFrame(() => taRef.current?.focus());
}, [seed]);
```

(`useRef` and `useEffect` are already imported in this file; `taRef`/`setText` already exist in `Composer`.)

- [ ] **Step 5: Run to verify it passes**

Run: `cd services/frontend && npx vitest run src/components/chat/Composer.seed.test.tsx`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/frontend/src/components/chat/ChatScreen.tsx services/frontend/src/components/chat/Composer.seed.test.tsx
git commit -m "feat(frontend): optional seed prop on Composer for starter prefill"
```

---

#### Task 4D: Wire the welcome into ChatScreen (branch, showRight gate, top-bar title)

**Interfaces — Consumes:** `NewSessionWelcome` (4B), `greetingFor` (4A), the seedable `Composer` (4C).
**Produces:** the `turns.length === 0` state of the center column is the welcome; otherwise the
existing thread + `ContextStrip` + docked composer. Right panel hidden and top bar reads "New chat"
while empty.

- [ ] **Step 1: (already done in Task 4A Step 1) confirm `Mode` is exported**

`export type Mode = 'chat' | 'paper' | 'code';` should already be in place from Task 4A Step 1.
No change if so.

- [ ] **Step 2: Add imports + seed state + the shared composer element**

Add imports at the top with the other `./` imports:

```tsx
import { NewSessionWelcome } from './NewSessionWelcome';
import { greetingFor } from './newSessionContent';
```

Inside the `ChatScreen` component body, add seed state near the other `useState`s (~line 1804-1810):

```tsx
const [seed, setSeed] = useState<{ text: string; nonce: number } | null>(null);
const seedNonce = useRef(0);
const seedComposer = (text: string) => setSeed({ text, nonce: ++seedNonce.current });
```

(`useRef` is already imported.)

- [ ] **Step 3: Gate `showRight` on non-empty + set the top-bar title**

Change `const showRight = debug || rightView !== null;` (`:1896`) to:

```tsx
const showWelcome = turns.length === 0;
const showRight = (debug || rightView !== null) && !showWelcome;
```

Change the TopBar title prop (`:1901`) from `sessionTitle={activeSession?.title ?? 'New session'}` to:

```tsx
sessionTitle={showWelcome ? 'New chat' : (activeSession?.title ?? 'New session')}
```

- [ ] **Step 4: Build the Composer element once and branch the center column**

The center column currently (≈`:1937-1992`) renders: a scroll `<div>` (thread), then `<ContextStrip …/>`,
then `<Composer …/>`. Refactor so the `Composer` is built once and the column shows welcome XOR thread.

First, just above the `return (`, build the shared element (copy the exact props from the current
`<Composer …/>` at `:1981`, and add `seed`):

```tsx
const composerEl = (
  <Composer
    mode={mode}
    budget={budget}
    sessionId={wsId}
    onSend={(text) => send(text, wsId, roots[0])}
    onCompact={() => compact(wsId)}
    isStreaming={!!streamingTurn}
    onStop={() => { if (streamingTurn) cancel(streamingTurn.id); }}
    seed={seed}
  />
);
```

Then replace the center column's inner content (the scroll `<div>` + `<ContextStrip/>` + `<Composer/>`
block, `≈:1938-1991`) with the branch — welcome when empty, else the existing thread using
`composerEl` in place of the inline `<Composer/>`:

```tsx
{showWelcome ? (
  <NewSessionWelcome
    mode={mode}
    greeting={greetingFor(new Date())}
    onStarter={seedComposer}
    composer={composerEl}
  />
) : (
  <>
    <div
      className="lm-scroll"
      ref={convScrollRef}
      onScroll={() => {
        const el = convScrollRef.current;
        if (el) {
          stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }
      }}
      style={{ flex: 1, overflowY: 'auto', padding: '34px 0 22px' }}
    >
      <div style={{ maxWidth: 680, margin: '0 auto', padding: '0 24px' }}>
        {turns.map((t) => /* …existing turn mapping, unchanged… */ null)}
      </div>
    </div>
    <ContextStrip window={state.contextWindow} open={sysOpen} onToggle={() => setSysOpen((o) => !o)} />
    {composerEl}
  </>
)}
```

> Keep the existing turn-mapping JSX (`turns.map(...)` for `UserBubble`/`AssistantTurnView`, `≈:1955-1976`) exactly as-is inside the thread branch — the `/* …existing… */` above is a placeholder for that unchanged block; do not rewrite it. The old inline empty-state line (`:1950-1954`, "Ask anything to get started.") is deleted (the welcome replaces it). `composerEl` appears in exactly one rendered branch at a time → one Composer mounts.

- [ ] **Step 5: Typecheck + run all the new tests**

Run: `cd services/frontend && npx tsc --noEmit && npx vitest run src/components/chat/newSessionContent.test.ts src/components/chat/NewSessionWelcome.test.tsx src/components/chat/Composer.seed.test.tsx`
Expected: `tsc` clean; all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add services/frontend/src/components/chat/ChatScreen.tsx
git commit -m "feat(frontend): render NewSessionWelcome for empty chats

turns.length === 0 renders the centered welcome (logo, greeting, mode
subtext, reused centered Composer, mode starter chips); starter chips
prefill the composer. Right panel hidden and top bar reads 'New chat'
while empty. One Composer element is reused across welcome/thread, so
exactly one mounts."
```

---

### Task 5: Full frontend suite green

**Files:** none (verification task).

- [ ] **Step 1: Run the whole frontend test suite**

Run: `cd services/frontend && npm test`
Expected: all PASS. Address any regressions in tests that assumed the old right-column default, old hover behavior, old list ordering, or the old inline empty state — the four tasks above call out each likely spot.

- [ ] **Step 2: Type-check (if the project exposes it)**

Run: `cd services/frontend && npx tsc --noEmit`
Expected: no errors. (The repo's pre-commit runs `frontend tsc --noEmit` on changed frontend files, so this must be clean before the commits land.)

- [ ] **Step 3: Confirm acceptance criteria (manual/visual, on the running app)**

Not automated (jsdom has no layout). When the app is run:
1. Click between chats — position unchanged; send a message — that chat jumps to top.
2. Fresh profile → right column hidden; open it, relaunch → it reopens.
3. Hover a chat row → rename/delete appear with no height jump.
4. New/empty chat → centered welcome: orbital badge (spinning), time-of-day greeting, mode subtext,
   centered composer, three mode starter chips; right panel hidden; top bar "New chat". Click a chip
   → composer prefilled. Switch mode → subtext/chips update, welcome stays. Send → thread renders,
   still one composer, top bar shows the session title, right panel available again.

---

## Notes for the implementer

- **Deferred / out of scope:** the optional backend tweak to stop emitting `session.updated` on `session.open` (`server.py:391`) is NOT part of this plan — Task 3's client-side sort fully fixes the bug and is robust regardless. Only revisit if a redundant re-render on open proves noticeable.
- **Item #4 fidelity vs live code:** the `~/Downloads/new_session/` prototype is the visual target, but **live code wins every conflict** — the reconciliations (welcome keyed on `turns.length === 0`, reused `Composer` + `LabmateMark`, time-of-day-only greeting, additive `seed` prop, `showRight` gate) are baked into Task 4. Do not reintroduce the prototype's `newChat` state machine, its static composer card, or a hardcoded name.
- **Greeting name** is intentionally omitted (no signed-in display name exists in client state). If/when one is plumbed in, pass it as `greetingFor(new Date(), name)` — the helper already supports it; no other change.
- **Chip hover** styling and any centered-then-docking composer *animation* are polish, not in scope; the composer is reused and repositioned by branch, not animated between positions.
- Selectors marked with `>` callouts (testids, accessible names, `textbox` role) must be verified against the real `TopBar`/`Composer`/`SkillsPanel` markup before relying on them — add stable `data-testid`s where missing rather than matching brittle text.
