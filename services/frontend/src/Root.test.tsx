import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Root } from './Root';

// Mock the useLabmateWS hook
vi.mock('@/hooks/useLabmateWS', () => ({
  useLabmateWS: () => ({
    state: { phase: 'idle' as const },
    send: vi.fn(),
    newSession: vi.fn(),
    openSession: vi.fn(),
    setDebug: vi.fn(),
  }),
}));

// Mock the screens
vi.mock('@/screens/BootScreen', () => ({
  BootScreen: () => <div data-testid="boot-screen">Boot Screen</div>,
}));

vi.mock('@/screens/LoginScreen', () => ({
  LoginScreen: ({ onSubmit }: { onSubmit: (creds: any) => void }) => (
    <div data-testid="login-screen">
      <button onClick={() => onSubmit({ email: 'test@test.com', password: 'pwd', remember: false })}>
        Login
      </button>
    </div>
  ),
}));

// Mock the layout and components
vi.mock('@/layouts/ChatLayout', () => ({
  ChatLayout: () => <div data-testid="chat-layout">Chat Layout</div>,
}));

describe('Root', () => {
  beforeEach(() => {
    // Mock window.electronAPI
    (window as any).electronAPI = {
      token: null,
      setToken: vi.fn(),
    };
  });

  afterEach(() => {
    delete (window as any).electronAPI;
  });

  it('renders without throwing', () => {
    expect(() => render(<Root />)).not.toThrow();
  });

  it('shows LoginScreen when idle', () => {
    render(<Root />);
    expect(screen.getByTestId('login-screen')).toBeInTheDocument();
  });
});
