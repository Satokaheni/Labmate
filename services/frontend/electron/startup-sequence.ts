import type { AppConfig } from './config-store';
import type { BackendSupervisor } from './backend-supervisor';

/** Start the backend if an endpoint is configured; a no-endpoint config means a
 * fresh user who still needs onboarding, so we skip supervision and let the UI
 * render the onboarding screen. */
export async function startupSequence(
  supervisor: BackendSupervisor,
  cfg: AppConfig,
  localPort: number,
  repoRoot: string,
  logPath?: string,
): Promise<void> {
  if (!cfg.gemmaBase) return;
  await supervisor.start({ gemmaBase: cfg.gemmaBase, localPort, repoRoot, logPath });
}
