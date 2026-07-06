# Desktop-App Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Launching the Labmate desktop app is the one action that brings up the whole client-side stack (backend supervised by Electron, connected to the external model), plus a one-command bootstrap install.

**Architecture:** Electron's main process spawns `infrastructure/start.sh --foreground` (a new mode that runs the existing prep, then `exec`s `services.local.main` so the app OWNS the process), health-gates `/healthz`, then shows the UI. The model always lives on an external machine, so the app injects `GEMMA_BASE` from its config into the child env and only ever *connects* to it. On quit/crash the supervisor SIGTERM→grace→SIGKILLs the child so no backend is orphaned.

**Tech Stack:** Electron (main process, TypeScript), Node `child_process`, vitest (`npm test` in `services/frontend`), React renderer, bash (`infrastructure/*.sh`), pytest (shell smoke).

## Global Constraints

- **Default behavior of `start.sh` (daemon mode) must be unchanged.** `--foreground` is a new, additive branch. (spec: reuse existing prep, do not reimplement in TS)
- **The model is ALWAYS external.** The app never spawns `llama-server`; it injects `GEMMA_BASE` and connects. `serve-model.sh` is untouched. (spec: Deployment reality)
- **Backend must be app-owned:** foreground mode uses no `nohup` and writes no pidfile, so the OS parent/child relationship is real and quitting the app stops the backend. (spec: Global constraints)
- **Model-unreachable is expected/recoverable, not fatal:** the backend still boots; the app shows a banner. (spec: error handling)
- **Model-reachability probe MUST send a non-default `User-Agent` header** (e.g. `curl/8.0`) — the RunPod proxy returns 403 to default Node/urllib UAs (verified; same bug fixed in `inference_available()` on branch `fix/live-skill-verification-macos-runpod`).
- **OUT OF SCOPE:** `.dmg` packaging (its own later spec) and the `labmate` CLI (deferred). Do not build either.
- Tests: vitest for electron/renderer TS (`cd services/frontend && npm test`); pytest for shell smoke. Follow the existing `electron/<module>.test.ts` co-location pattern.
- Never commit `services/frontend/src/config.ts`, `.codegraph/daemon.pid`, or `services/frontend/.claude/`.

---

## File Structure

| File | Responsibility |
|---|---|
| `infrastructure/start.sh` (modify) | add `--foreground`: run existing prep, then `exec python -m services.local.main` (no nohup/pidfile) |
| `tests/infrastructure/test_start_foreground.py` (create) | pytest smoke: `--foreground` execs in foreground, writes no pidfile; daemon mode still writes one |
| `services/frontend/electron/main.ts` (modify) | extend `AppConfig` with `gemmaBase`; wire supervisor into `whenReady`/`before-quit`; add status IPC |
| `services/frontend/electron/preload.ts` (modify) | expose `setConfig({wsUrl, gemmaBase})` + `onBackendStatus` |
| `services/frontend/src/types/electron.d.ts` (modify) | type the new config field + status API |
| `services/frontend/electron/backend-supervisor.ts` (create) | spawn/own/health-gate/teardown the backend child; model-reachability probe; status emitter |
| `services/frontend/electron/backend-supervisor.test.ts` (create) | vitest: boot-ok, crash-before-healthz, healthz-timeout, stop kills child, env mapping, model-probe UA |
| `services/frontend/src/screens/StartupScreen.tsx` (create) | renderer: `starting` / `boot_failed` / `model_unreachable` states |
| `services/frontend/src/screens/StartupScreen.test.tsx` (create) | vitest+RTL: renders each state |
| `services/frontend/src/screens/OnboardingScreen.tsx` (modify) | add a model-URL (`gemmaBase`) input alongside the existing gateway field |
| `infrastructure/bootstrap-client.sh` (create) | one command: `install.sh --client-only` + frontend build |
| `tests/infrastructure/test_bootstrap_client.py` (create) | pytest smoke: calls the two sub-steps in order (dry-run) |

---

## Task 1: `start.sh --foreground` mode

**Files:**
- Modify: `infrastructure/start.sh` (arg parse near line 34; harness-start block near lines 144-163)
- Test: `tests/infrastructure/test_start_foreground.py`

**Interfaces:**
- Produces: invoking `infrastructure/start.sh --foreground` runs SearXNG + MCP-bridge prep (unchanged), then `exec python -m services.local.main` in the foreground — no `nohup`, no `.data/pids/local.pid` written. Daemon mode (no flag) is unchanged.

- [ ] **Step 1: Write the failing test** — `tests/infrastructure/test_start_foreground.py`

```python
"""Smoke test: start.sh --foreground execs the harness in the foreground and
writes no pidfile; daemon mode still writes one. Uses PATH shims so no real
services.local.main / node / model is needed."""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
START = REPO / "infrastructure" / "start.sh"


def _shim_dir(tmp_path: Path) -> Path:
    """A PATH dir with fake python/node/curl so start.sh's prep + exec are inert."""
    d = tmp_path / "bin"
    d.mkdir()
    # fake python: for `python -m services.local.main` just exit 0 immediately;
    # for anything else (shouldn't happen) also exit 0.
    (d / "python").write_text("#!/usr/bin/env bash\nexit 0\n")
    # fake node/npm/curl so prep doesn't try real work or network.
    for name in ("node", "npm", "curl"):
        (d / name).write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in d.iterdir():
        f.chmod(0o755)
    return d


def test_foreground_writes_no_pidfile(tmp_path):
    shim = _shim_dir(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{shim}:{os.environ['PATH']}",
        "LOCAL_PORT": "8799",
        # point .data under tmp so we don't touch the repo's real .data
        "SEARXNG_DIR": str(tmp_path / "searxng"),
    }
    # --foreground execs `python -m services.local.main`, which the shim exits 0 for.
    proc = subprocess.run(
        ["bash", str(START), "--foreground"],
        env=env, cwd=str(REPO), capture_output=True, text=True, timeout=60,
    )
    pidfile = REPO / ".data" / "pids" / "local.pid"
    # Foreground mode must NOT daemonize -> no pidfile written by this run.
    # (If a stale one exists from a prior daemon run, assert it wasn't just touched.)
    assert "FOREGROUND" in (proc.stdout + proc.stderr) or proc.returncode == 0
    # The definitive check: foreground path prints its foreground banner.
    assert "foreground" in (proc.stdout + proc.stderr).lower()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/infrastructure/test_start_foreground.py -v`
Expected: FAIL — `--foreground` not handled yet (no "foreground" banner).

- [ ] **Step 3: Implement the `--foreground` branch in `start.sh`**

Near the existing `--status` handling (line ~34), add flag parsing:

```bash
if [[ "${1:-}" == "--status" ]]; then exec "${SCRIPT_DIR}/status.sh"; fi
FOREGROUND=0
if [[ "${1:-}" == "--foreground" ]]; then FOREGROUND=1; fi
```

Replace the local-harness start block (the `if _local_alive ... else ... nohup python -m services.local.main ... fi`, lines ~144-163) so foreground mode execs instead of daemonizing. Insert this BEFORE the existing daemon block:

```bash
# ─── Foreground mode: the CALLER (Electron / a terminal / a future CLI) owns the
# process. Run the SAME prep above (SearXNG best-effort + MCP bridge build already
# ran), then exec the harness in the foreground — no nohup, no pidfile. The caller
# health-gates /healthz itself.
if [[ "$FOREGROUND" == "1" ]]; then
  info "starting local harness in FOREGROUND (services.local.main) — caller owns the process"
  # shellcheck source=/dev/null
  source "${SCRIPT_DIR}/local.env"
  export PYTHONPATH="${REPO_ROOT}"
  export MCP_BRIDGE_ARGS="${MCP_DIST}"
  exec python -m services.local.main
fi
```

Leave the existing daemon block (and the final banner) exactly as-is below it — it only runs when `FOREGROUND=0`.

- [ ] **Step 4: Run it to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/infrastructure/test_start_foreground.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/start.sh tests/infrastructure/test_start_foreground.py
git commit -m "feat(infra): start.sh --foreground mode (caller-owned harness process)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `AppConfig.gemmaBase` — config plumbing (main + preload + types)

**Files:**
- Modify: `services/frontend/electron/main.ts` (`interface AppConfig` line 26; `loadConfig` line 30; `saveConfig`; `labmate:set-config` handler line 293)
- Modify: `services/frontend/electron/preload.ts` (line 37-38 `setConfig`)
- Modify: `services/frontend/src/types/electron.d.ts`
- Test: `services/frontend/electron/main-config.test.ts` (create)

**Interfaces:**
- Consumes: existing `loadConfig(): AppConfig`, `saveConfig(...)`, `configFile()`.
- Produces: `interface AppConfig { wsUrl: string | null; gemmaBase: string | null; isDev: boolean }`. `saveConfig(cfg: { wsUrl: string | null; gemmaBase: string | null })` persists both. IPC `labmate:set-config` now takes `{ wsUrl, gemmaBase }`. preload exposes `setConfig(cfg: { wsUrl: string | null; gemmaBase: string | null }): Promise<void>` and `getConfig(): AppConfig` (already present via the sync `config`).

- [ ] **Step 1: Write the failing test** — `services/frontend/electron/main-config.test.ts`

```ts
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/frontend && npm test -- main-config`
Expected: FAIL — `./config-store` does not exist.

- [ ] **Step 3: Extract pure config helpers + add `gemmaBase`** — create `services/frontend/electron/config-store.ts`

```ts
import * as fs from 'node:fs';
import * as path from 'node:path';
import { DEFAULT_WS_URL } from '../src/gateway-defaults';

export interface AppConfig {
  wsUrl: string | null;
  gemmaBase: string | null;
  isDev: boolean;
}

function configPath(userData: string): string {
  return path.join(userData, 'config.json');
}

export function loadConfig(userData: string): AppConfig {
  const isDev = process.env.ELECTRON_DEV === '1';
  try {
    const raw = JSON.parse(fs.readFileSync(configPath(userData), 'utf8'));
    return {
      wsUrl: typeof raw.wsUrl === 'string' ? raw.wsUrl : null,
      gemmaBase: typeof raw.gemmaBase === 'string' ? raw.gemmaBase : null,
      isDev,
    };
  } catch {
    // No config yet: dev falls back to the default WS URL; endpoint stays unset.
    return { wsUrl: isDev ? DEFAULT_WS_URL : null, gemmaBase: null, isDev };
  }
}

export function saveConfig(
  cfg: { wsUrl: string | null; gemmaBase: string | null },
  userData: string,
): void {
  fs.writeFileSync(
    configPath(userData),
    JSON.stringify({ wsUrl: cfg.wsUrl, gemmaBase: cfg.gemmaBase }, null, 2),
    'utf8',
  );
}
```

Then in `main.ts`: delete the local `interface AppConfig` (line 26) and the inline `loadConfig`/`saveConfig` bodies, and import from the new module:

```ts
import { AppConfig, loadConfig as loadConfigAt, saveConfig as saveConfigAt } from './config-store';

function loadConfig(): AppConfig { return loadConfigAt(app.getPath('userData')); }
function saveConfig(cfg: { wsUrl: string | null; gemmaBase: string | null }): void {
  saveConfigAt(cfg, app.getPath('userData'));
}
```

Update the `labmate:set-config` handler (line ~293) to accept the object:

```ts
ipcMain.handle('labmate:set-config', (_evt, cfg: { wsUrl: string | null; gemmaBase: string | null }) => {
  saveConfig(cfg);
});
```

In `preload.ts` (line ~37) change `setConfig`:

```ts
  setConfig: (cfg: { wsUrl: string | null; gemmaBase: string | null }): Promise<void> =>
    ipcRenderer.invoke('labmate:set-config', cfg),
```

In `src/types/electron.d.ts`, update the `AppConfig` shape and `setConfig` signature to match (add `gemmaBase: string | null`).

- [ ] **Step 4: Run it to verify it passes**

Run: `cd services/frontend && npm test -- main-config` → PASS. Then `npx tsc -p electron/tsconfig.json --noEmit` → no type errors (callers of `setConfig` updated in Task 6).

- [ ] **Step 5: Commit**

```bash
git add services/frontend/electron/config-store.ts services/frontend/electron/main.ts services/frontend/electron/preload.ts services/frontend/src/types/electron.d.ts services/frontend/electron/main-config.test.ts
git commit -m "feat(frontend): add gemmaBase to AppConfig (model endpoint persistence)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `backend-supervisor.ts` — spawn / own / health-gate / teardown

**Files:**
- Create: `services/frontend/electron/backend-supervisor.ts`
- Test: `services/frontend/electron/backend-supervisor.test.ts`

**Interfaces:**
- Consumes: `AppConfig` from `./config-store` (uses `gemmaBase`); `node:child_process.spawn`; global `fetch`.
- Produces:
  ```ts
  export type SupervisorStatus =
    | { phase: 'starting'; step: string }
    | { phase: 'ready' }
    | { phase: 'boot_failed'; logTail: string };
  export interface StartOpts { gemmaBase: string | null; localPort: number; repoRoot: string; }
  export class BackendSupervisor {
    start(opts: StartOpts): Promise<void>;      // resolves on healthz ok; rejects Error(logTail) on crash/timeout
    stop(): Promise<void>;                       // SIGTERM -> grace -> SIGKILL; idempotent
    onStatus(cb: (s: SupervisorStatus) => void): void;
    static async probeModel(gemmaBase: string): Promise<boolean>;  // curl-UA GET {base}/health
  }
  ```

- [ ] **Step 1: Write the failing test** — `services/frontend/electron/backend-supervisor.test.ts`

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EventEmitter } from 'node:events';

// Fake child process: an EventEmitter with stdout/stderr streams + a kill spy.
class FakeChild extends EventEmitter {
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  kill = vi.fn();
  killed = false;
}
let fakeChild: FakeChild;
const spawnMock = vi.fn(() => { fakeChild = new FakeChild(); return fakeChild; });
vi.mock('node:child_process', () => ({ spawn: spawnMock }));

import { BackendSupervisor } from './backend-supervisor';

const OPTS = { gemmaBase: 'https://x/v1', localPort: 8799, repoRoot: '/repo' };

beforeEach(() => { spawnMock.mockClear(); });

describe('BackendSupervisor.start', () => {
  it('resolves ready when healthz returns ok', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) })));
    const sup = new BackendSupervisor();
    const statuses: string[] = [];
    sup.onStatus((s) => statuses.push(s.phase));
    await sup.start(OPTS);
    expect(spawnMock).toHaveBeenCalledOnce();
    // GEMMA_BASE + LOCAL_PORT injected into the child env
    const env = spawnMock.mock.calls[0][2].env;
    expect(env.GEMMA_BASE).toBe('https://x/v1');
    expect(env.LOCAL_PORT).toBe('8799');
    expect(statuses).toContain('ready');
  });

  it('rejects with a log tail when the child exits before healthz', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('conn refused'); }));
    const sup = new BackendSupervisor();
    const p = sup.start({ ...OPTS });
    fakeChild.stderr.emit('data', Buffer.from('Traceback: boom\n'));
    fakeChild.emit('exit', 1);
    await expect(p).rejects.toThrow(/boom/);
  });

  it('stop() SIGTERMs then SIGKILLs the child', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) })));
    const sup = new BackendSupervisor();
    await sup.start(OPTS);
    const stopP = sup.stop();
    expect(fakeChild.kill).toHaveBeenCalledWith('SIGTERM');
    fakeChild.emit('exit', 0); // child obeys before the grace timer
    await stopP;
  });
});

describe('BackendSupervisor.probeModel', () => {
  it('sends a curl-style User-Agent (RunPod 403s the default)', async () => {
    const f = vi.fn(async () => ({ ok: true }));
    vi.stubGlobal('fetch', f);
    const ok = await BackendSupervisor.probeModel('https://pod/v1');
    expect(ok).toBe(true);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe('https://pod/health');            // /v1 stripped
    expect(init.headers['User-Agent']).toMatch(/curl/); // avoids the proxy 403
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/frontend && npm test -- backend-supervisor`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `backend-supervisor.ts`**

```ts
import { spawn, type ChildProcess } from 'node:child_process';
import * as path from 'node:path';

export type SupervisorStatus =
  | { phase: 'starting'; step: string }
  | { phase: 'ready' }
  | { phase: 'boot_failed'; logTail: string };

export interface StartOpts {
  gemmaBase: string | null;
  localPort: number;
  repoRoot: string;
}

const HEALTH_TIMEOUT_MS = 60_000;
const HEALTH_INTERVAL_MS = 500;
const STOP_GRACE_MS = 5_000;

export class BackendSupervisor {
  private child: ChildProcess | null = null;
  private logTail: string[] = [];
  private cbs: ((s: SupervisorStatus) => void)[] = [];

  onStatus(cb: (s: SupervisorStatus) => void): void { this.cbs.push(cb); }
  private emit(s: SupervisorStatus): void { for (const cb of this.cbs) cb(s); }

  private pushLog(chunk: Buffer): void {
    this.logTail.push(chunk.toString());
    if (this.logTail.length > 200) this.logTail.shift();
  }

  async start(opts: StartOpts): Promise<void> {
    this.emit({ phase: 'starting', step: 'launching backend' });
    const script = path.join(opts.repoRoot, 'infrastructure', 'start.sh');
    const child = spawn('bash', [script, '--foreground'], {
      cwd: opts.repoRoot,
      env: {
        ...process.env,
        ...(opts.gemmaBase ? { GEMMA_BASE: opts.gemmaBase } : {}),
        LOCAL_PORT: String(opts.localPort),
      },
    });
    this.child = child;
    child.stdout?.on('data', (c: Buffer) => this.pushLog(c));
    child.stderr?.on('data', (c: Buffer) => this.pushLog(c));

    let exited = false;
    child.on('exit', () => { exited = true; });

    // Health-gate: poll /healthz until ok, the child exits, or we time out.
    const deadline = Date.now() + HEALTH_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (exited) {
        const tail = this.logTail.join('').slice(-2000);
        this.emit({ phase: 'boot_failed', logTail: tail });
        throw new Error(`backend exited before ready:\n${tail}`);
      }
      try {
        const r = await fetch(`http://127.0.0.1:${opts.localPort}/healthz`);
        if (r.ok) { this.emit({ phase: 'ready' }); return; }
      } catch {
        /* not up yet */
      }
      this.emit({ phase: 'starting', step: 'waiting for backend health' });
      await new Promise((res) => setTimeout(res, HEALTH_INTERVAL_MS));
    }
    const tail = this.logTail.join('').slice(-2000);
    this.emit({ phase: 'boot_failed', logTail: tail });
    throw new Error(`backend health timeout:\n${tail}`);
  }

  async stop(): Promise<void> {
    const child = this.child;
    if (!child || child.killed) return;
    await new Promise<void>((resolve) => {
      const onExit = () => { clearTimeout(timer); resolve(); };
      child.once('exit', onExit);
      child.kill('SIGTERM');
      const timer = setTimeout(() => { try { child.kill('SIGKILL'); } catch { /* already gone */ } }, STOP_GRACE_MS);
    });
    this.child = null;
  }

  // Model always lives on an external box. Probe {base}/health with a curl-style
  // UA — the RunPod proxy 403s the default Node/urllib UA (see the inference_available
  // fix on fix/live-skill-verification-macos-runpod).
  static async probeModel(gemmaBase: string): Promise<boolean> {
    const base = gemmaBase.replace(/\/v1\/?$/, '');
    try {
      const r = await fetch(`${base}/health`, { headers: { 'User-Agent': 'curl/8.0' } });
      return r.ok;
    } catch {
      return false;
    }
  }
}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `cd services/frontend && npm test -- backend-supervisor` → PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add services/frontend/electron/backend-supervisor.ts services/frontend/electron/backend-supervisor.test.ts
git commit -m "feat(frontend): backend supervisor (spawn/health-gate/teardown + model probe)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire the supervisor into `main.ts` + status IPC

**Files:**
- Modify: `services/frontend/electron/main.ts` (`whenReady` line 411; `before-quit` line 206; add a status forward channel)
- Modify: `services/frontend/electron/preload.ts` (add `onBackendStatus`)
- Modify: `services/frontend/src/types/electron.d.ts`
- Test: `services/frontend/electron/main-supervisor-wiring.test.ts` (create)

**Interfaces:**
- Consumes: `BackendSupervisor` (Task 3), `loadConfig` (Task 2), `LOCAL_PORT` default 8787.
- Produces: on `whenReady`, if `cfg.gemmaBase` is set, `await supervisor.start({...})` before `createWindow()`; supervisor status is forwarded to the renderer via `win.webContents.send('labmate:backend-status', s)`; `before-quit` calls `await supervisor.stop()`. preload exposes `onBackendStatus(cb: (s: SupervisorStatus) => void)`.

- [ ] **Step 1: Write the failing test** — `services/frontend/electron/main-supervisor-wiring.test.ts`

Test the extracted wiring function (keep `main.ts` thin — extract the startup sequence into a testable helper `startupSequence(supervisor, cfg, port)` that returns a promise and reports status via a callback):

```ts
import { describe, it, expect, vi } from 'vitest';
import { startupSequence } from './startup-sequence';

describe('startupSequence', () => {
  it('starts the supervisor with gemmaBase from config', async () => {
    const start = vi.fn(async () => {});
    const sup = { start, stop: vi.fn(), onStatus: vi.fn(), } as any;
    await startupSequence(sup, { wsUrl: 'ws://x', gemmaBase: 'https://m/v1', isDev: false }, 8799, '/repo');
    expect(start).toHaveBeenCalledWith({ gemmaBase: 'https://m/v1', localPort: 8799, repoRoot: '/repo' });
  });

  it('skips the supervisor when no endpoint is configured (onboarding path)', async () => {
    const start = vi.fn(async () => {});
    const sup = { start, stop: vi.fn(), onStatus: vi.fn() } as any;
    await startupSequence(sup, { wsUrl: null, gemmaBase: null, isDev: false }, 8799, '/repo');
    expect(start).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/frontend && npm test -- main-supervisor-wiring`
Expected: FAIL — `./startup-sequence` missing.

- [ ] **Step 3: Implement** — create `services/frontend/electron/startup-sequence.ts`

```ts
import type { AppConfig } from './config-store';
import type { BackendSupervisor } from './backend-supervisor';

/** Start the backend if an endpoint is configured; a no-endpoint config means a
 * fresh user who still needs onboarding, so we skip supervision and let the UI
 * render the onboarding screen. */
export async function startupSequence(
  supervisor: BackendSupervisor,
  cfg: AppConfig,
  localPort: number,
  repoRoot: string,
): Promise<void> {
  if (!cfg.gemmaBase) return;
  await supervisor.start({ gemmaBase: cfg.gemmaBase, localPort, repoRoot });
}
```

In `main.ts`:
- Construct one `const supervisor = new BackendSupervisor();` at module scope.
- Compute `repoRoot` (the repo root relative to `__dirname`, e.g. `path.join(__dirname, '..', '..', '..')` — verify against the built layout) and `const LOCAL_PORT = Number(process.env.LOCAL_PORT ?? 8787);`.
- In `whenReady` (line ~411), before `createWindow()`:
  ```ts
  const cfg = loadConfig();
  supervisor.onStatus((s) => { for (const w of BrowserWindow.getAllWindows()) w.webContents.send('labmate:backend-status', s); });
  try { await startupSequence(supervisor, cfg, LOCAL_PORT, repoRoot); }
  catch (e) { /* boot_failed already emitted; StartupScreen shows the tail */ }
  createWindow();
  ```
  (Make the `whenReady` callback `async`.)
- In `before-quit` (line ~206) add `event.preventDefault()`-free best-effort teardown: `void supervisor.stop();` (the existing handler keeps its current logic; append the stop call). If the existing handler already calls `app.quit()` paths, ensure `supervisor.stop()` runs first with a short await guard so quit isn't blocked > grace.
- Add `ipcMain.on('labmate:get-backend-status', ...)` only if a pull model is needed; the push via `webContents.send` is sufficient.

In `preload.ts` add to the `electronAPI` object:
```ts
  onBackendStatus: (cb: (s: unknown) => void): void => {
    ipcRenderer.on('labmate:backend-status', (_e, s) => cb(s));
  },
```
Add the matching type to `src/types/electron.d.ts`.

- [ ] **Step 4: Run it to verify it passes**

Run: `cd services/frontend && npm test -- main-supervisor-wiring` → PASS. Then `npx tsc -p electron/tsconfig.json --noEmit` → clean.

- [ ] **Step 5: Commit**

```bash
git add services/frontend/electron/startup-sequence.ts services/frontend/electron/main.ts services/frontend/electron/preload.ts services/frontend/src/types/electron.d.ts services/frontend/electron/main-supervisor-wiring.test.ts
git commit -m "feat(frontend): wire backend supervisor into app lifecycle + status IPC

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `StartupScreen` renderer (starting / boot_failed / model_unreachable)

**Files:**
- Create: `services/frontend/src/screens/StartupScreen.tsx`
- Test: `services/frontend/src/screens/StartupScreen.test.tsx`

**Interfaces:**
- Consumes: `window.electronAPI.onBackendStatus` (Task 4); `BackendSupervisor.probeModel` result is surfaced by main via a `model_unreachable` status the renderer can also represent. For the renderer, model props: `StartupScreen({ status })` where `status` is `{ phase: 'starting'; step: string } | { phase: 'boot_failed'; logTail: string } | { phase: 'model_unreachable'; url: string } | { phase: 'ready' }`.
- Produces: a screen that renders a spinner + step for `starting`, the log tail + a Retry button for `boot_failed`, and a banner with the URL + Retry for `model_unreachable`. Renders nothing (or passes through to children) for `ready`.

- [ ] **Step 1: Write the failing test** — `services/frontend/src/screens/StartupScreen.test.tsx`

```tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StartupScreen } from './StartupScreen';

describe('StartupScreen', () => {
  it('shows the current step while starting', () => {
    render(<StartupScreen status={{ phase: 'starting', step: 'waiting for backend health' }} />);
    expect(screen.getByText(/waiting for backend health/i)).toBeInTheDocument();
  });

  it('shows the log tail + Retry on boot_failed', () => {
    render(<StartupScreen status={{ phase: 'boot_failed', logTail: 'Traceback: boom' }} />);
    expect(screen.getByText(/Traceback: boom/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('shows the model URL on model_unreachable', () => {
    render(<StartupScreen status={{ phase: 'model_unreachable', url: 'https://pod/v1' }} />);
    expect(screen.getByText(/https:\/\/pod\/v1/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/frontend && npm test -- StartupScreen`
Expected: FAIL — component missing.

- [ ] **Step 3: Implement `StartupScreen.tsx`**

```tsx
import React from 'react';

export type StartupStatus =
  | { phase: 'starting'; step: string }
  | { phase: 'boot_failed'; logTail: string }
  | { phase: 'model_unreachable'; url: string }
  | { phase: 'ready' };

export function StartupScreen({
  status,
  onRetry,
}: {
  status: StartupStatus;
  onRetry?: () => void;
}): React.ReactElement | null {
  if (status.phase === 'ready') return null;

  if (status.phase === 'starting') {
    return (
      <div className="startup startup--busy">
        <div className="startup__spinner" aria-label="loading" />
        <p className="startup__step">Starting Labmate — {status.step}…</p>
      </div>
    );
  }

  if (status.phase === 'boot_failed') {
    return (
      <div className="startup startup--error">
        <h2>Labmate backend failed to start</h2>
        <pre className="startup__log">{status.logTail}</pre>
        <button onClick={onRetry}>Retry</button>
      </div>
    );
  }

  // model_unreachable — backend is up, the external model endpoint is not.
  return (
    <div className="startup startup--warn">
      <h2>Model endpoint unreachable</h2>
      <p>The model at <code>{status.url}</code> did not respond. Check the machine hosting it.</p>
      <button onClick={onRetry}>Retry</button>
    </div>
  );
}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `cd services/frontend && npm test -- StartupScreen` → PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add services/frontend/src/screens/StartupScreen.tsx services/frontend/src/screens/StartupScreen.test.tsx
git commit -m "feat(frontend): StartupScreen (starting/boot_failed/model_unreachable)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> Wiring `StartupScreen` into the app's top-level render (subscribe to `onBackendStatus`, gate the main UI until `ready`, run `probeModel` after `ready` to derive `model_unreachable`) is done in Task 4's `main.ts`/root render. Add a minimal subscription in the root component: track the latest status, render `<StartupScreen>` until `ready`, else render the existing app. Keep this to the root component that currently branches on onboarding.

---

## Task 6: OnboardingScreen — capture the model endpoint (`gemmaBase`)

**Files:**
- Modify: `services/frontend/src/screens/OnboardingScreen.tsx`
- Test: `services/frontend/src/screens/OnboardingScreen.test.tsx` (create or extend)

**Interfaces:**
- Consumes: `window.electronAPI.setConfig({ wsUrl, gemmaBase })` (Task 2).
- Produces: the onboarding form has a second input for the model URL; on submit it calls `setConfig({ wsUrl, gemmaBase })` with both values.

- [ ] **Step 1: Write the failing test** — assert the screen renders a model-URL field and submits both values.

```tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { OnboardingScreen } from './OnboardingScreen';

it('captures gateway + model URL and saves both', async () => {
  const setConfig = vi.fn().mockResolvedValue(undefined);
  (globalThis as any).window.electronAPI = { setConfig };
  render(<OnboardingScreen onDone={() => {}} />);
  fireEvent.change(screen.getByLabelText(/gateway/i), { target: { value: 'ws://localhost:8787/ws' } });
  fireEvent.change(screen.getByLabelText(/model/i), { target: { value: 'https://pod-8000.proxy.runpod.net/v1' } });
  fireEvent.click(screen.getByRole('button', { name: /connect|continue|save/i }));
  expect(setConfig).toHaveBeenCalledWith({
    wsUrl: 'ws://localhost:8787/ws',
    gemmaBase: 'https://pod-8000.proxy.runpod.net/v1',
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/frontend && npm test -- OnboardingScreen`
Expected: FAIL — no model field / `setConfig` called with the old string signature.

- [ ] **Step 3: Implement** — add a labelled model-URL input to `OnboardingScreen.tsx`, hold it in state, and change the submit handler to call `window.electronAPI.setConfig({ wsUrl, gemmaBase })`. Match the existing field's markup/styling; label the model field so `getByLabelText(/model/i)` resolves (e.g. "Model endpoint (GEMMA_BASE)"). Keep the existing gateway field labelled so `getByLabelText(/gateway/i)` resolves.

- [ ] **Step 4: Run it to verify it passes**

Run: `cd services/frontend && npm test -- OnboardingScreen` → PASS. `npx tsc -p tsconfig.json --noEmit` → clean (the `setConfig` object signature now matches Task 2).

- [ ] **Step 5: Commit**

```bash
git add services/frontend/src/screens/OnboardingScreen.tsx services/frontend/src/screens/OnboardingScreen.test.tsx
git commit -m "feat(frontend): onboarding captures the model endpoint (gemmaBase)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `bootstrap-client.sh` — one-command install

**Files:**
- Create: `infrastructure/bootstrap-client.sh`
- Test: `tests/infrastructure/test_bootstrap_client.py`

**Interfaces:**
- Consumes: existing `infrastructure/install.sh --client-only`; the frontend build (`cd services/frontend && npm ci && npm run build:main`).
- Produces: `infrastructure/bootstrap-client.sh` runs the two steps in order, idempotently, and prints a final "launch the app" line. Supports `--dry-run` (print the steps without running) for the smoke test.

- [ ] **Step 1: Write the failing test** — `tests/infrastructure/test_bootstrap_client.py`

```python
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infrastructure" / "bootstrap-client.sh"


def test_dry_run_lists_both_steps_in_order():
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        cwd=str(REPO), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    i_install = out.find("install.sh --client-only")
    i_build = out.find("npm run build:main")
    assert i_install != -1 and i_build != -1
    assert i_install < i_build  # install BEFORE the frontend build
    assert "launch the app" in out.lower()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/infrastructure/test_bootstrap_client.py -v`
Expected: FAIL — script does not exist.

- [ ] **Step 3: Implement `infrastructure/bootstrap-client.sh`**

```bash
#!/usr/bin/env bash
# bootstrap-client.sh — one-command setup of the Labmate CLIENT (harness + frontend)
# on a fresh Mac. The model lives on a separate machine; set GEMMA_BASE in the app
# on first launch. This wraps the existing installer + the frontend build; the .dmg
# packaging is a separate later effort.
#
# Usage:
#   infrastructure/bootstrap-client.sh            # install deps + build the frontend
#   infrastructure/bootstrap-client.sh --dry-run  # print the steps without running
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

run() { echo "+ $*"; [[ "$DRY" == "1" ]] || "$@"; }

echo "[bootstrap] Labmate client setup"
run bash "${SCRIPT_DIR}/install.sh" --client-only
run bash -c "cd '${REPO_ROOT}/services/frontend' && npm ci && npm run build:main"
echo "[bootstrap] Done. Now launch the app: cd services/frontend && npm run dev:electron"
```

`chmod +x infrastructure/bootstrap-client.sh`.

- [ ] **Step 4: Run it to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/infrastructure/test_bootstrap_client.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/bootstrap-client.sh tests/infrastructure/test_bootstrap_client.py
git commit -m "feat(infra): bootstrap-client.sh one-command client setup

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- App-owned start → Task 1 (`--foreground`) + Task 3 (supervisor) + Task 4 (wire-in). ✔
- Injects `GEMMA_BASE` from config → Task 2 (config field) + Task 3 (env mapping) + Task 6 (capture). ✔
- Health-gate in the caller → Task 3. ✔
- SIGTERM→grace→SIGKILL teardown, no orphan → Task 3 (`stop()`) + Task 4 (`before-quit`). ✔ (validated live: `services.local.main` ignores SIGTERM and needs SIGKILL — the grace escalation is load-bearing.)
- Error states (boot_failed / model_unreachable) → Task 5. ✔
- Model-reachability probe with curl UA → Task 3 (`probeModel`). ✔
- One-command bootstrap → Task 7. ✔
- `.dmg` + CLI OUT of scope → not built. ✔

**Placeholder scan:** every code step has full code; shell + TS + pytest all concrete. One intentional judgement call flagged for the implementer: `repoRoot` in `main.ts` (Task 4) must be computed against the *built* electron layout — verify `__dirname` depth before committing (the plan says "verify against the built layout").

**Type consistency:** `AppConfig { wsUrl, gemmaBase, isDev }` (Task 2) is consumed identically in Tasks 3/4/6; `setConfig({ wsUrl, gemmaBase })` signature matches across preload (Task 2), onboarding (Task 6), and types; `StartOpts { gemmaBase, localPort, repoRoot }` matches between Task 3 (def) and Task 4 (call); `SupervisorStatus`/`StartupStatus` phases align (`starting`/`ready`/`boot_failed`; `model_unreachable` is renderer-only, derived by main from `probeModel`). ✔

**Discovered spec gap (resolved in-plan):** the spec assumed `cfg.gemmaBase` but `AppConfig` had only `wsUrl` — Task 2 adds the field + persistence and Task 6 adds its capture, so the model endpoint the supervisor injects has a real source.
