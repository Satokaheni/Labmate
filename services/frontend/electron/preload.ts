import { contextBridge, ipcRenderer } from 'electron';

export interface ExecuteToolResponse {
  result?: unknown;
  error?: string;
}

export interface AppConfig {
  wsUrl: string | null;
  isDev: boolean;
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
  executeTool: (name: string, args: Record<string, unknown>): Promise<ExecuteToolResponse> =>
    ipcRenderer.invoke('labmate:tool-execute', { name, args }),
});
