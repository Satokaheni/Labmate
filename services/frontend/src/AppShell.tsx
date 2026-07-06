import { useState } from 'react';
import type React from 'react';
import { Root } from './Root';
import { BackendGate } from './screens/BackendGate';
import { OnboardingScreen } from './screens/OnboardingScreen';

/** First-run gate: if the app has no model endpoint configured, show onboarding.
 *  On save, ask the main process to start the backend (retryBackend re-reads the
 *  freshly-saved config), then enter the app (BackendGate shows the boot status).
 *
 *  When window.electronAPI is undefined entirely (browser/dev-preview context,
 *  not a packaged Electron app), we treat that as "configured" so onboarding
 *  never blocks a non-Electron preview. A present `config` object with a null
 *  gemmaBase/wsUrl (the real first-run case) still gates on onboarding. */
export function AppShell(): React.ReactElement {
  const api = typeof window !== 'undefined' ? window.electronAPI : undefined;
  const cfg = api?.config;
  const isConfigured = !api || !!(cfg && cfg.gemmaBase && cfg.wsUrl);
  const [configured, setConfigured] = useState<boolean>(isConfigured);

  if (!configured) {
    return (
      <OnboardingScreen
        onSaved={() => {
          // config was just persisted by OnboardingScreen; start the backend now.
          void window.electronAPI?.retryBackend?.();
          setConfigured(true);
        }}
      />
    );
  }
  return (
    <BackendGate>
      <Root />
    </BackendGate>
  );
}
