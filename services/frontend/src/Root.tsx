import React, { useState, useCallback } from 'react';
import { useLabmateWS } from '@/hooks/useLabmateWS';
import { useMinimumElapsed } from '@/hooks/useMinimumElapsed';
import { BootScreen } from '@/screens/BootScreen';
import { LoginScreen } from '@/screens/LoginScreen';
import { ChatScreen } from '@/components/chat/ChatScreen';
import { WS_URL, API_URL } from '@/config';
import type { SubsystemId } from '@/types/events';

/** Minimum time the boot/loading screen stays visible, so a fast boot doesn't
 *  flash the loading animation. */
const MIN_BOOT_MS = 3000;

export function Root(): React.ReactNode {
  // Resolve token from Electron config (if available) or null
  const initialToken = window.electronAPI?.token ?? null;
  const [token, setToken] = useState<string | null>(initialToken);
  const [loginError, setLoginError] = useState<string | undefined>(undefined);
  const [loggingIn, setLoggingIn] = useState(false);

  // Call the WebSocket hook
  const { state, send, newSession, newChat, openSession, setDebug, renameSession, deleteSession } = useLabmateWS(WS_URL, token);

  // Minimum boot/loading display time: if the connect→ready sequence finishes in
  // under MIN_BOOT_MS the loading animation flashes by and looks bad, so hold the
  // boot screen until at least MIN_BOOT_MS has elapsed since loading began.
  const isBooting =
    state.phase === 'connecting' ||
    state.phase === 'authenticating' ||
    state.phase === 'booting';
  const minBootElapsed = useMinimumElapsed(isBooting, MIN_BOOT_MS);

  // Handle login submission
  const handleLoginSubmit = useCallback(
    async (creds: { email: string; password: string; remember: boolean }) => {
      setLoginError(undefined);
      setLoggingIn(true);
      try {
        const res = await fetch(`${API_URL}/auth/login`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: creds.email, password: creds.password }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setLoginError((body as { detail?: string }).detail ?? 'invalid_credentials');
          return;
        }
        const { token: newToken } = (await res.json()) as { token: string };
        setToken(newToken);
        if (window.electronAPI?.setToken) {
          await window.electronAPI.setToken(newToken, creds.remember);
        }
      } catch {
        setLoginError('network_error');
      } finally {
        setLoggingIn(false);
      }
    },
    []
  );

  // Handle boot screen retry (no-op for now; actual retry logic could be added)
  const handleBootRetry = useCallback((_id: SubsystemId) => {
    // TODO: implement retry logic
  }, []);

  // Render based on phase
  switch (state.phase) {
    case 'idle': {
      return <LoginScreen onSubmit={handleLoginSubmit} submitting={loggingIn} error={loginError} />;
    }

    case 'error': {
      return <LoginScreen onSubmit={handleLoginSubmit} submitting={loggingIn} error={loginError ?? state.authError} />;
    }

    case 'connecting':
    case 'authenticating': {
      return <BootScreen subsystems={[]} onRetry={handleBootRetry} />;
    }

    case 'booting': {
      return <BootScreen subsystems={state.subsystems ?? []} onRetry={handleBootRetry} />;
    }

    case 'ready': {
      // Honor the minimum boot display time: if ready arrived before MIN_BOOT_MS,
      // keep showing the (completed) boot screen until the minimum has elapsed.
      if (!minBootElapsed) {
        return <BootScreen subsystems={state.subsystems ?? []} onRetry={handleBootRetry} />;
      }

      if (!state.agentStatus) {
        return <div className="flex h-screen items-center justify-center text-mono">Loading…</div>;
      }

      return (
        <ChatScreen
          state={state}
          send={send}
          newSession={newSession}
          newChat={newChat}
          openSession={openSession}
          setDebug={setDebug}
          renameSession={renameSession}
          deleteSession={deleteSession}
        />
      );
    }

    default: {
      // Fallback for unknown phases
      return <div className="flex items-center justify-center h-screen">Unknown phase: {state.phase}</div>;
    }
  }
}
