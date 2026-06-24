import { useCallback, useEffect, useState } from 'react';
import { App } from '@/App';
import { BootScreen } from '@/screens/BootScreen';
import { LoginScreen, type LoginCredentials } from '@/screens/LoginScreen';
import { OnboardingScreen } from '@/screens/OnboardingScreen';
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
  // In a packaged build with no saved URL, show the onboarding screen.
  const [wsUrl, setWsUrl] = useState(WS_URL);
  const [token, setToken] = useState<string | null>(loadToken);
  const [loginError, setLoginError] = useState<string | undefined>();
  const [submitting, setSubmitting] = useState(false);
  const [reconnectKey, setReconnectKey] = useState(0);

  const { state, send, newSession, openSession, compact, cancel, clearAuthError } = useLabmateWS(wsUrl, token, reconnectKey);

  useEffect(() => {
    if (state.authError) {
      clearToken();
      setToken(null);
      setLoginError(state.authError);
      clearAuthError();
    }
  }, [state.authError, clearAuthError]);

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

  if (!wsUrl) {
    return <OnboardingScreen onSaved={(url) => setWsUrl(url)} />;
  }

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
      onStop={cancel}
      onOpenSession={openSession}
      onNewSession={newSession}
      onCompact={compact}
      compacting={state.compacting}
    />
  );
}
