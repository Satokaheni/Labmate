import { useState, useCallback } from 'react';
import { useLabmateWS } from '@/hooks/useLabmateWS';
import { ChatLayout } from '@/layouts/ChatLayout';
import { BootScreen } from '@/screens/BootScreen';
import { LoginScreen } from '@/screens/LoginScreen';
import { Composer } from '@/components/Composer';
import { ContextBar } from '@/components/ContextBar';
import { SystemFooter } from '@/components/SystemFooter';
import { Turn } from '@/components/Turn';
import { FilePreview } from '@/components/FilePreview';
import { WS_URL } from '@/config';
import type { Artifact, SubsystemId } from '@/types/events';

export function Root(): JSX.Element {
  // Resolve token from Electron config (if available) or null
  const initialToken = window.electronAPI?.token ?? null;
  const [token, setToken] = useState<string | null>(initialToken);
  const [previewArtifact, setPreviewArtifact] = useState<Artifact | null>(null);

  // Call the WebSocket hook
  const { state, send, newSession, openSession, setDebug } = useLabmateWS(WS_URL, token);

  // Handle login submission
  const handleLoginSubmit = useCallback(
    async (creds: { email: string; password: string; remember: boolean }) => {
      try {
        // In a real app, you'd authenticate here and get a token from the backend.
        // For now, assume the email:password is the token (or use a placeholder).
        // This should be replaced with actual backend auth.
        const newToken = `${creds.email}:${creds.password}`;
        setToken(newToken);
        if (window.electronAPI?.setToken) {
          await window.electronAPI.setToken(newToken, creds.remember);
        }
      } catch (err) {
        console.error('Login failed:', err);
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
      return <LoginScreen onSubmit={handleLoginSubmit} />;
    }

    case 'error': {
      return <LoginScreen onSubmit={handleLoginSubmit} error={state.authError} />;
    }

    case 'connecting':
    case 'authenticating': {
      return <BootScreen subsystems={[]} onRetry={handleBootRetry} />;
    }

    case 'booting': {
      return <BootScreen subsystems={state.subsystems} onRetry={handleBootRetry} />;
    }

    case 'ready': {
      return (
        <ChatLayout
          topBar={
            <div className="flex items-center justify-between w-full">
              <div className="text-sm font-medium text-primary">Labmate Chat</div>
              <SystemFooter status={state.agentStatus} />
            </div>
          }
          left={
            <div className="flex flex-col gap-3 p-3">
              <button
                onClick={() => newSession?.('chat')}
                className="rounded bg-secondary px-3 py-2 text-sm font-medium text-white hover:bg-secondary-hover"
              >
                + New Chat
              </button>
              <div className="flex flex-col gap-1 overflow-y-auto">
                {state.sessions.map((session) => (
                  <button
                    key={session.id}
                    onClick={() => openSession?.(session.id)}
                    className="truncate rounded px-2 py-1 text-left text-sm text-secondary hover:bg-rail-hover"
                    title={session.title}
                  >
                    {session.title || `Session ${session.id.slice(0, 6)}`}
                  </button>
                ))}
              </div>
            </div>
          }
          center={
            <div className="flex flex-col">
              <div className="flex-1 overflow-y-auto p-4">
                <div className="flex flex-col gap-4">
                  {state.turns.map((turn) => (
                    <Turn
                      key={turn.id}
                      turn={turn}
                      onPreviewArtifact={(artifact: Artifact) => setPreviewArtifact(artifact)}
                    />
                  ))}
                </div>
              </div>

              <div className="border-t border-border-1 px-4 py-2">
                <Composer
                  onSend={(text: string) => {
                    const sessionId = state.sessions[0]?.id ?? 'default';
                    send(text, sessionId);
                  }}
                  onStop={() => {
                    // TODO: implement stop signal via setDebug or similar
                    setDebug(state.sessions[0]?.id ?? 'default', false);
                  }}
                  streaming={false}
                />
              </div>

              {state.contextWindow && (
                <div className="border-t border-border-1">
                  <ContextBar window={state.contextWindow} />
                </div>
              )}
            </div>
          }
          right={
            previewArtifact ? (
              <FilePreview artifact={previewArtifact} />
            ) : (
              <div className="flex items-center justify-center p-4 text-sm text-mono">
                Select an artifact to preview
              </div>
            )
          }
        />
      );
    }

    default: {
      // Exhaustiveness check
      const _unreachable: never = state;
      return _unreachable;
    }
  }
}
