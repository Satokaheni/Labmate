import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useLabmateWS, ensureActiveSession, mintSessionId } from './useLabmateWS';
import { capabilitiesFrame } from '@/protocol/capabilities';

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

  it('send() includes workspaceRoot when provided', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    result.current.send('hello', 's-1', '/abs/workspace');
    expect(mockWs.send).toHaveBeenCalledWith(
      JSON.stringify({ type: 'send', sessionId: 's-1', mode: 'chat', text: 'hello', workspaceRoot: '/abs/workspace' }),
    );
  });

  it('send() omits workspaceRoot when not provided', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    result.current.send('hello', 's-1', undefined);
    const sent = JSON.parse(mockWs.send.mock.calls[0][0] as string);
    expect(sent.workspaceRoot).toBeUndefined();
    expect(sent).not.toHaveProperty('workspaceRoot');
  });

  it('services tool.request via electronAPI and replies tool.result', async () => {
    const executeTool = vi.fn().mockResolvedValue({ result: { content: "hi" } });
    // Set electronAPI directly to avoid replacing window (which breaks React DOM instanceof checks)
    (window as Window & { electronAPI?: unknown }).electronAPI = {
      config: { wsUrl: null, isDev: true }, token: null,
      setConfig: vi.fn(), setToken: vi.fn(), clearToken: vi.fn(), executeTool,
      getMcpTools: vi.fn().mockResolvedValue([]),
      getWorkspaceRoots: vi.fn().mockResolvedValue([]),
      addWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      removeWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      hasDefaultWorkspace: vi.fn().mockResolvedValue(true),
      getDefaultWorkspace: vi.fn().mockResolvedValue(null),
      setDefaultWorkspace: vi.fn().mockResolvedValue({ path: null }),
      searchWorkspace: vi.fn().mockResolvedValue({ entries: [] }),
    };

    try {
      renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
      act(() => {
        mockWs.onopen?.();
      });
      act(() => {
        mockWs.onmessage?.({
          data: JSON.stringify({
            type: 'tool.request',
            turnId: 'turn-1',
            toolRequestId: 'req-1',
            name: 'read_file',
            args: { path: 'a.txt' },
          }),
        });
      });

      await vi.waitFor(() => {
        expect(executeTool).toHaveBeenCalledWith('read_file', { path: 'a.txt' }, null);
      });
      await vi.waitFor(() => {
        const sent = mockWs.send.mock.calls.map((c) => JSON.parse(c[0] as string));
        expect(sent).toContainEqual({
          type: 'tool.result',
          toolRequestId: 'req-1',
          result: { content: 'hi' },
          error: undefined,
        });
      });
    } finally {
      delete (window as Window & { electronAPI?: unknown }).electronAPI;
    }
  });

  it("routes a tool.request to the workspace of its turn session", async () => {
    const executeTool = vi.fn().mockResolvedValue({ result: { content: 'hi' } });
    (window as Window & { electronAPI?: unknown }).electronAPI = {
      config: { wsUrl: null, isDev: true }, token: null,
      setConfig: vi.fn(), setToken: vi.fn(), clearToken: vi.fn(), executeTool,
      getMcpTools: vi.fn().mockResolvedValue([]),
      getWorkspaceRoots: vi.fn().mockResolvedValue([]),
      addWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      removeWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      hasDefaultWorkspace: vi.fn().mockResolvedValue(true),
      getDefaultWorkspace: vi.fn().mockResolvedValue(null),
      setDefaultWorkspace: vi.fn().mockResolvedValue({ path: null }),
      searchWorkspace: vi.fn().mockResolvedValue({ entries: [] }),
    };

    try {
      renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
      act(() => { mockWs.onopen?.(); });
      // The assistant turn announces its session first…
      act(() => {
        mockWs.onmessage?.({
          data: JSON.stringify({ type: 'turn.created', turn: { id: 'turn-7', sessionId: 'sess-X', role: 'assistant' } }),
        });
      });
      // …then a tool.request carrying only that turnId must resolve to sess-X.
      act(() => {
        mockWs.onmessage?.({
          data: JSON.stringify({ type: 'tool.request', turnId: 'turn-7', toolRequestId: 'req-7', name: 'read_file', args: { path: 'a.txt' } }),
        });
      });

      await vi.waitFor(() => {
        expect(executeTool).toHaveBeenCalledWith('read_file', { path: 'a.txt' }, 'sess-X');
      });
    } finally {
      delete (window as Window & { electronAPI?: unknown }).electronAPI;
    }
  });

  it('replies tool.result with error when electronAPI is absent', async () => {
    // Ensure electronAPI is not present
    delete (window as Window & { electronAPI?: unknown }).electronAPI;

    renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => {
      mockWs.onopen?.();
    });
    act(() => {
      mockWs.onmessage?.({
        data: JSON.stringify({
          type: 'tool.request',
          turnId: 'turn-1',
          toolRequestId: 'req-2',
          name: 'read_file',
          args: { path: 'a.txt' },
        }),
      });
    });
    await vi.waitFor(() => {
      const sent = mockWs.send.mock.calls.map((c) => JSON.parse(c[0] as string));
      const frame = sent.find((f) => f.toolRequestId === 'req-2');
      expect(frame?.type).toBe('tool.result');
      expect(frame?.error).toMatch(/no local filesystem/i);
    });
  });

  it('SESSION_UPDATED with unknown id adds session to list; second update renames in place', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });

    const newSession: import('@/types/events').Session = {
      id: 's-new',
      title: 'New session',
      mode: 'code' as const,
      turnCount: 0,
      contextTokens: 0,
      createdAt: '2026-01-01T00:00:00Z',
      updatedAt: '2026-01-01T00:00:00Z',
    };
    emit({ type: 'session.updated', session: newSession });
    expect(result.current.state.sessions).toHaveLength(1);
    expect(result.current.state.sessions[0].id).toBe('s-new');

    // emit again with rename — must not duplicate, must update in place
    emit({ type: 'session.updated', session: { ...newSession, title: 'Renamed' } });
    expect(result.current.state.sessions).toHaveLength(1);
    expect(result.current.state.sessions[0].title).toBe('Renamed');
  });

  it('reasoning.done attaches reasoning to the matching turn', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });

    const turn: import('@/types/events').Turn = {
      id: 't-reason', sessionId: 's1', role: 'assistant', text: '',
      createdAt: '2026-01-01T00:00:00Z', status: 'streaming',
    };
    emit({ type: 'turn.created', turn });

    const reasoning: import('@/types/events').Reasoning = {
      summary: 'I think',
      text: 'I think carefully',
      node: 'plan_node',
      tokens: 5,
      budget: 0,
      durationMs: 100,
    };
    emit({ type: 'reasoning.done', turnId: 't-reason', reasoning });

    const updated = result.current.state.turns.find((t) => t.id === 't-reason');
    expect(updated?.reasoning).toEqual(reasoning);
  });

  it('artifact.created appends artifact to the matching turn', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });

    const turn: import('@/types/events').Turn = {
      id: 't-artifact', sessionId: 's1', role: 'assistant', text: '',
      createdAt: '2026-01-01T00:00:00Z', status: 'streaming',
    };
    emit({ type: 'turn.created', turn });

    const artifact: import('@/types/events').Artifact = {
      id: 'art-1', name: 'server.py', path: 'services/ws_gateway/server.py',
      language: 'Python', mime: 'text/x-python', sizeBytes: 512, lineCount: 20,
      preview: 'code', content: '# code', downloadUrl: '/artifacts/art-1',
    };
    emit({ type: 'artifact.created', turnId: 't-artifact', artifact });

    const updated = result.current.state.turns.find((t) => t.id === 't-artifact');
    expect(updated?.artifacts).toHaveLength(1);
    expect(updated?.artifacts?.[0]).toEqual(artifact);
  });

  it('second artifact.created appends without removing the first', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });

    const turn: import('@/types/events').Turn = {
      id: 't-multi', sessionId: 's1', role: 'assistant', text: '',
      createdAt: '2026-01-01T00:00:00Z', status: 'streaming',
    };
    emit({ type: 'turn.created', turn });

    const mkArtifact = (id: string): import('@/types/events').Artifact => ({
      id, name: `${id}.py`, path: `${id}.py`, language: 'Python', mime: 'text/x-python',
      sizeBytes: 100, lineCount: 5, preview: 'code', content: '#', downloadUrl: `/artifacts/${id}`,
    });
    emit({ type: 'artifact.created', turnId: 't-multi', artifact: mkArtifact('a1') });
    emit({ type: 'artifact.created', turnId: 't-multi', artifact: mkArtifact('a2') });

    const updated = result.current.state.turns.find((t) => t.id === 't-multi');
    expect(updated?.artifacts).toHaveLength(2);
  });

  it('setDebug sends a debug.set frame to the server', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    result.current.setDebug('s-abc', true);
    expect(mockWs.send).toHaveBeenCalledWith(
      JSON.stringify({ type: 'debug.set', sessionId: 's-abc', enabled: true }),
    );
  });

  it('reconnects (new WebSocket) when reconnectKey increments and closes the old one', () => {
    const instances: typeof mockWs[] = [];
    vi.mocked(WebSocket).mockImplementation(() => {
      const inst = { send: vi.fn(), close: vi.fn(), onopen: null as (() => void) | null, onmessage: null as ((ev: { data: string }) => void) | null, onclose: null as (() => void) | null };
      instances.push(inst);
      return inst as unknown as WebSocket;
    });

    const { rerender } = renderHook(
      ({ k }: { k: number }) => useLabmateWS('ws://localhost:8787/ws', 'tok', k),
      { initialProps: { k: 0 } },
    );
    act(() => instances[0]?.onopen?.());
    rerender({ k: 1 });

    expect(instances).toHaveLength(2);
    expect(instances[0].close).toHaveBeenCalledTimes(1);
  });

  it('newChat() mints a fresh active session id (no backend round-trip)', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });
    let id = '';
    act(() => {
      id = result.current.newChat();
    });
    expect(id).toMatch(/^s-/);
    expect(result.current.state.activeSessionId).toBe(id);
  });

  it('boot.ready with no active session mints exactly one — created at the source, not in a view effect', () => {
    const { result, rerender } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    // BOOTSTRAP has sessions:[] and activeSessionId:null
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });
    const id = result.current.state.activeSessionId;
    expect(id).toMatch(/^s-/); // a session exists immediately, no reactive newChat() needed
    // Stable across re-renders: created once, no churn (the old useEffect could re-fire).
    rerender();
    expect(result.current.state.activeSessionId).toBe(id);
  });

  it('boot.ready that already names an active session does not mint a new one', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: { ...BOOTSTRAP, activeSessionId: 's-existing' } });
    expect(result.current.state.activeSessionId).toBe('s-existing');
  });

  it('openSession() sets the active session and sends session.open', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });
    act(() => result.current.openSession?.('s-xyz'));
    expect(result.current.state.activeSessionId).toBe('s-xyz');
    expect(mockWs.send).toHaveBeenCalledWith(
      JSON.stringify({ type: 'session.open', sessionId: 's-xyz' }),
    );
  });

  it('session.history merges turns, deduplicating by id', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });

    // Add one turn to the state
    const turn1: import('@/types/events').Turn = {
      id: 't-1',
      sessionId: 's-1',
      role: 'user',
      text: 'Hello',
      createdAt: '2026-01-01T00:00:00Z',
      status: 'complete',
    };
    emit({ type: 'turn.created', turn: turn1 });
    expect(result.current.state.turns).toHaveLength(1);

    // Emit session.history with two turns: one already in state (t-1), one new (t-2)
    const turn2: import('@/types/events').Turn = {
      id: 't-2',
      sessionId: 's-1',
      role: 'assistant',
      text: 'Hi',
      createdAt: '2026-01-01T00:00:01Z',
      status: 'complete',
    };
    emit({ type: 'session.history', sessionId: 's-1', turns: [turn1, turn2] });

    // Should have both turns, no duplicate of t-1
    expect(result.current.state.turns).toHaveLength(2);
    expect(result.current.state.turns.map((t) => t.id)).toEqual(['t-1', 't-2']);
  });

  it('session.history populates turnSessionRef so tool routing works', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });

    const turns: import('@/types/events').Turn[] = [
      {
        id: 't-1',
        sessionId: 's-chat-1',
        role: 'user',
        text: 'Read file',
        createdAt: '2026-01-01T00:00:00Z',
        status: 'complete',
      },
      {
        id: 't-2',
        sessionId: 's-chat-1',
        role: 'assistant',
        text: 'Done',
        createdAt: '2026-01-01T00:00:01Z',
        status: 'complete',
      },
    ];
    emit({ type: 'session.history', sessionId: 's-chat-1', turns });

    // Should have loaded both turns
    expect(result.current.state.turns).toHaveLength(2);
  });

  it('session.history drops previous session turns when switching to a new session', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });

    // Load session A with two turns
    const sessionATurns: import('@/types/events').Turn[] = [
      {
        id: 't-a-1',
        sessionId: 's-session-a',
        role: 'user',
        text: 'Session A message 1',
        createdAt: '2026-01-01T00:00:00Z',
        status: 'complete',
      },
      {
        id: 't-a-2',
        sessionId: 's-session-a',
        role: 'assistant',
        text: 'Session A response',
        createdAt: '2026-01-01T00:00:01Z',
        status: 'complete',
      },
    ];
    emit({ type: 'session.history', sessionId: 's-session-a', turns: sessionATurns });
    expect(result.current.state.turns).toHaveLength(2);
    expect(result.current.state.turns.every((t) => t.sessionId === 's-session-a')).toBe(true);

    // Switch to session B with its own turns
    const sessionBTurns: import('@/types/events').Turn[] = [
      {
        id: 't-b-1',
        sessionId: 's-session-b',
        role: 'user',
        text: 'Session B message',
        createdAt: '2026-01-01T00:00:02Z',
        status: 'complete',
      },
    ];
    emit({ type: 'session.history', sessionId: 's-session-b', turns: sessionBTurns });

    // Should only have session B turns, no A turns leakage
    expect(result.current.state.turns).toHaveLength(1);
    expect(result.current.state.turns[0].id).toBe('t-b-1');
    expect(result.current.state.turns[0].sessionId).toBe('s-session-b');
  });

  it('session.history is idempotent when called twice with same turns', () => {
    const { result } = renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
    act(() => mockWs.onopen?.());
    emit({ type: 'boot.plan', subsystems: SUBSYSTEMS });
    emit({ type: 'boot.ready', sessionBootstrap: BOOTSTRAP });

    const turns: import('@/types/events').Turn[] = [
      {
        id: 't-1',
        sessionId: 's-session-c',
        role: 'user',
        text: 'Hello',
        createdAt: '2026-01-01T00:00:00Z',
        status: 'complete',
      },
    ];

    // Dispatch session.history twice with the same turns
    emit({ type: 'session.history', sessionId: 's-session-c', turns });
    expect(result.current.state.turns).toHaveLength(1);

    emit({ type: 'session.history', sessionId: 's-session-c', turns });
    // Should still be 1 turn, not 2 (idempotent, no duplicate)
    expect(result.current.state.turns).toHaveLength(1);
    expect(result.current.state.turns[0].id).toBe('t-1');
  });
});

describe('ensureActiveSession', () => {
  it('mints an id only when there is no active session and no sessions', () => {
    const out = ensureActiveSession(BOOTSTRAP, () => 's-fixed');
    expect(out.activeSessionId).toBe('s-fixed');
  });

  it('leaves an already-active bootstrap untouched (idempotent, no double create)', () => {
    const b = { ...BOOTSTRAP, activeSessionId: 's-real' };
    expect(ensureActiveSession(b, () => 's-new')).toBe(b);
  });

  it('falls back to existing sessions instead of minting', () => {
    const b = {
      ...BOOTSTRAP,
      sessions: [{ id: 's-old', title: 'x', mode: 'chat', turnCount: 0 }],
      activeSessionId: null,
    };
    expect(ensureActiveSession(b, () => 's-new')).toBe(b);
  });

  it('mintSessionId returns an s-prefixed id', () => {
    expect(mintSessionId()).toMatch(/^s-/);
  });

  it('sends capabilities frame after auth.ok with builtins only', async () => {
    (window as Window & { electronAPI?: unknown }).electronAPI = {
      config: { wsUrl: null, isDev: true }, token: null,
      setConfig: vi.fn(), setToken: vi.fn(), clearToken: vi.fn(),
      executeTool: vi.fn(),
      getMcpTools: vi.fn().mockResolvedValue([]),
      getWorkspaceRoots: vi.fn().mockResolvedValue([]),
      addWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      removeWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      hasDefaultWorkspace: vi.fn().mockResolvedValue(true),
      getDefaultWorkspace: vi.fn().mockResolvedValue(null),
      setDefaultWorkspace: vi.fn().mockResolvedValue({ path: null }),
      searchWorkspace: vi.fn().mockResolvedValue({ entries: [] }),
    };

    try {
      renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
      act(() => mockWs.onopen?.());
      mockWs.send.mockClear();
      act(() => {
        mockWs.onmessage?.({
          data: JSON.stringify({ type: 'auth.ok' }),
        });
      });

      // Wait for async getMcpTools to complete
      await vi.waitFor(() => {
        const sent = mockWs.send.mock.calls.map((c) => JSON.parse(c[0] as string));
        expect(sent.some((f) => f.type === 'client.capabilities')).toBe(true);
      });

      const sent = mockWs.send.mock.calls.map((c) => JSON.parse(c[0] as string));
      const capFrame = sent.find((f) => f.type === 'client.capabilities');
      expect(capFrame).toEqual({
        type: 'client.capabilities',
        protocolVersion: 1,
        tools: [
          { name: 'read_file', source: 'builtin' },
          { name: 'write_file', source: 'builtin' },
          { name: 'list_dir', source: 'builtin' },
          { name: 'search_files', source: 'builtin' },
          { name: 'run_tests', source: 'builtin' },
        ],
      });
    } finally {
      delete (window as Window & { electronAPI?: unknown }).electronAPI;
    }
  });

  it('includes MCP tools in capabilities frame when getMcpTools returns tools', async () => {
    const mcpTool = {
      name: 'test_tool',
      source: 'mcp' as const,
      namespace: 'svc',
      schema: { type: 'object', properties: {} },
    };
    (window as Window & { electronAPI?: unknown }).electronAPI = {
      config: { wsUrl: null, isDev: true }, token: null,
      setConfig: vi.fn(), setToken: vi.fn(), clearToken: vi.fn(),
      executeTool: vi.fn(),
      getMcpTools: vi.fn().mockResolvedValue([mcpTool]),
      getWorkspaceRoots: vi.fn().mockResolvedValue([]),
      addWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      removeWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      hasDefaultWorkspace: vi.fn().mockResolvedValue(true),
      getDefaultWorkspace: vi.fn().mockResolvedValue(null),
      setDefaultWorkspace: vi.fn().mockResolvedValue({ path: null }),
      searchWorkspace: vi.fn().mockResolvedValue({ entries: [] }),
    };

    try {
      renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
      act(() => mockWs.onopen?.());
      mockWs.send.mockClear();
      act(() => {
        mockWs.onmessage?.({
          data: JSON.stringify({ type: 'auth.ok' }),
        });
      });

      await vi.waitFor(() => {
        const sent = mockWs.send.mock.calls.map((c) => JSON.parse(c[0] as string));
        const capFrame = sent.find((f) => f.type === 'client.capabilities');
        expect(capFrame?.tools).toHaveLength(6);
      });

      const sent = mockWs.send.mock.calls.map((c) => JSON.parse(c[0] as string));
      const capFrame = sent.find((f) => f.type === 'client.capabilities');
      expect(capFrame?.tools).toContainEqual(mcpTool);
      // Verify builtins are still there
      expect(capFrame?.tools.some((t: any) => t.name === 'read_file')).toBe(true);
    } finally {
      delete (window as Window & { electronAPI?: unknown }).electronAPI;
    }
  });

  it('sends capabilities frame with builtins only when getMcpTools is absent', async () => {
    // No getMcpTools in electronAPI (cast away the requirement for this test)
    (window as Window & { electronAPI?: unknown }).electronAPI = {
      config: { wsUrl: null, isDev: true }, token: null,
      setConfig: vi.fn(), setToken: vi.fn(), clearToken: vi.fn(),
      executeTool: vi.fn(),
      getMcpTools: undefined,
      getWorkspaceRoots: vi.fn().mockResolvedValue([]),
      addWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      removeWorkspaceRoot: vi.fn().mockResolvedValue({ roots: [] }),
      hasDefaultWorkspace: vi.fn().mockResolvedValue(true),
      getDefaultWorkspace: vi.fn().mockResolvedValue(null),
      setDefaultWorkspace: vi.fn().mockResolvedValue({ path: null }),
      searchWorkspace: vi.fn().mockResolvedValue({ entries: [] }),
    } as any;

    try {
      renderHook(() => useLabmateWS('ws://localhost:8787/ws', 'tok'));
      act(() => mockWs.onopen?.());
      mockWs.send.mockClear();
      act(() => {
        mockWs.onmessage?.({
          data: JSON.stringify({ type: 'auth.ok' }),
        });
      });

      await vi.waitFor(() => {
        const sent = mockWs.send.mock.calls.map((c) => JSON.parse(c[0] as string));
        expect(sent.some((f) => f.type === 'client.capabilities')).toBe(true);
      });

      const sent = mockWs.send.mock.calls.map((c) => JSON.parse(c[0] as string));
      const capFrame = sent.find((f) => f.type === 'client.capabilities');
      // Should still have 5 builtins, no errors thrown
      expect(capFrame?.tools).toHaveLength(5);
    } finally {
      delete (window as Window & { electronAPI?: unknown }).electronAPI;
    }
  });
});

describe('capabilitiesFrame', () => {
  it('returns a frame with type client.capabilities and five builtin tools', () => {
    const frame = capabilitiesFrame();
    expect(frame.type).toBe('client.capabilities');
    expect(frame.protocolVersion).toBe(1);
    expect(frame.tools).toHaveLength(5);
    expect(frame.tools.map((t) => t.name)).toEqual([
      'read_file',
      'write_file',
      'list_dir',
      'search_files',
      'run_tests',
    ]);
    expect(frame.tools.every((t) => t.source === 'builtin')).toBe(true);
  });
});
