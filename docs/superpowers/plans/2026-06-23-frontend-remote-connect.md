# Frontend Remote Connect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the frontend to a remote Labmate backend (ws_gateway + orchestrator) over WebSocket, with JWT login, boot sequence, and full chat — all configurable via a single `VITE_WS_URL` env var.

**Architecture:** A new `Root.tsx` owns the application lifecycle. It shows `LoginScreen` until the user authenticates via `POST /auth/login`, then opens a WebSocket connection managed by `useLabmateWS`, shows `BootScreen` while the server boot sequence runs, and finally shows `App` once `boot.ready` arrives. All WS-derived state flows from `Root` into `App` as props — no context providers needed. The `VITE_WS_URL` env var controls whether the WS points at localhost or a remote RunPod host; `API_URL` is derived from it automatically.

**Tech Stack:** React 18, TypeScript strict, Vite (`import.meta.env`), `@testing-library/react` `renderHook`, Vitest `vi.stubGlobal` for WebSocket mocking, `vi.mock` for hook/config mocking in Root tests.

---

## Files

| File | Action |
|---|---|
| `services/frontend/src/config.ts` | Create — exports `WS_URL` and `API_URL` from `VITE_WS_URL` |
| `services/frontend/.env.example` | Create — documents `VITE_WS_URL` for local and RunPod use |
| `services/frontend/src/hooks/useLabmateWS.ts` | Create — WebSocket client hook with reducer-driven state |
| `services/frontend/src/hooks/useLabmateWS.test.ts` | Create — hook tests with mocked global `WebSocket` |
| `services/frontend/src/Root.tsx` | Create — screen router: login → boot → chat |
| `services/frontend/src/Root.test.tsx` | Create — Root tests with mocked hook and `fetch` |
| `services/frontend/src/main.tsx` | Modify — render `<Root>` instead of `<App>` |

---

### Task 1: Config module + `.env.example`

**Files:**
- Create: `services/frontend/src/config.ts`
- Create: `services/frontend/.env.example`

No tests for this task — it's a one-liner constant, tested implicitly through Root tests.

- [ ] **Step 1: Create `config.ts`**

```ts
// services/frontend/src/config.ts
export const WS_URL: string =
  (import.meta.env.VITE_WS_URL as string | undefined) ?? 'ws://localhost:8787/ws';

// Derive HTTP base URL: ws:// → http://, wss:// → https://
export const API_URL: string = WS_URL.replace(/^ws/, 'http').replace(/\/ws$/, '');
```

- [ ] **Step 2: Create `.env.example`**

```dotenv
# services/frontend/.env.example

# WebSocket gateway URL.
# Local dev (default):  ws://localhost:8787/ws
# RunPod remote:        ws://<pod-id>.runpod.net:8787/ws
# Production (TLS):     wss://labmate.yourdomain.com/ws
VITE_WS_URL=ws://localhost:8787/ws
```

- [ ] **Step 3: Commit**

```bash
git add services/frontend/src/config.ts services/frontend/.env.example
git commit -m "feat(frontend): VITE_WS_URL config module + env example"
```

---

### Task 2: `useLabmateWS` hook

**Files:**
- Create: `services/frontend/src/hooks/useLabmateWS.ts`
- Test: `services/frontend/src/hooks/useLabmateWS.test.ts`

The hook accepts `(url, token, reconnectKey)`. When `token` is null it stays idle. When token is set, it opens a WebSocket, sends the auth frame on open, and dispatches incoming `StreamEvent`s into a `useReducer`. The `reconnectKey` parameter — an integer that `Root` increments — forces the effect to re-run and reconnect (used by the BootScreen Retry button).

- [ ] **Step 1: Write failing tests**

```ts
// services/frontend/src/hooks/useLabmateWS.test.ts
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useLabmateWS } from './useLabmateWS';

let mockWs: {
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onclose: (() => void) | null;
};

beforeEach(() => {
  mockWs = { send: vi.fn(), close: vi.fn(), onopen: null, onmessage: null, onclose: null };
  vi.stubGlobal('WebSocket', vi.fn(() => mockWs));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function emit(data: object) {
  act(() => mockWs.onmessage?.({ data: JSON.stringify(data) }));
}

const SUBSYSTEMS = [
  { id: 'brain' as const, label: 'Brain', detail: ':8000', state: 'pending' as const, required: true },
];

const BOOTSTRAP = {
  sessions: [],
  activeSessionId: null,
  agentStatus: {
    brain: { model: 'g', endpoint: ':8000', state: 'idle' as const, node: 'chat_node' as const, thinkingBudget: 0 },
    nervousSystem: { name: 'mcp', transport: 'stdio', state: 'connected' as const, toolsRegistered: 0 },
    hands: { skills: [] },
  },
};

describe('useLabmateWS', () => {
  it('stays idle and does not open a socket when token is null', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', null));
    expect(result.current.state.phase).toBe('idle');
    expect(vi.mocked(WebSocket)).not.toHaveBeenCalled();
  });

  it('transitions connecting → authenticating on open, sends auth frame', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    expect(result.current.state.phase).toBe('connecting');
    act(() => mockWs.onopen?.());
    expect(result.current.state.phase).toBe('authenticating');
    expect(mockWs.send).toHaveBeenCalledWith(JSON.stringify({ type: 'auth', token: 'tok' }));
  });

  it('transitions to booting on boot.plan and stores subsystems', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    expect(result.current.state.phase).toBe('booting');
    expect(result.current.state.subsystems).toEqual(SUBSYSTEMS);
  });

  it('updates a subsystem state on boot.update', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.update', id: 'brain', state: 'ready' });
    expect(result.current.state.subsystems[0].state).toBe('ready');
  });

  it('transitions to ready on boot.ready with sessions and agentStatus', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });
    expect(result.current.state.phase).toBe('ready');
    expect(result.current.state.agentStatus).toEqual(BOOTSTRAP.agentStatus);
  });

  it('appends answer.delta text to the matching turn', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: [] });
    const turn = { id: 't-1', sessionId: 's-1', role: 'assistant', text: '', createdAt: '', status: 'streaming' };
    emit({ type: 'turn.created', turn });
    emit({ type: 'answer.delta', turnId: 't-1', text: 'hel' });
    emit({ type: 'answer.delta', turnId: 't-1', text: 'lo' });
    expect(result.current.state.turns.find((t) => t.id === 't-1')?.text).toBe('hello');
  });

  it('records auth.error as error phase with authError set', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'auth.error', reason: 'expired' });
    expect(result.current.state.phase).toBe('error');
    expect(result.current.state.authError).toBe('expired');
  });

  it('send() writes a send frame to the socket', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    result.current.send('hello', 's-1');
    expect(mockWs.send).toHaveBeenCalledWith(
      JSON.stringify({ type: 'send', sessionId: 's-1', mode: 'chat', text: 'hello' }),
    );
  });

  it('reconnects (new WebSocket) when reconnectKey increments', () => {
    const { rerender } = renderHook(
      ({ k }: { k: number }) => useLabmateWS('ws://localhost:8787/ws', 'tok', k),
      { initialProps: { k: 0 } },
    );
    act(() => mockWs.onopen?.());
    rerender({ k: 1 });
    expect(vi.mocked(WebSocket)).toHaveBeenCalledTimes(2);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zachstallbohm/Work/Labmate/services/frontend
npm test -- --reporter=verbose src/hooks/useLabmateWS.test.ts
```

Expected: all 9 tests fail with "Cannot find module './useLabmateWS'"

- [ ] **Step 3: Create the hook**

```ts
// services/frontend/src/hooks/useLabmateWS.ts
import { useCallback, useEffect, useReducer, useRef } from 'react';
import type {
  AgentStatus,
  ContextWindow,
  Mode,
  Session,
  StreamEvent,
  Subsystem,
  SubsystemId,
  SubsystemState,
  Turn,
} from '@/types/events';

export type WsPhase = 'idle' | 'connecting' | 'authenticating' | 'booting' | 'ready' | 'error';

export interface WsState {
  phase: WsPhase;
  subsystems: Subsystem[];
  sessions: Session[];
  activeSessionId: string | null;
  agentStatus: AgentStatus | null;
  context: ContextWindow | null;
  turns: Turn[];
  authError: string | null;
}

type Action =
  | { type: 'CONNECTING' }
  | { type: 'AUTHENTICATING' }
  | { type: 'AUTH_ERROR'; reason: string }
  | { type: 'BOOT_PLAN'; subsystems: Subsystem[] }
  | { type: 'BOOT_UPDATE'; id: SubsystemId; state: SubsystemState; message?: string }
  | { type: 'BOOT_READY'; sessions: Session[]; activeSessionId: string | null; agentStatus: AgentStatus }
  | { type: 'TURN_CREATED'; turn: Turn }
  | { type: 'ANSWER_DELTA'; turnId: string; text: string }
  | { type: 'TURN_DONE'; turnId: string; status: 'complete' | 'error' }
  | { type: 'CONTEXT_UPDATE'; window: ContextWindow }
  | { type: 'AGENT_STATUS'; status: AgentStatus }
  | { type: 'SESSION_UPDATED'; session: Session }
  | { type: 'CLOSED' };

const INITIAL: WsState = {
  phase: 'idle',
  subsystems: [],
  sessions: [],
  activeSessionId: null,
  agentStatus: null,
  context: null,
  turns: [],
  authError: null,
};

function reducer(state: WsState, action: Action): WsState {
  switch (action.type) {
    case 'CONNECTING':
      return { ...INITIAL, phase: 'connecting' };
    case 'AUTHENTICATING':
      return { ...state, phase: 'authenticating' };
    case 'AUTH_ERROR':
      return { ...state, phase: 'error', authError: action.reason };
    case 'BOOT_PLAN':
      return { ...state, phase: 'booting', subsystems: action.subsystems };
    case 'BOOT_UPDATE':
      return {
        ...state,
        subsystems: state.subsystems.map((s) =>
          s.id === action.id
            ? { ...s, state: action.state, message: action.message ?? s.message }
            : s,
        ),
      };
    case 'BOOT_READY':
      return {
        ...state,
        phase: 'ready',
        sessions: action.sessions,
        activeSessionId: action.activeSessionId,
        agentStatus: action.agentStatus,
      };
    case 'TURN_CREATED':
      return { ...state, turns: [...state.turns, action.turn] };
    case 'ANSWER_DELTA':
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.id === action.turnId ? { ...t, text: t.text + action.text } : t,
        ),
      };
    case 'TURN_DONE':
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.id === action.turnId ? { ...t, status: action.status } : t,
        ),
      };
    case 'CONTEXT_UPDATE':
      return { ...state, context: action.window };
    case 'AGENT_STATUS':
      return { ...state, agentStatus: action.status };
    case 'SESSION_UPDATED':
      return {
        ...state,
        sessions: state.sessions.map((s) => (s.id === action.session.id ? action.session : s)),
      };
    case 'CLOSED':
      return { ...state, phase: state.phase === 'ready' ? 'error' : 'idle' };
    default:
      return state;
  }
}

export function useLabmateWS(url: string, token: string | null, reconnectKey = 0) {
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!token) return;

    dispatch({ type: 'CONNECTING' });
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      dispatch({ type: 'AUTHENTICATING' });
      ws.send(JSON.stringify({ type: 'auth', token }));
    };

    ws.onmessage = (ev: MessageEvent<string>) => {
      const event = JSON.parse(ev.data) as StreamEvent;
      switch (event.type) {
        case 'auth.ok':
          break;
        case 'auth.error':
          dispatch({ type: 'AUTH_ERROR', reason: event.reason });
          break;
        case 'boot.plan':
          dispatch({ type: 'BOOT_PLAN', subsystems: event.subsystems });
          break;
        case 'boot.update':
          dispatch({ type: 'BOOT_UPDATE', id: event.id, state: event.state, message: event.message });
          break;
        case 'boot.error':
          dispatch({ type: 'BOOT_UPDATE', id: event.id, state: 'failed', message: event.message });
          break;
        case 'boot.ready':
          dispatch({
            type: 'BOOT_READY',
            sessions: event.sessionBootstrap.sessions,
            activeSessionId: event.sessionBootstrap.activeSessionId,
            agentStatus: event.sessionBootstrap.agentStatus,
          });
          break;
        case 'turn.created':
          dispatch({ type: 'TURN_CREATED', turn: event.turn });
          break;
        case 'answer.delta':
          dispatch({ type: 'ANSWER_DELTA', turnId: event.turnId, text: event.text });
          break;
        case 'turn.done':
          dispatch({ type: 'TURN_DONE', turnId: event.turnId, status: event.status });
          break;
        case 'context.update':
          dispatch({ type: 'CONTEXT_UPDATE', window: event.window });
          break;
        case 'agent.status':
          dispatch({ type: 'AGENT_STATUS', status: event.status });
          break;
        case 'session.updated':
          dispatch({ type: 'SESSION_UPDATED', session: event.session });
          break;
      }
    };

    ws.onclose = () => {
      dispatch({ type: 'CLOSED' });
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, [url, token, reconnectKey]);

  const send = useCallback((text: string, sessionId: string) => {
    wsRef.current?.send(JSON.stringify({ type: 'send', sessionId, mode: 'chat', text }));
  }, []);

  const newSession = useCallback((mode: Mode) => {
    wsRef.current?.send(JSON.stringify({ type: 'session.new', mode }));
  }, []);

  const openSession = useCallback((sessionId: string) => {
    wsRef.current?.send(JSON.stringify({ type: 'session.open', sessionId }));
  }, []);

  return { state, send, newSession, openSession };
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
npm test -- --reporter=verbose src/hooks/useLabmateWS.test.ts
```

Expected: 9/9 pass

- [ ] **Step 5: Commit**

```bash
git add services/frontend/src/hooks/useLabmateWS.ts services/frontend/src/hooks/useLabmateWS.test.ts
git commit -m "feat(frontend): useLabmateWS hook — WS client with reducer-driven state"
```

---

### Task 3: `Root.tsx` screen router

**Files:**
- Create: `services/frontend/src/Root.tsx`
- Test: `services/frontend/src/Root.test.tsx`
- Modify: `services/frontend/src/main.tsx`

`Root` manages three things: (1) JWT stored in `localStorage` (remember=true) or `sessionStorage` (remember=false), (2) a `fetch` POST to `/auth/login` to obtain the token, (3) passing the token to `useLabmateWS` and routing to the correct screen based on `state.phase`.

- [ ] **Step 1: Write failing tests**

```tsx
// services/frontend/src/Root.test.tsx
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { Root } from './Root';

vi.mock('@/hooks/useLabmateWS', () => ({ useLabmateWS: vi.fn() }));
vi.mock('@/config', () => ({ WS_URL: 'ws://localhost:8787/ws', API_URL: 'http://localhost:8787' }));

import { useLabmateWS } from '@/hooks/useLabmateWS';

const IDLE_STATE = {
  phase: 'idle' as const,
  subsystems: [],
  sessions: [],
  activeSessionId: null,
  agentStatus: null,
  context: null,
  turns: [],
  authError: null,
};

const AGENT_STATUS = {
  brain: { model: 'g', endpoint: ':8000', state: 'idle' as const, node: 'chat_node' as const, thinkingBudget: 0 },
  nervousSystem: { name: 'mcp', transport: 'stdio', state: 'connected' as const, toolsRegistered: 0 },
  hands: { skills: [] },
};

const CONTEXT = {
  max: 8000, used: 0, free: 8000,
  segments: { systemPrompt: 0, skillInstructions: 0, conversation: 0, workingMemory: 0, reasoning: 0 },
};

beforeEach(() => {
  vi.mocked(useLabmateWS).mockReturnValue({
    state: IDLE_STATE, send: vi.fn(), newSession: vi.fn(), openSession: vi.fn(),
  });
  localStorage.clear();
  sessionStorage.clear();
});

describe('Root', () => {
  it('shows LoginScreen when no token is stored', () => {
    render(<Root />);
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
  });

  it('shows BootScreen when token exists but phase is booting', () => {
    localStorage.setItem('labmate_token', 'tok');
    vi.mocked(useLabmateWS).mockReturnValue({
      state: { ...IDLE_STATE, phase: 'booting', subsystems: [] },
      send: vi.fn(), newSession: vi.fn(), openSession: vi.fn(),
    });
    render(<Root />);
    expect(screen.getByTestId('boot-progress')).toBeInTheDocument();
  });

  it('shows the chat layout when phase is ready', () => {
    localStorage.setItem('labmate_token', 'tok');
    vi.mocked(useLabmateWS).mockReturnValue({
      state: { ...IDLE_STATE, phase: 'ready', agentStatus: AGENT_STATUS, context: CONTEXT },
      send: vi.fn(), newSession: vi.fn(), openSession: vi.fn(),
    });
    render(<Root />);
    expect(screen.getByTestId('layout-topbar')).toBeInTheDocument();
  });

  it('POSTs /auth/login and stores JWT in sessionStorage when remember is false', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ token: 'jwt123' }),
    });
    render(<Root />);
    await userEvent.type(screen.getByLabelText('Email'), 'a@b.com');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    await waitFor(() => expect(sessionStorage.getItem('labmate_token')).toBe('jwt123'));
    expect(localStorage.getItem('labmate_token')).toBeNull();
  });

  it('stores JWT in localStorage when "Keep me signed in" is checked', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ token: 'jwt456' }),
    });
    render(<Root />);
    await userEvent.type(screen.getByLabelText('Email'), 'a@b.com');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByLabelText(/keep me signed in/i));
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    await waitFor(() => expect(localStorage.getItem('labmate_token')).toBe('jwt456'));
  });

  it('shows a login error alert on 401', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({ detail: 'invalid_credentials' }),
    });
    render(<Root />);
    await userEvent.type(screen.getByLabelText('Email'), 'a@b.com');
    await userEvent.type(screen.getByLabelText('Password'), 'wrong');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
  });

  it('clears the stored token and returns to LoginScreen when WS reports authError', async () => {
    localStorage.setItem('labmate_token', 'stale-tok');
    vi.mocked(useLabmateWS).mockReturnValue({
      state: { ...IDLE_STATE, phase: 'error', authError: 'expired' },
      send: vi.fn(), newSession: vi.fn(), openSession: vi.fn(),
    });
    render(<Root />);
    await waitFor(() => expect(screen.getByLabelText('Email')).toBeInTheDocument());
    expect(localStorage.getItem('labmate_token')).toBeNull();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
npm test -- --reporter=verbose src/Root.test.tsx
```

Expected: all 7 tests fail with "Cannot find module './Root'"

- [ ] **Step 3: Create `Root.tsx`**

```tsx
// services/frontend/src/Root.tsx
import { useCallback, useEffect, useState } from 'react';
import { App } from '@/App';
import { BootScreen } from '@/screens/BootScreen';
import { LoginScreen, type LoginCredentials } from '@/screens/LoginScreen';
import { useLabmateWS } from '@/hooks/useLabmateWS';
import { API_URL, WS_URL } from '@/config';
import type { SubsystemId } from '@/types/events';

function loadToken(): string | null {
  return localStorage.getItem('labmate_token') ?? sessionStorage.getItem('labmate_token');
}

function storeToken(token: string, remember: boolean): void {
  if (remember) {
    localStorage.setItem('labmate_token', token);
    sessionStorage.removeItem('labmate_token');
  } else {
    sessionStorage.setItem('labmate_token', token);
    localStorage.removeItem('labmate_token');
  }
}

function clearToken(): void {
  localStorage.removeItem('labmate_token');
  sessionStorage.removeItem('labmate_token');
}

export function Root() {
  const [token, setToken] = useState<string | null>(loadToken);
  const [loginError, setLoginError] = useState<string | undefined>();
  const [submitting, setSubmitting] = useState(false);
  const [reconnectKey, setReconnectKey] = useState(0);

  const { state, send, newSession, openSession } = useLabmateWS(WS_URL, token, reconnectKey);

  // WS reported a stale or invalid token — clear storage and return to login
  useEffect(() => {
    if (state.authError) {
      clearToken();
      setToken(null);
      setLoginError(state.authError);
    }
  }, [state.authError]);

  const handleLogin = useCallback(async (creds: LoginCredentials) => {
    setSubmitting(true);
    setLoginError(undefined);
    try {
      const res = await fetch(`${API_URL}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: creds.email, password: creds.password }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        setLoginError(body.detail ?? 'invalid_credentials');
        return;
      }
      const { token: tok } = (await res.json()) as { token: string };
      storeToken(tok, creds.remember);
      setToken(tok);
    } catch {
      setLoginError('Cannot reach the server. Check your connection.');
    } finally {
      setSubmitting(false);
    }
  }, []);

  const handleRetry = useCallback((_id: SubsystemId) => {
    setReconnectKey((k) => k + 1);
  }, []);

  if (!token) {
    return <LoginScreen onSubmit={handleLogin} submitting={submitting} error={loginError} />;
  }

  if (state.phase !== 'ready') {
    return <BootScreen subsystems={state.subsystems} onRetry={handleRetry} />;
  }

  return (
    <App
      sessions={state.sessions}
      turns={state.turns}
      activeSessionId={state.activeSessionId}
      agentStatus={state.agentStatus ?? undefined}
      context={state.context ?? undefined}
      onSend={(text) => send(text, state.activeSessionId ?? '')}
      onOpenSession={openSession}
      onNewSession={newSession}
    />
  );
}
```

- [ ] **Step 4: Update `main.tsx` to render `Root`**

```tsx
// services/frontend/src/main.tsx
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Root } from './Root';
import './styles/tokens.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
```

- [ ] **Step 5: Run the full test suite**

```bash
npm test -- --reporter=verbose
```

Expected: 83 tests pass across 20 files (67 existing + 9 hook + 7 Root)

- [ ] **Step 6: Commit**

```bash
git add services/frontend/src/Root.tsx services/frontend/src/Root.test.tsx services/frontend/src/main.tsx
git commit -m "feat(frontend): Root screen router — login → boot → chat"
```

---

## Using it locally against RunPod

Create `services/frontend/.env.local` (git-ignored by Vite by default):

```dotenv
VITE_WS_URL=ws://<your-pod-id>.runpod.net:8787/ws
```

On RunPod, set the ws_gateway `CORS_ORIGINS` env var to include your local origin:

```
CORS_ORIGINS=http://localhost:8080
```

Then locally:

```bash
cd services/frontend
npm run dev   # http://localhost:8080
```

Sign in with your admin credentials → boot sequence runs on RunPod → chat is live against the remote instance.
