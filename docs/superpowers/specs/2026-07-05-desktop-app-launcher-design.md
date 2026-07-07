# Desktop-App Launcher — Single Start + One-Command Bootstrap (Design)

**Date:** 2026-07-05
**Status:** Design approved (brainstorm) — pending written-spec review before planning.
**Branch:** `feat/desktop-app-launcher` (off `experimental`).

## Goal

Launching the Labmate desktop app is the ONE action a user takes to bring the whole
client-side stack up — like starting Claude Desktop. The Electron app supervises the
local backend (`services.local.main`, plus best-effort SearXNG), connects to the
remote model endpoint, and tears everything down on quit. Installation collapses to a
single bootstrap command on a fresh Mac.

## Deployment reality this design assumes (fixed constraints)

- **Single-user-per-harness.** Each user runs their own local harness. The desktop
  **frontend is the primary interface** (not the CLI). No multi-tenant concerns.
- **The model is ALWAYS external.** `llama-server` lives on a separate machine (RunPod
  today). The app is *always* a pure client to a configured `GEMMA_BASE` — it never
  spawns, supervises, downloads, or health-manages `llama-server`. `serve-model.sh`
  stays entirely on the model box and is out of scope here.
- **macOS client for now.** Windows/Linux client packaging is deferred.

## Scope

**In scope**
1. **App-owned start** — Electron main process supervises the backend as a managed
   child, health-gates it, then shows the UI; tears it down on quit/crash.
2. **One-command bootstrap install** — a single script that runs the existing
   client-side install + frontend build, then tells the user to launch the app.

**Explicitly OUT of scope (each its own later cycle)**
- **`.dmg` packaging** — bundling a Python runtime into the Electron app +
  code-signing/notarization. A dedicated later spec, "after the app works" (user
  directive). Until then, "launch the app" = `npm run dev:electron`.
- **A `labmate` CLI** — deferred by the user ("when I want to use the CLI we'll add
  it"). This design keeps the launcher *core* reusable so the CLI is a thin later add.
- **Managing the local model** — the model is always external (see constraints).

## Global constraints (bind every task)

- Reuse `infrastructure/start.sh`'s existing prep logic (SearXNG best-effort,
  MCP-bridge staleness rebuild, env sourcing) — do NOT reimplement it in TypeScript
  (DRY; honors the infra-docs-in-sync rule). The app calls the shell launcher.
- The backend must be **app-owned**: quitting or crashing the app must not leave an
  orphaned backend. This requires a *foreground* run mode (no `nohup`, no pidfile) so
  the OS parent/child relationship is real.
- The model endpoint is read from the app's existing config (`userData/config.json`
  via `loadConfig()` in `electron/main.ts`) and injected into the child's env as
  `GEMMA_BASE`. No hardcoded endpoints.
- Model-unreachable is an **expected, recoverable** state (the model is external), not
  a fatal error: the backend still boots; the app shows a clear banner, not a crash.
- No new services in the stack (no Mongo/Redis/Chroma). SearXNG stays optional +
  best-effort exactly as `start.sh` treats it today.

## Architecture

```
Electron app launch (npm run dev:electron  →  later: the .dmg)
  │
  ├─ main.ts  app.whenReady()
  │     cfg = loadConfig()                 // userData/config.json: { gatewayUrl, gemmaBase, localPort, … }
  │
  ├─ if cfg has an endpoint → BackendSupervisor.start(cfg)   [NEW: electron/backend-supervisor.ts]
  │     spawn("infrastructure/start.sh", ["--foreground"],
  │           { cwd: repoRoot, env: { ...process.env, GEMMA_BASE: cfg.gemmaBase, LOCAL_PORT: cfg.localPort } })
  │     ├─ pipe child stdout/stderr → userData/logs/backend.log  AND  parse step lines → status IPC
  │     ├─ poll  http://127.0.0.1:{localPort}/healthz  until {ok:true}  (bounded: ~60s)
  │     ├─ retain the ChildProcess handle (owner of the lifecycle)
  │     └─ resolve READY  |  reject BOOT_FAILED (child exited early / healthz timeout, with log tail)
  │
  ├─ renderer: StartupScreen  ← consumes supervisor status IPC
  │     "Starting Labmate…" + current step  →  on READY, load the app pointed at cfg.gatewayUrl
  │     error states: BOOT_FAILED (show backend.log tail + retry)  |  MODEL_UNREACHABLE (banner + retry)
  │
  ├─ once READY: probe GEMMA_BASE reachability (non-blocking) → if down, MODEL_UNREACHABLE banner
  │
  └─ app.on('before-quit') → BackendSupervisor.stop()
        SIGTERM child (start.sh --foreground exec'd the python, so the backend dies with it)
        → escalate to SIGKILL after a grace timeout; best-effort SearXNG stop (stop.sh path)
```

## Components (each one responsibility, independently testable)

### 1. `infrastructure/start.sh` — add a `--foreground` mode (only backend change)
- Today `start.sh` does prep (SearXNG best-effort, MCP-bridge staleness rebuild) then
  backgrounds `services.local.main` with `nohup`, writes `.data/pids/local.pid`, and
  returns. That daemon behavior is kept for terminal users.
- **New:** `start.sh --foreground` runs the **same prep**, then `exec python -m
  services.local.main` in the foreground — no `nohup`, no pidfile — so the *caller*
  (Electron now, a CLI later, or a terminal) owns the process. The health-gate loop is
  the caller's job in this mode (Electron polls `/healthz`); prep still fails fast with
  a clear message if MCP-bridge build fails, etc.
- Prep is factored so both modes share it (a `_prep()` function or equivalent), so the
  two modes cannot drift.

### 2. `services/frontend/electron/backend-supervisor.ts` (new)
- **Responsibility:** spawn / own / health-gate / tear down the backend child, and emit
  startup status.
- **Interface (consumed by `main.ts`):**
  - `start(cfg): Promise<void>` — resolves on healthz OK; rejects `BootError` (carrying
    a captured log tail) on early child exit or healthz timeout.
  - `stop(): Promise<void>` — SIGTERM → grace → SIGKILL the child; best-effort SearXNG
    stop; idempotent.
  - `onStatus(cb)` — emits `{phase: 'starting'|'ready'|'boot_failed', step?, logTail?}`.
- **Depends on:** `node:child_process` (already used by `tool-executor.ts`), a healthz
  poller (small `fetch` loop with timeout), and the repo root path.
- Holds NO business logic about *what* the backend does — it only manages the process.

### 3. `electron/main.ts` — wire-in (small, surgical)
- On `whenReady`: load config; if an endpoint is configured, `await supervisor.start(cfg)`
  before creating the main window (window shows the StartupScreen meanwhile); on
  `BootError`, keep the StartupScreen with the error + retry.
- Register `app.on('before-quit', () => supervisor.stop())` (await with a grace cap so
  quit isn't blocked indefinitely).
- No change to the existing onboarding/config path — the supervisor consumes whatever
  `loadConfig()` already returns; onboarding (endpoint capture) is unchanged here.

### 4. Renderer `StartupScreen` (small)
- Consumes supervisor status via the existing IPC/preload bridge. Three visible states:
  **Starting** (spinner + current step text), **BootFailed** (log tail + Retry), and a
  **ModelUnreachable** banner (shows the configured `GEMMA_BASE` + Retry) layered over a
  booted app. No business logic; pure status view.

### 5. `infrastructure/bootstrap-client.sh` (new) — the one-command install
- Runs, in order: `install.sh --client-only` (Node/Python deps + optional SearXNG),
  then the frontend build (`cd services/frontend && npm ci && npm run build:main`).
- Idempotent (delegates to the already-idempotent `install.sh`); prints a final "now
  launch the app" line. This is the "single install" until the `.dmg` phase replaces it.

## Data flow (config → child env)

`OnboardingScreen`/settings write `userData/config.json` (existing) → `loadConfig()` in
`main.ts` (existing) → `BackendSupervisor.start(cfg)` maps `cfg.gemmaBase` →
`GEMMA_BASE`, `cfg.localPort` → `LOCAL_PORT` in the child env → `start.sh --foreground`
sources `local.env` (respecting the passed-in `GEMMA_BASE`/`LOCAL_PORT`, since those are
`${VAR:-default}` in `local.env`) → `services.local.main` binds `LOCAL_PORT` and calls
the remote `GEMMA_BASE`. The renderer connects to `cfg.gatewayUrl`
(`ws://localhost:{localPort}/ws`).

## Error handling

| Situation | Behavior |
|---|---|
| Child exits non-zero before healthz | `start()` rejects `BootError` with captured `backend.log` tail; StartupScreen shows it + Retry. Never a blank window. |
| healthz never OK within timeout (~60s) | Same `BootError` path (timeout reason + log tail). |
| `GEMMA_BASE` unreachable | Backend still boots (it's up); app loads with a **ModelUnreachable** banner (shows URL + Retry). Expected/recoverable — the model is external. |
| Quit while still starting | `stop()` kills the partially-started child cleanly (SIGTERM→SIGKILL grace). |
| App crashes | OS reaps the foreground child (it was a real child, not a daemon) → no orphaned backend. |
| No endpoint configured yet | Skip supervisor; go straight to onboarding (unchanged existing path). |

## Testing

- **`backend-supervisor.ts`** (unit, mocked `child_process` + fake healthz server/stub):
  boots-ok → resolves + `ready`; child-crashes-before-healthz → rejects with log tail;
  healthz-timeout → rejects; quit-mid-start → `stop()` kills the child; `stop()` is
  idempotent and escalates SIGTERM→SIGKILL; env mapping (`cfg.gemmaBase`→`GEMMA_BASE`).
  No model/GPU/network needed.
- **`start.sh --foreground`** (shell smoke): prep runs, then the process is exec'd in the
  foreground (assert no pidfile written, no `nohup`); a `--foreground` with a forced
  MCP-build failure exits non-zero with the clear message. Model-free.
- **`bootstrap-client.sh`** (shell smoke, mocked sub-steps or `--dry-run`): calls
  `install.sh --client-only` then the frontend build in order; idempotent re-run.
- **Manual E2E gate (documented, needs the model box):** with the RunPod pod up and
  `GEMMA_BASE` set in `config.json`, launching the app boots the backend and reaches a
  usable session; killing the app leaves no `services.local.main` process behind. This is
  the one path unit tests can't cover and is run once on the real client.

## Open decisions folded in (no further questions needed)

- SearXNG stays **best-effort/optional**, supervised only as `start.sh` already does it
  (no new Docker-lifecycle burden on the app).
- Health-gate lives in the **caller** (Electron) in foreground mode, matching how a CLI
  would poll too — keeps `start.sh --foreground` a pure exec.
- The supervisor **core is the shell launcher**, so the deferred CLI reuses the exact
  same `start.sh --foreground` + healthz-poll contract with no rewrite.
