import { app, BrowserWindow, ipcMain, Menu, Tray, nativeImage, safeStorage } from 'electron';
import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs';
import { deflateSync } from 'node:zlib';
import { executeTool, LOCAL_TOOL_NAMES, type LocalToolName } from './tool-executor';

const WORKSPACE = process.env.LABMATE_WORKSPACE
  ? path.resolve(process.env.LABMATE_WORKSPACE)
  : os.homedir();

const DEV_URL = 'http://localhost:8080';
const IS_DEV = process.env.ELECTRON_DEV === '1';

// ── Auth token (encrypted via OS keychain; session-only when remember=false) ──

let _sessionToken: string | null = null;

function tokenFile() { return path.join(app.getPath('userData'), 'token.enc'); }

// ── App config (runtime WS URL) ───────────────────────────────────────────────

interface AppConfig { wsUrl: string | null; isDev: boolean; }

function configFile() { return path.join(app.getPath('userData'), 'config.json'); }

function loadConfig(): AppConfig {
  if (IS_DEV) return { wsUrl: null, isDev: true };
  try { return { wsUrl: (JSON.parse(fs.readFileSync(configFile(), 'utf8')) as { wsUrl: string }).wsUrl, isDev: false }; }
  catch { return { wsUrl: null, isDev: false }; }
}

function saveConfig(wsUrl: string): void {
  fs.writeFileSync(configFile(), JSON.stringify({ wsUrl }), 'utf8');
}

// ── Window state persistence ──────────────────────────────────────────────────

interface WindowState { x?: number; y?: number; width: number; height: number; }

function stateFile() { return path.join(app.getPath('userData'), 'window-state.json'); }

function loadWindowState(): WindowState {
  try { return JSON.parse(fs.readFileSync(stateFile(), 'utf8')) as WindowState; }
  catch { return { width: 1280, height: 800 }; }
}

function saveWindowState(win: BrowserWindow): void {
  if (win.isMaximized() || win.isFullScreen()) return;
  try { fs.writeFileSync(stateFile(), JSON.stringify(win.getBounds()), 'utf8'); }
  catch { /* ignore */ }
}

// ── Tray icon (generated inline PNG — no asset file needed) ───────────────────

function buildPng(r: number, g: number, b: number, size = 16): Buffer {
  const row = Buffer.alloc(1 + size * 3);
  row[0] = 0; // filter byte: None
  for (let i = 0; i < size; i++) { row[1 + i * 3] = r; row[2 + i * 3] = g; row[3 + i * 3] = b; }
  const raw = Buffer.concat(Array.from({ length: size }, () => row));
  const compressed = deflateSync(raw);

  // CRC-32 table
  const table = Uint32Array.from({ length: 256 }, (_, i) => {
    let c = i;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    return c;
  });
  const crc32 = (buf: Buffer): number => {
    let c = 0xFFFFFFFF;
    for (const byte of buf) c = table[(c ^ byte) & 0xFF]! ^ (c >>> 8);
    return (~c) >>> 0;
  };
  const chunk = (type: string, data: Buffer): Buffer => {
    const t = Buffer.from(type, 'ascii');
    const len = Buffer.allocUnsafe(4); len.writeUInt32BE(data.length);
    const crcBuf = Buffer.allocUnsafe(4); crcBuf.writeUInt32BE(crc32(Buffer.concat([t, data])));
    return Buffer.concat([len, t, data, crcBuf]);
  };

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0); ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; ihdr[9] = 2; // bit depth 8, color type RGB

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
    chunk('IHDR', ihdr),
    chunk('IDAT', compressed),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// ── Application menu ──────────────────────────────────────────────────────────

function buildMenu(): Menu {
  return Menu.buildFromTemplate([
    ...(process.platform === 'darwin' ? [{
      label: app.name,
      submenu: [
        { role: 'about' as const },
        { type: 'separator' as const },
        { role: 'services' as const },
        { type: 'separator' as const },
        { role: 'hide' as const },
        { role: 'hideOthers' as const },
        { role: 'unhide' as const },
        { type: 'separator' as const },
        { role: 'quit' as const },
      ],
    }] : []),
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' as const },
        { role: 'redo' as const },
        { type: 'separator' as const },
        { role: 'cut' as const },
        { role: 'copy' as const },
        { role: 'paste' as const },
        { role: 'selectAll' as const },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' as const },
        { role: 'toggleDevTools' as const },
        { type: 'separator' as const },
        { role: 'resetZoom' as const },
        { role: 'zoomIn' as const },
        { role: 'zoomOut' as const },
        { type: 'separator' as const },
        { role: 'togglefullscreen' as const },
      ],
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' as const },
        { role: 'zoom' as const },
        ...(process.platform === 'darwin'
          ? [{ type: 'separator' as const }, { role: 'front' as const }]
          : [{ role: 'close' as const }]),
      ],
    },
  ]);
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

let tray: Tray | null = null;
let isQuitting = false;

app.on('before-quit', () => { isQuitting = true; });

function createWindow(): BrowserWindow {
  const state = loadWindowState();
  const win = new BrowserWindow({
    ...state,
    title: 'Labmate',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  win.on('resize', () => saveWindowState(win));
  win.on('move', () => saveWindowState(win));
  win.on('close', (e) => {
    saveWindowState(win);
    // On macOS, hide instead of quit so the tray keeps the app alive.
    if (process.platform === 'darwin' && !isQuitting) {
      e.preventDefault();
      win.hide();
    }
  });

  if (process.env.ELECTRON_DEV === '1') {
    void win.loadURL(DEV_URL);
  } else {
    void win.loadFile(path.join(__dirname, '..', 'dist', 'index.html'));
  }

  Menu.setApplicationMenu(buildMenu());

  if (tray === null) {
    const icon = nativeImage.createFromBuffer(buildPng(167, 139, 250, 16)); // #a78bfa
    tray = new Tray(icon);
    tray.setToolTip('Labmate');
    tray.on('click', () => {
      if (win.isVisible()) win.focus();
      else win.show();
    });
  }

  return win;
}

ipcMain.on('labmate:get-config', (event) => { event.returnValue = loadConfig(); });

ipcMain.on('labmate:get-token', (event) => {
  if (_sessionToken !== null) { event.returnValue = _sessionToken; return; }
  try {
    if (safeStorage.isEncryptionAvailable()) {
      event.returnValue = safeStorage.decryptString(fs.readFileSync(tokenFile()));
    } else {
      event.returnValue = null;
    }
  } catch { event.returnValue = null; }
});

ipcMain.handle('labmate:set-token', (_evt, { token, remember }: { token: string; remember: boolean }) => {
  if (remember) {
    if (safeStorage.isEncryptionAvailable()) {
      fs.writeFileSync(tokenFile(), safeStorage.encryptString(token));
    }
    _sessionToken = null;
  } else {
    _sessionToken = token;
    try { fs.unlinkSync(tokenFile()); } catch { /* no persisted token to remove */ }
  }
});

ipcMain.handle('labmate:clear-token', () => {
  _sessionToken = null;
  try { fs.unlinkSync(tokenFile()); } catch { /* ignore */ }
});

ipcMain.handle('labmate:set-config', (_evt, wsUrl: string) => { saveConfig(wsUrl); });

ipcMain.handle(
  'labmate:tool-execute',
  async (_evt, payload: unknown) => {
    try {
      if (!payload || typeof payload !== 'object') return { error: 'invalid payload' };
      const { name, args } = payload as { name: string; args: Record<string, unknown> };
      if (!LOCAL_TOOL_NAMES.includes(name as LocalToolName)) {
        return { error: `unknown local tool: ${name}` };
      }
      return { result: await executeTool(name as LocalToolName, args ?? {}, WORKSPACE) };
    } catch (err) {
      return { error: err instanceof Error ? err.message : String(err) };
    }
  },
);

void app.whenReady().then(() => {
  createWindow();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  const wins = BrowserWindow.getAllWindows();
  if (wins.length === 0) createWindow();
  else wins[0]!.show();
});
