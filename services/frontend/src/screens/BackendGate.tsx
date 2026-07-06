import React, { useEffect, useState } from 'react';
import { StartupScreen, type StartupStatus } from './StartupScreen';

export function BackendGate({ children }: { children: React.ReactNode }): React.ReactElement {
  const [status, setStatus] = useState<StartupStatus | null>(null);

  useEffect(() => {
    const api = window.electronAPI;
    if (!api?.onBackendStatus) return; // non-Electron: never gate
    api.onBackendStatus((s) => setStatus(s as StartupStatus));
    void api.getBackendStatus?.().then((s) => {
      if (s) setStatus(s as StartupStatus);
    });
  }, []);

  const handleRetry = () => { void window.electronAPI?.retryBackend?.(); };

  if (!status || status.phase === 'ready') return <>{children}</>;
  return <StartupScreen status={status} onRetry={handleRetry} />;
}
