import React from 'react';

export type StartupStatus =
  | { phase: 'starting'; step: string }
  | { phase: 'boot_failed'; logTail: string }
  | { phase: 'model_unreachable'; url: string }
  | { phase: 'ready' };

export function StartupScreen({
  status,
  onRetry,
}: {
  status: StartupStatus;
  onRetry?: () => void;
}): React.ReactElement | null {
  if (status.phase === 'ready') return null;

  if (status.phase === 'starting') {
    return (
      <div className="startup startup--busy">
        <div className="startup__spinner" aria-label="loading" />
        <p className="startup__step">Starting Labmate — {status.step}…</p>
      </div>
    );
  }

  if (status.phase === 'boot_failed') {
    return (
      <div className="startup startup--error">
        <h2>Labmate backend failed to start</h2>
        <pre className="startup__log">{status.logTail}</pre>
        <button onClick={onRetry}>Retry</button>
      </div>
    );
  }

  // model_unreachable — backend is up, the external model endpoint is not.
  return (
    <div className="startup startup--warn">
      <h2>Model endpoint unreachable</h2>
      <p>The model at <code>{status.url}</code> did not respond. Check the machine hosting it.</p>
      <button onClick={onRetry}>Retry</button>
    </div>
  );
}
