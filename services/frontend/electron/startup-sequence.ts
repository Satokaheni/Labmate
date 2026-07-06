import type { BackendSupervisor } from './backend-supervisor';

/** Always start the local backend. The model URL is NOT injected here — the
 *  spawned start.sh --foreground sources local.env for GEMMA_BASE (single source
 *  of truth); we only pin LOCAL_PORT so the renderer and backend agree. */
export async function startupSequence(
  supervisor: BackendSupervisor,
  localPort: number,
  repoRoot: string,
  logPath?: string,
): Promise<void> {
  await supervisor.start({ gemmaBase: null, localPort, repoRoot, logPath });
}
