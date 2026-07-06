import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

// The config helpers are small and file-based; test them via a temp userData dir.
// main.ts reads app.getPath('userData'); we mock electron's app to point there.
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'labmate-cfg-'));
vi.mock('electron', () => ({
  app: { getPath: () => tmp, on: vi.fn(), whenReady: () => Promise.resolve(), quit: vi.fn() },
  ipcMain: { on: vi.fn(), handle: vi.fn() },
  BrowserWindow: vi.fn(),
  contextBridge: { exposeInMainWorld: vi.fn() },
  ipcRenderer: { sendSync: vi.fn(), invoke: vi.fn() },
}));

describe('AppConfig gemmaBase', () => {
  beforeEach(() => {
    const f = path.join(tmp, 'config.json');
    if (fs.existsSync(f)) fs.rmSync(f);
  });

  it('round-trips wsUrl + gemmaBase through save/load', async () => {
    const mod = await import('./config-store'); // extracted pure helpers (see Step 3)
    mod.saveConfig({ wsUrl: 'ws://localhost:8799/ws', gemmaBase: 'https://pod-8000.proxy.runpod.net/v1' }, tmp);
    const cfg = mod.loadConfig(tmp);
    expect(cfg.wsUrl).toBe('ws://localhost:8799/ws');
    expect(cfg.gemmaBase).toBe('https://pod-8000.proxy.runpod.net/v1');
  });

  it('defaults gemmaBase to null when absent', async () => {
    const mod = await import('./config-store');
    const cfg = mod.loadConfig(tmp);
    expect(cfg.gemmaBase).toBeNull();
  });
});
