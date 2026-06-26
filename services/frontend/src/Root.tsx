import { useCallback, useReducer, useRef, useState } from 'react';
import { App } from '@/App';
import { BootScreen } from '@/screens/BootScreen';
import { LoginScreen, type LoginCredentials } from '@/screens/LoginScreen';
import { OnboardingScreen } from '@/screens/OnboardingScreen';
import { useLabmateWS } from '@/hooks/useLabmateWS';
import { API_URL, WS_URL } from '@/config';
import type { SubsystemId } from '@/types/events';

function loadToken(): string | null {
  return window.electronAPI?.token ?? null;
}

function storeToken(token: string, remember: boolean): void {
  void window.electronAPI?.setToken(token, remember);
}

function clearToken(): void {
  void window.electronAPI?.clearToken();
}

interface AuthState {
  token: string | null;
  loginError: string | undefined;
  submitting: boolean;
  reconnectKey: number;
}

type AuthAction =
  | { type: 'LOGIN_START' }
  | { type: 'LOGIN_SUCCESS'; token: string }
  | { type: 'LOGIN_FAILURE'; error: string }
  | { type: 'LOGOUT' }
  | { type: 'RECONNECT' };

function authReducer(state: AuthState, action: AuthAction): AuthState {
  switch (action.type) {
    case 'LOGIN_START':
      return { ...state, submitting: true, loginError: undefined };
    case 'LOGIN_SUCCESS':
      return { ...state, submitting: false, token: action.token, loginError: undefined };
    case 'LOGIN_FAILURE':
      return { ...state, submitting: false, loginError: action.error };
    case 'LOGOUT':
      return { ...state, token: null };
    case 'RECONNECT':
      return { ...state, reconnectKey: state.reconnectKey + 1 };
  }
}

export function Root() {
  const [wsUrl, setWsUrl] = useState(WS_URL);
  const [auth, dispatch] = useReducer(authReducer, {
    token: loadToken(),
    loginError: undefined,
    submitting: false,
    reconnectKey: 0,
  });

  const clearAuthErrorRef = useRef<() => void>(() => {});

  const handleAuthError = useCallback((reason: string) => {
    void reason;
    clearToken();
    dispatch({ type: 'LOGOUT' });
    clearAuthErrorRef.current();
  }, []);

  const { state, send, newSession, openSession, compact, cancel, deleteSession, clearAuthError } = useLabmateWS(
    wsUrl, auth.token, auth.reconnectKey, { onAuthError: handleAuthError },
  );
  clearAuthErrorRef.current = clearAuthError;

  const handleLogin = useCallback(async (creds: LoginCredentials) => {
    dispatch({ type: 'LOGIN_START' });
    try {
      const res = await fetch(`${API_URL}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: creds.email, password: creds.password }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        dispatch({ type: 'LOGIN_FAILURE', error: body.detail ?? 'invalid_credentials' });
        return;
      }
      const { token: tok } = (await res.json()) as { token: string };
      storeToken(tok, creds.remember);
      dispatch({ type: 'LOGIN_SUCCESS', token: tok });
    } catch {
      dispatch({ type: 'LOGIN_FAILURE', error: 'Cannot reach the server. Check your connection.' });
    }
  }, []);

  const handleRetry = useCallback((_id: SubsystemId) => {
    dispatch({ type: 'RECONNECT' });
  }, []);

  const handleSaved = useCallback((url: string) => setWsUrl(url), []);

  // Derive display error from WS auth error or form submission error (no state copy needed).
  const displayError = state.authError ?? auth.loginError;

  if (!wsUrl) {
    return <OnboardingScreen onSaved={handleSaved} />;
  }

  if (!auth.token) {
    return <LoginScreen onSubmit={handleLogin} submitting={auth.submitting} error={displayError} />;
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
      onDeleteSession={deleteSession}
      compacting={state.compacting}
    />
  );
}
