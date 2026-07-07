import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { BackendGate } from './BackendGate';

describe('BackendGate', () => {
  const originalApi = window.electronAPI;

  afterEach(() => {
    // Restore whatever electronAPI looked like before each test mutated it.
    (window as unknown as { electronAPI?: unknown }).electronAPI = originalApi;
    vi.restoreAllMocks();
  });

  it('renders children (not StartupScreen) when status is ready', async () => {
    (window as unknown as { electronAPI?: unknown }).electronAPI = {
      onBackendStatus: vi.fn(),
      getBackendStatus: vi.fn().mockResolvedValue({ phase: 'ready' }),
      retryBackend: vi.fn(),
    };

    render(
      <BackendGate>
        <div>APP</div>
      </BackendGate>,
    );

    expect(await screen.findByText('APP')).toBeInTheDocument();
    expect(screen.queryByText(/starting labmate/i)).not.toBeInTheDocument();
  });

  it('renders StartupScreen while starting (async pull)', async () => {
    (window as unknown as { electronAPI?: unknown }).electronAPI = {
      onBackendStatus: vi.fn(),
      getBackendStatus: vi.fn().mockResolvedValue({ phase: 'starting', step: 'waiting for backend health' }),
      retryBackend: vi.fn(),
    };

    render(
      <BackendGate>
        <div>APP</div>
      </BackendGate>,
    );

    expect(await screen.findByText(/waiting for backend health/i)).toBeInTheDocument();
    expect(screen.queryByText('APP')).not.toBeInTheDocument();
  });

  it('flips to the failure view on a pushed boot_failed status', async () => {
    let pushedCallback: ((s: unknown) => void) | null = null;
    (window as unknown as { electronAPI?: unknown }).electronAPI = {
      onBackendStatus: vi.fn((cb: (s: unknown) => void) => {
        pushedCallback = cb;
      }),
      getBackendStatus: vi.fn().mockResolvedValue(null),
      retryBackend: vi.fn(),
    };

    render(
      <BackendGate>
        <div>APP</div>
      </BackendGate>,
    );

    // Initial pull resolves to null -> children render until a push arrives.
    expect(await screen.findByText('APP')).toBeInTheDocument();

    await waitFor(() => expect(pushedCallback).not.toBeNull());
    act(() => {
      pushedCallback!({ phase: 'boot_failed', logTail: 'boom' });
    });

    expect(await screen.findByText(/boom/)).toBeInTheDocument();
    expect(screen.queryByText('APP')).not.toBeInTheDocument();
  });

  it('renders children when window.electronAPI is undefined (non-Electron context)', () => {
    (window as unknown as { electronAPI?: unknown }).electronAPI = undefined;

    render(
      <BackendGate>
        <div>APP</div>
      </BackendGate>,
    );

    expect(screen.getByText('APP')).toBeInTheDocument();
  });
});
