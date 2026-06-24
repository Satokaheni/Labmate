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
