import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
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
  compacting: false,
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

function makeElectronAPI(token: string | null = null) {
  return {
    config: { wsUrl: 'ws://localhost:8787/ws', isDev: true },
    token,
    setToken: vi.fn().mockResolvedValue(undefined),
    clearToken: vi.fn().mockResolvedValue(undefined),
    setConfig: vi.fn().mockResolvedValue(undefined),
    executeTool: vi.fn(),
  };
}

beforeEach(() => {
  vi.mocked(useLabmateWS).mockReturnValue({
    state: IDLE_STATE, send: vi.fn(), newSession: vi.fn(), openSession: vi.fn(), setDebug: vi.fn(), compact: vi.fn(), cancel: vi.fn(), deleteSession: vi.fn(), clearAuthError: vi.fn(),
  });
  vi.stubGlobal('electronAPI', makeElectronAPI());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Root', () => {
  it('shows LoginScreen when no token is stored', () => {
    render(<Root />);
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
  });

  it('shows BootScreen when token exists but phase is booting', () => {
    vi.stubGlobal('electronAPI', makeElectronAPI('tok'));
    vi.mocked(useLabmateWS).mockReturnValue({
      state: { ...IDLE_STATE, phase: 'booting', subsystems: [] },
      send: vi.fn(), newSession: vi.fn(), openSession: vi.fn(), setDebug: vi.fn(), compact: vi.fn(), cancel: vi.fn(), deleteSession: vi.fn(), clearAuthError: vi.fn(),
    });
    render(<Root />);
    expect(screen.getByTestId('boot-progress')).toBeInTheDocument();
  });

  it('shows the chat layout when phase is ready', () => {
    vi.stubGlobal('electronAPI', makeElectronAPI('tok'));
    vi.mocked(useLabmateWS).mockReturnValue({
      state: { ...IDLE_STATE, phase: 'ready', agentStatus: AGENT_STATUS, context: CONTEXT },
      send: vi.fn(), newSession: vi.fn(), openSession: vi.fn(), setDebug: vi.fn(), compact: vi.fn(), cancel: vi.fn(), deleteSession: vi.fn(), clearAuthError: vi.fn(),
    });
    render(<Root />);
    expect(screen.getByTestId('layout-topbar')).toBeInTheDocument();
  });

  it('POSTs /auth/login and stores JWT via safeStorage when remember is false', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ token: 'jwt123' }),
    });
    render(<Root />);
    await userEvent.type(screen.getByLabelText('Email'), 'a@b.com');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    await waitFor(() => expect(window.electronAPI!.setToken).toHaveBeenCalledWith('jwt123', false));
  });

  it('stores JWT via safeStorage when "Keep me signed in" is checked', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ token: 'jwt456' }),
    });
    render(<Root />);
    await userEvent.type(screen.getByLabelText('Email'), 'a@b.com');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByLabelText(/keep me signed in/i));
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    await waitFor(() => expect(window.electronAPI!.setToken).toHaveBeenCalledWith('jwt456', true));
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
    vi.stubGlobal('electronAPI', makeElectronAPI('stale-tok'));
    let capturedOnAuthError: ((reason: string) => void) | undefined;
    vi.mocked(useLabmateWS).mockImplementation((_url, _token, _key, options) => {
      capturedOnAuthError = options?.onAuthError;
      return {
        state: { ...IDLE_STATE, phase: 'error', authError: 'expired' },
        send: vi.fn(), newSession: vi.fn(), openSession: vi.fn(), setDebug: vi.fn(), compact: vi.fn(), cancel: vi.fn(), deleteSession: vi.fn(), clearAuthError: vi.fn(),
      };
    });
    render(<Root />);
    act(() => { capturedOnAuthError?.('expired'); });
    await waitFor(() => expect(screen.getByLabelText('Email')).toBeInTheDocument());
    expect(window.electronAPI!.clearToken).toHaveBeenCalled();
  });
});
