import { app, BrowserWindow, dialog, ipcMain, Menu, Tray, nativeImage, safeStorage } from 'electron';
import path from 'node:path';
import fs from 'node:fs';
import { deflateSync } from 'node:zlib';
import { executeTool, LOCAL_TOOL_NAMES, type LocalToolName } from './tool-executor.js';
import { WorkspaceStore } from './workspace.js';
import { searchWorkspace } from './fs-search.js';
import { McpHostManager } from './mcp-registry.js';
import { resolveMcpPathArgs, injectCodegraphProjectPath } from './mcp-path-rooting.js';
import { skillDescriptors } from './skill-discovery.js';
import { userSkillsDir, ensureUserSkillsDir } from './labmate-home.js';
import { AppConfig, loadConfig as loadConfigAt, saveConfig as saveConfigAt } from './config-store';
import { BackendSupervisor, type SupervisorStatus } from './backend-supervisor.js';
import { startupSequence } from './startup-sequence.js';
import { readLocalEnv, type LocalEnv } from './local-env.js';

const DEV_URL = 'http://localhost:8080';
const IS_DEV = process.env.ELECTRON_DEV === '1';
/** Default UI scale (1.0 = 100%). Lower renders the whole app smaller. */
const UI_ZOOM = 0.75;

// ── Auth token (encrypted via OS keychain; session-only when remember=false) ──

let _sessionToken: string | null = null;

function tokenFile() { return path.join(app.getPath('userData'), 'token.enc'); }

// ── App config (runtime WS URL + model endpoint) ──────────────────────────────

function loadConfig(): AppConfig { return loadConfigAt(app.getPath('userData')); }
function saveConfig(cfg: { wsUrl: string | null; gemmaBase: string | null }): void {
  saveConfigAt(cfg, app.getPath('userData'));
}

// ── Per-chat workspace store ──────────────────────────────────────────────────

let _workspaceStore: WorkspaceStore | null = null;

function workspaceStore(): WorkspaceStore {
  if (_workspaceStore === null) {
    const seed = process.env.LABMATE_WORKSPACE ? path.resolve(process.env.LABMATE_WORKSPACE) : null;
    _workspaceStore = new WorkspaceStore(path.join(app.getPath('userData'), 'workspaces.json'), seed);
  }
  return _workspaceStore;
}

/** Open the native folder picker (single); returns the chosen absolute path or null. */
async function pickFolder(): Promise<string | null> {
  const win = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0] ?? null;
  const opts = { properties: ['openDirectory', 'createDirectory'] as Array<'openDirectory' | 'createDirectory'> };
  const res = win ? await dialog.showOpenDialog(win, opts) : await dialog.showOpenDialog(opts);
  return res.canceled || !res.filePaths[0] ? null : res.filePaths[0];
}

/** Open the native folder picker allowing multiple selections; returns chosen paths. */
async function pickFolders(): Promise<string[]> {
  const win = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0] ?? null;
  const opts = {
    properties: ['openDirectory', 'createDirectory', 'multiSelections'] as Array<
      'openDirectory' | 'createDirectory' | 'multiSelections'
    >,
  };
  const res = win ? await dialog.showOpenDialog(win, opts) : await dialog.showOpenDialog(opts);
  return res.canceled ? [] : res.filePaths;
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
      label: 'Workspace',
      submenu: [
        {
          label: 'Set Default Workspace…',
          click: async () => {
            const chosen = await pickFolder();
            if (chosen) workspaceStore().setDefault(chosen);
          },
        },
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

// ── MCP Host Manager (hosted MCP skill servers) ──────────────────────────────

const mcpManager = new McpHostManager();
let mcpReady: Promise<void> = Promise.resolve();

// ── Backend supervisor (spawns infrastructure/start.sh, health-gates it) ─────

// main.ts compiles to dist-electron/main.js (see package.json "main"), which
// lives at <repoRoot>/services/frontend/dist-electron/main.js — three levels
// up from __dirname is the repo root (verified: infrastructure/start.sh
// resolves from there).
const REPO_ROOT = path.join(__dirname, '..', '..', '..');
const LOCAL_PORT = Number(process.env.LOCAL_PORT ?? 8787);

const supervisor = new BackendSupervisor();

// Latest backend status, tracked so a renderer created AFTER `ready` (or any
// other status) already fired can still learn the current state via a pull
// (`labmate:get-backend-status`) instead of only a push. The supervisor emits
// `ready` DURING startupSequence, BEFORE createWindow() runs, so a push-only
// design would miss it entirely.
type BackendStatus = SupervisorStatus | { phase: 'model_unreachable'; url: string };
let latestBackendStatus: BackendStatus | null = null;

function broadcastBackendStatus(s: BackendStatus): void {
  latestBackendStatus = s;
  for (const w of BrowserWindow.getAllWindows()) w.webContents.send('labmate:backend-status', s);
}
supervisor.onStatus(broadcastBackendStatus);

// infrastructure/local.env is the single source of truth for LOCAL_PORT +
// GEMMA_BASE (the same file the backend sources). Read once at startup and
// again on every retry so an edit to the pod URL + Retry works without a
// full app restart.
let localEnv: LocalEnv = { localPort: 8787, gemmaBase: '' };

async function runStartupAndProbe(): Promise<void> {
  const localPort = localEnv.localPort;
  const logPath = path.join(REPO_ROOT, '.data', 'logs', 'local.log');
  try {
    await startupSequence(supervisor, localPort, REPO_ROOT, logPath);
  } catch (e) {
    console.error('backend startup failed:', e);
    return; // boot_failed already broadcast by the supervisor
  }
  if (localEnv.gemmaBase) {
    const ok = await BackendSupervisor.probeModel(localEnv.gemmaBase);
    broadcastBackendStatus(ok ? { phase: 'ready' } : { phase: 'model_unreachable', url: localEnv.gemmaBase });
  }
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

let tray: Tray | null = null;
let isQuitting = false;

app.on('before-quit', (_event) => {
  isQuitting = true;
  void mcpManager.stopAll();
  // Best-effort teardown: BackendSupervisor.stop() has its own bounded grace
  // (SIGTERM, then SIGKILL after STOP_GRACE_MS) so this never blocks quit
  // beyond the supervisor's own guard.
  void supervisor.stop();
});

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

  // Default UI scale: render the whole app slightly smaller than 1:1. Applied
  // on every load so it survives dev HMR full-reloads and navigations. Tune
  // UI_ZOOM (1.0 = 100%); lower = smaller.
  win.webContents.on('did-finish-load', () => {
    win.webContents.setZoomFactor(UI_ZOOM);
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

ipcMain.on('labmate:get-config', (event) => {
  event.returnValue = {
    wsUrl: `ws://localhost:${localEnv.localPort}/ws`,
    gemmaBase: localEnv.gemmaBase || null,
    isDev: IS_DEV,
  };
});

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

ipcMain.handle('labmate:set-config', (_evt, cfg: { wsUrl: string | null; gemmaBase: string | null }) => {
  saveConfig(cfg);
});

// ── Backend supervisor status: pull (in case a renderer misses the push) + retry ──

ipcMain.handle('labmate:get-backend-status', () => latestBackendStatus);
ipcMain.handle('labmate:retry-backend', async () => {
  localEnv = await readLocalEnv(REPO_ROOT);
  await runStartupAndProbe();
  return latestBackendStatus;
});

// ── Workspace (multi-root per chat) ───────────────────────────────────────────

ipcMain.handle(
  'labmate:get-workspace-roots',
  (_evt, payload: { sessionId?: string | null } | undefined) =>
    workspaceStore().roots(payload?.sessionId ?? null),
);

ipcMain.handle('labmate:has-default-workspace', () => workspaceStore().hasDefault());
ipcMain.handle('labmate:get-default-workspace', () => workspaceStore().getDefault());

// Pick a folder and set it as the global default ("current directory"). Returns the path or null.
ipcMain.handle('labmate:set-default-workspace', async () => {
  const chosen = await pickFolder();
  if (chosen === null) return { path: null };
  workspaceStore().setDefault(chosen);
  return { path: chosen };
});

// Add one or more directories as roots of a chat. Returns the updated roots.
ipcMain.handle(
  'labmate:add-workspace-root',
  async (_evt, payload: { sessionId: string }) => {
    const chosen = await pickFolders();
    let roots = workspaceStore().roots(payload.sessionId);
    for (const dir of chosen) roots = workspaceStore().addRoot(payload.sessionId, dir);
    await mcpManager.setWorkspaceRoots(roots);
    return { roots };
  },
);

ipcMain.handle(
  'labmate:remove-workspace-root',
  async (_evt, payload: { sessionId: string; path: string }) => {
    const roots = workspaceStore().removeRoot(payload.sessionId, payload.path);
    await mcpManager.setWorkspaceRoots(roots);
    return { roots };
  },
);

// Fuzzy file/dir search across a chat's roots, for the @-mention autocomplete.
ipcMain.handle(
  'labmate:search-workspace',
  async (_evt, payload: { sessionId?: string | null; query: string }) => {
    const roots = workspaceStore().roots(payload?.sessionId ?? null);
    if (roots.length === 0) return { entries: [] };
    return { entries: await searchWorkspace(roots, payload.query ?? '') };
  },
);

// Notify MCP servers of the active session change (update roots)
ipcMain.handle('labmate:active-session', async (_e, payload: { sessionId?: string | null }) => {
  try {
    await mcpManager.setWorkspaceRoots(workspaceStore().roots(payload?.sessionId ?? null));
  } catch (err) {
    console.error('active-session roots update failed:', err);
  }
  return { ok: true };
});

ipcMain.handle('labmate:mcp-tools', async () => {
  try {
    await mcpReady;
    return mcpManager.getToolDescriptors();
  } catch (err) {
    console.error('failed to get mcp tools:', err);
    return [];
  }
});

ipcMain.handle('labmate:skill-descriptors', async () => {
  try {
    const skills = skillDescriptors(userSkillsDir());
    return skills;
  } catch (err) {
    console.error('failed to get skill descriptors:', err);
    return [];
  }
});

ipcMain.handle(
  'labmate:tool-execute',
  async (_evt, payload: unknown) => {
    try {
      if (!payload || typeof payload !== 'object') return { error: 'invalid payload' };
      const { name, args, sessionId } = payload as {
        name: string;
        args: Record<string, unknown>;
        sessionId?: string | null;
      };

      // Route mcp__ prefixed tools to McpHostManager
      if (name.startsWith('mcp__')) {
        const roots = workspaceStore().roots(sessionId ?? null);
        // Resolve path args to absolute, then default CodeGraph's projectPath to the
        // active chat's workspace root (CodeGraph resolves its repo from projectPath/cwd,
        // not MCP roots), so codegraph_* tools follow the chat's workspace.
        const rootedArgs = injectCodegraphProjectPath(
          name,
          resolveMcpPathArgs(args ?? {}, roots),
          roots,
        );
        return { result: await mcpManager.callTool(name, rootedArgs) };
      }

      if (!LOCAL_TOOL_NAMES.includes(name as LocalToolName)) {
        return { error: `unknown local tool: ${name}` };
      }
      const roots = workspaceStore().roots(sessionId ?? null);
      return { result: await executeTool(name as LocalToolName, args ?? {}, roots) };
    } catch (err) {
      return { error: err instanceof Error ? err.message : String(err) };
    }
  },
);

void app.whenReady().then(async () => {
  ensureUserSkillsDir();
  mcpReady = mcpManager.startAll().catch((e) => {
    console.error('mcp startAll failed:', e);
  });
  // Seed initial roots for the default workspace (null sessionId)
  try {
    void mcpManager.setWorkspaceRoots(workspaceStore().roots(null));
  } catch (err) {
    console.error('failed to seed initial workspace roots:', err);
  }

  localEnv = await readLocalEnv(REPO_ROOT);
  await runStartupAndProbe();

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
