import { app, BrowserWindow, ipcMain } from 'electron';
import path from 'node:path';
import os from 'node:os';
import { executeTool, LOCAL_TOOL_NAMES, type LocalToolName } from './tool-executor';

const WORKSPACE = process.env.LABMATE_WORKSPACE
  ? path.resolve(process.env.LABMATE_WORKSPACE)
  : os.homedir();

const DEV_URL = 'http://localhost:8080';

function createWindow(): void {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  if (process.env.ELECTRON_DEV === '1') {
    void win.loadURL(DEV_URL);
    win.webContents.openDevTools({ mode: 'detach' });
  } else {
    void win.loadFile(path.join(__dirname, '..', 'dist', 'index.html'));
  }
}

ipcMain.handle(
  'labmate:tool-execute',
  async (_evt, payload: unknown) => {
    try {
      if (!payload || typeof payload !== 'object') {
        return { error: 'invalid payload' };
      }
      const { name, args } = payload as { name: string; args: Record<string, unknown> };
      if (!LOCAL_TOOL_NAMES.includes(name as LocalToolName)) {
        return { error: `unknown local tool: ${name}` };
      }
      const result = await executeTool(name as LocalToolName, args ?? {}, WORKSPACE);
      return { result };
    } catch (err) {
      return { error: err instanceof Error ? err.message : String(err) };
    }
  },
);

void app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
