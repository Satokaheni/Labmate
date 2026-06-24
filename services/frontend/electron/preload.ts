import { contextBridge, ipcRenderer } from 'electron';

export interface ExecuteToolResponse {
  result?: unknown;
  error?: string;
}

export interface AppConfig {
  wsUrl: string | null;
  isDev: boolean;
}

// Read config synchronously so it's available before the renderer module graph runs.
const config = ipcRenderer.sendSync('labmate:get-config') as AppConfig;

contextBridge.exposeInMainWorld('electronAPI', {
  config,
  setConfig: (wsUrl: string): Promise<void> =>
    ipcRenderer.invoke('labmate:set-config', wsUrl),
  executeTool: (name: string, args: Record<string, unknown>): Promise<ExecuteToolResponse> =>
    ipcRenderer.invoke('labmate:tool-execute', { name, args }),
});
