/**
 * Electron preload API type definitions
 * Ensures renderer has access to native APIs via contextBridge
 */

export interface ExecuteToolResponse {
  result?: unknown;
  error?: string;
}

export interface ToolDescriptor {
  name: string;
  source: 'builtin' | 'mcp' | 'skill';
  namespace?: string;
  schema?: unknown;
}

export interface WorkspaceEntry {
  absolute: string;
  insert: string;
  display: string;
  root: string;
  isDir: boolean;
}

export type SupervisorStatus =
  | { phase: 'starting'; step: string }
  | { phase: 'ready' }
  | { phase: 'boot_failed'; logTail: string };

declare global {
  interface Window {
    electronAPI?: {
      config: { wsUrl: string | null; gemmaBase: string | null; isDev: boolean };
      token: string | null;
      setConfig(cfg: { wsUrl: string | null; gemmaBase: string | null }): Promise<void>;
      setToken(token: string, remember: boolean): Promise<void>;
      clearToken(): Promise<void>;
      executeTool(name: string, args: Record<string, unknown>, sessionId?: string | null): Promise<ExecuteToolResponse>;
      getMcpTools(): Promise<ToolDescriptor[]>;
      getSkillDescriptors(): Promise<ToolDescriptor[]>;
      getWorkspaceRoots(sessionId: string | null): Promise<string[]>;
      addWorkspaceRoot(sessionId: string): Promise<{ roots: string[] }>;
      removeWorkspaceRoot(sessionId: string, path: string): Promise<{ roots: string[] }>;
      hasDefaultWorkspace(): Promise<boolean>;
      getDefaultWorkspace(): Promise<string | null>;
      setDefaultWorkspace(): Promise<{ path: string | null }>;
      searchWorkspace(sessionId: string | null, query: string): Promise<{ entries: WorkspaceEntry[] }>;
      setActiveSession(sessionId: string | null): Promise<{ ok: boolean }>;
      onBackendStatus(cb: (s: SupervisorStatus) => void): void;
    };
  }
}

export {};
