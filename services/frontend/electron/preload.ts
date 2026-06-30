import { contextBridge, ipcRenderer } from 'electron';

export interface ExecuteToolResponse {
  result?: unknown;
  error?: string;
}

export interface AppConfig {
  wsUrl: string | null;
  isDev: boolean;
}

export interface WorkspaceEntry {
  absolute: string;
  insert: string;
  display: string;
  root: string;
  isDir: boolean;
}

export interface ToolDescriptor {
  name: string;
  source: 'builtin' | 'mcp' | 'skill';
  namespace?: string;
  schema?: unknown;
}

// Read config and token synchronously so they're available before the renderer module graph runs.
const config = ipcRenderer.sendSync('labmate:get-config') as AppConfig;
const token = ipcRenderer.sendSync('labmate:get-token') as string | null;

contextBridge.exposeInMainWorld('electronAPI', {
  config,
  token,
  setConfig: (wsUrl: string): Promise<void> =>
    ipcRenderer.invoke('labmate:set-config', wsUrl),
  setToken: (t: string, remember: boolean): Promise<void> =>
    ipcRenderer.invoke('labmate:set-token', { token: t, remember }),
  clearToken: (): Promise<void> =>
    ipcRenderer.invoke('labmate:clear-token'),
  executeTool: (
    name: string,
    args: Record<string, unknown>,
    sessionId?: string | null,
  ): Promise<ExecuteToolResponse> =>
    ipcRenderer.invoke('labmate:tool-execute', { name, args, sessionId }),
  getMcpTools: (): Promise<ToolDescriptor[]> =>
    ipcRenderer.invoke('labmate:mcp-tools'),

  // ── Workspace (multi-root per chat) ──
  getWorkspaceRoots: (sessionId: string | null): Promise<string[]> =>
    ipcRenderer.invoke('labmate:get-workspace-roots', { sessionId }),
  addWorkspaceRoot: (sessionId: string): Promise<{ roots: string[] }> =>
    ipcRenderer.invoke('labmate:add-workspace-root', { sessionId }),
  removeWorkspaceRoot: (sessionId: string, p: string): Promise<{ roots: string[] }> =>
    ipcRenderer.invoke('labmate:remove-workspace-root', { sessionId, path: p }),
  hasDefaultWorkspace: (): Promise<boolean> =>
    ipcRenderer.invoke('labmate:has-default-workspace'),
  getDefaultWorkspace: (): Promise<string | null> =>
    ipcRenderer.invoke('labmate:get-default-workspace'),
  setDefaultWorkspace: (): Promise<{ path: string | null }> =>
    ipcRenderer.invoke('labmate:set-default-workspace'),
  // Fuzzy file/dir search across the chat's roots, for @-mention autocomplete.
  searchWorkspace: (
    sessionId: string | null,
    query: string,
  ): Promise<{ entries: WorkspaceEntry[] }> =>
    ipcRenderer.invoke('labmate:search-workspace', { sessionId, query }),
});
