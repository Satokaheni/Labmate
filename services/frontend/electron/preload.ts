import { contextBridge, ipcRenderer } from 'electron';

export interface ExecuteToolResponse {
  result?: unknown;
  error?: string;
}

contextBridge.exposeInMainWorld('electronAPI', {
  executeTool: (name: string, args: Record<string, unknown>): Promise<ExecuteToolResponse> =>
    ipcRenderer.invoke('labmate:tool-execute', { name, args }),
});
