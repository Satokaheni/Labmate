import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('electron', {
  executeTool: (request: any) => ipcRenderer.invoke('execute-tool', request),
});
