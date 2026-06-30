import { useCallback, useEffect, useState } from 'react';

/**
 * Live view of a chat's workspace roots (multi-root). Keyed on the effective
 * session id (the same id used for sends + tool routing), so the roots shown
 * here are exactly the ones the agent's file tools resolve against.
 */
export function useWorkspace(sessionId: string) {
  const api = typeof window !== 'undefined' ? window.electronAPI : undefined;
  const [roots, setRoots] = useState<string[]>([]);

  const refresh = useCallback(() => {
    if (!api?.getWorkspaceRoots) return;
    api.getWorkspaceRoots(sessionId).then(setRoots).catch(() => { /* ignore */ });
  }, [api, sessionId]);

  useEffect(() => { refresh(); }, [refresh]);

  const addRoot = useCallback(async () => {
    if (!api?.addWorkspaceRoot) return;
    const { roots: next } = await api.addWorkspaceRoot(sessionId);
    setRoots(next);
  }, [api, sessionId]);

  const removeRoot = useCallback(async (p: string) => {
    if (!api?.removeWorkspaceRoot) return;
    const { roots: next } = await api.removeWorkspaceRoot(sessionId, p);
    setRoots(next);
  }, [api, sessionId]);

  return { roots, addRoot, removeRoot, refresh, available: !!api?.getWorkspaceRoots };
}
