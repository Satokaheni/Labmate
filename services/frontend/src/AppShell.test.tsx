import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AppShell } from './AppShell';

// Root pulls in a real WebSocket hook; stub it so the "configured" path stays hermetic.
vi.mock('./Root', () => ({
  Root: () => <div data-testid="app-root">APP ROOT</div>,
}));

describe('AppShell', () => {
  const originalApi = window.electronAPI;

  afterEach(() => {
    (window as unknown as { electronAPI?: unknown }).electronAPI = originalApi;
    vi.restoreAllMocks();
  });

  it('renders OnboardingScreen when unconfigured (gemmaBase null)', () => {
    (window as unknown as { electronAPI?: unknown }).electronAPI = {
      config: { wsUrl: null, gemmaBase: null, isDev: true },
      setConfig: vi.fn().mockResolvedValue(undefined),
      retryBackend: vi.fn().mockResolvedValue({ phase: 'starting' }),
      onBackendStatus: vi.fn(),
      getBackendStatus: vi.fn().mockResolvedValue({ phase: 'ready' }),
    };

    render(<AppShell />);

    expect(screen.getByRole('heading', { name: /connect labmate/i })).toBeInTheDocument();
    expect(screen.queryByTestId('app-root')).not.toBeInTheDocument();
  });

  it('renders the app path (OnboardingScreen absent, BackendGate mounted) when configured', async () => {
    (window as unknown as { electronAPI?: unknown }).electronAPI = {
      config: { wsUrl: 'ws://localhost:8787/ws', gemmaBase: 'http://localhost:8000/v1', isDev: true },
      setConfig: vi.fn().mockResolvedValue(undefined),
      retryBackend: vi.fn().mockResolvedValue({ phase: 'ready' }),
      onBackendStatus: vi.fn(),
      getBackendStatus: vi.fn().mockResolvedValue({ phase: 'ready' }),
    };

    render(<AppShell />);

    expect(await screen.findByTestId('app-root')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /connect labmate/i })).not.toBeInTheDocument();
  });

  it('on onSaved: calls retryBackend and transitions away from onboarding into the app', async () => {
    const retryBackend = vi.fn().mockResolvedValue({ phase: 'ready' });
    (window as unknown as { electronAPI?: unknown }).electronAPI = {
      config: { wsUrl: null, gemmaBase: null, isDev: true },
      setConfig: vi.fn().mockResolvedValue(undefined),
      retryBackend,
      onBackendStatus: vi.fn(),
      getBackendStatus: vi.fn().mockResolvedValue({ phase: 'ready' }),
    };

    const user = userEvent.setup();
    render(<AppShell />);

    expect(screen.getByRole('heading', { name: /connect labmate/i })).toBeInTheDocument();

    // Drive OnboardingScreen's real save flow: fill both inputs, click Save.
    await user.type(screen.getByLabelText(/gateway websocket url/i), 'ws://localhost:8787/ws');
    await user.type(screen.getByLabelText(/model endpoint/i), 'http://localhost:8000/v1');
    const saveButton = screen.getByRole('button', { name: 'Connect' });
    await user.click(saveButton);

    await waitFor(() => expect(retryBackend).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: /connect labmate/i })).not.toBeInTheDocument(),
    );
    expect(await screen.findByTestId('app-root')).toBeInTheDocument();
  });

  it('does not crash when window.electronAPI is undefined, and renders the app path', async () => {
    (window as unknown as { electronAPI?: unknown }).electronAPI = undefined;

    render(<AppShell />);

    expect(await screen.findByTestId('app-root')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /connect labmate/i })).not.toBeInTheDocument();
  });
});
