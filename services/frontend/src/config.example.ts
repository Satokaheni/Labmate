// config.example.ts — template copied to config.ts on setup (predev/prebuild/CI).
// Edit config.ts, not this file, for local overrides — or better, set VITE_WS_URL
// in services/frontend/.env (see .env.example) so config.ts never needs editing.
import { DEFAULT_WS_URL } from '@/gateway-defaults';

export interface ToolDescriptor {
  name: string;
  source: 'builtin' | 'mcp' | 'skill';
  namespace?: string;
  schema?: unknown;
}

declare global {
  interface Window {
    electronAPI?: {
      config: { wsUrl: string | null; gemmaBase: string | null; isDev: boolean };
      token: string | null;
      setConfig: (cfg: { wsUrl: string | null; gemmaBase: string | null }) => Promise<void>;
      setToken: (token: string, remember: boolean) => Promise<void>;
      clearToken: () => Promise<void>;
      executeTool: (
        name: string,
        args: Record<string, unknown>,
        sessionId?: string | null,
      ) => Promise<{ result?: unknown; error?: string }>;
      getMcpTools: () => Promise<ToolDescriptor[]>;
      getWorkspaceRoots: (sessionId: string | null) => Promise<string[]>;
      addWorkspaceRoot: (sessionId: string) => Promise<{ roots: string[] }>;
      removeWorkspaceRoot: (sessionId: string, path: string) => Promise<{ roots: string[] }>;
      hasDefaultWorkspace: () => Promise<boolean>;
      getDefaultWorkspace: () => Promise<string | null>;
      setDefaultWorkspace: () => Promise<{ path: string | null }>;
      searchWorkspace: (
        sessionId: string | null,
        query: string,
      ) => Promise<{ entries: WorkspaceMentionEntry[] }>;
      onBackendStatus?: (cb: (s: { phase: string }) => void) => void;
      getBackendStatus?: () => Promise<{ phase: string } | null>;
      retryBackend?: () => Promise<{ phase: string } | null>;
    };
  }
}

/** A file/dir match from the workspace @-mention search (mirrors fs-search.WorkspaceEntry). */
export interface WorkspaceMentionEntry {
  absolute: string;
  insert: string;
  display: string;
  root: string;
  isDir: boolean;
}

const ec = window.electronAPI?.config;

// Prefer the onboarded/runtime gateway URL (userData/config.json) in BOTH dev and
// packaged builds, so the renderer connects to the same gateway the backend was
// spawned on (the port the supervisor derives from cfg.wsUrl). In dev, VITE_WS_URL
// remains the PRE-onboarding fallback (before cfg.wsUrl is set); this avoids the
// footgun where .env's port disagrees with the onboarded port (green backend behind
// a renderer dialing a dead port).
export const WS_URL: string = ec?.isDev
  ? ec?.wsUrl ?? (import.meta.env.VITE_WS_URL as string | undefined) ?? DEFAULT_WS_URL
  : ec?.wsUrl ?? DEFAULT_WS_URL;

export const API_URL: string = WS_URL
  .replace(/^wss:\/\//, 'https://')
  .replace(/^ws:\/\//, 'http://')
  .replace(/\/ws$/, '');
