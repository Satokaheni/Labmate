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

  it('services tool.request via electronAPI and replies tool.result', async () => {
    const executeTool = vi.fn().mockResolvedValue({ result: { content: 'hi' } });
    // Set electronAPI directly to avoid replacing window (which breaks React DOM instanceof checks)
    (window as Window & { electronAPI?: unknown }).electronAPI = {
      config: { wsUrl: null, isDev: true }, token: null,
      setConfig: vi.fn(), setToken: vi.fn(), clearToken: vi.fn(), executeTool,
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
        expect(executeTool).toHaveBeenCalledWith('read_file', { path: 'a.txt' });
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
});
