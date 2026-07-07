# Local-First Onboarding (Piece 7b) — Design

**Date:** 2026-07-04
**Branch target:** `experimental` (pod version on `main` untouched)
**Piece:** 7b — first of the Piece-7 decomposition (7a cleanup, **7b frontend local default + script seeding visibility**, 7c packaging/add-user CLI).
**Execution:** subagent-driven-development (haiku implement → opus review).

## Goal

Make a fresh LOCAL first-run friction-free: the Electron frontend defaults its gateway
connection to the **local** single-process harness (`ws://localhost:8787/ws`) instead of
a hardcoded RunPod pod URL, and the infra scripts **surface the admin seeding** so the
user knows the credentials to log in with. Together: boot locally → connect → log in,
without editing source files or guessing credentials.

## Non-Goals / Out of Scope

- Wiring `OnboardingScreen` into the app (it stays dead-but-kept for future new-user
  onboarding; this piece only localizes its hint text). — deferred.
- Packaging/installer UX, `GEMMA_BASE` onboarding, add-user CLI — that's **7c**.
- The migration cleanup (run_mode.sh, dead deps, stale docstrings) — that's **7a**.
- No live E2E (frontend/GPU powered off) — verified by `tsc`/tests, not a live boot.

## Context (investigated)

- Gateway URL resolution lives in `services/frontend/src/config.ts:44-53`:
  ```ts
  const ec = window.electronAPI?.config;
  export const WS_URL: string = ec?.isDev
    ? (import.meta.env.VITE_WS_URL as string | undefined) ?? 'wss://<pod-id>.proxy.runpod.net/ws'  // ← hardcoded RunPod fallback
    : ec?.wsUrl ?? '';
  ```
  `config.ts` is imported as `@/config` by `Root.tsx` (and others) → it must exist for the
  build + CI `frontend-typecheck` (`npx tsc --noEmit -p tsconfig.json`).
- `config.ts` is under a standing **never-commit** rule because it holds the user's
  personal dev-pod URL (the working tree has a dirty pod-id edit).
- `services/frontend/.env.example` already documents `VITE_WS_URL=ws://localhost:8787/ws`.
- `OnboardingScreen.tsx` is dead code (zero imports); its hint shows `wss://your-pod.runpod.net/ws`.
- Admin auto-seeds on gateway boot from `ADMIN_EMAIL`/`ADMIN_PASSWORD` (`server.py::_seed_admin`,
  only when `auth_users` is empty AND a password is set); `local.env` now sets dev creds
  (`zach.stallbohm@gmail.com` / `labmate-dev`) but the scripts don't surface this.

## Approach (decided with the user)

Make `config.ts` a **personal, gitignored** file with a committed **`config.example.ts`**
template + a committed **`gateway-defaults.ts`** holding the local default. This honors the
never-commit rule (config.ts becomes untracked; the personal pod URL never enters git) AND
gives fresh clones a local default. Per-machine pod override goes in a gitignored `.env`
(`VITE_WS_URL`), which `.env.example` already documents.

## Components

### 1. `services/frontend/src/gateway-defaults.ts` (NEW, committed)
```ts
/** Default gateway WS URL for a LOCAL single-process harness (services.local.main
 *  on LOCAL_PORT 8787). Override per-machine via VITE_WS_URL (.env, dev) or the
 *  saved userData/config.json (packaged build). */
export const DEFAULT_WS_URL = 'ws://localhost:8787/ws';
```

### 2. `services/frontend/src/config.ts` → untracked + local default
- `git rm --cached services/frontend/src/config.ts` (working copy preserved — the user's
  dirty pod edit is untouched).
- Add `services/frontend/src/config.ts` to `.gitignore`.
- In the working copy, change the WS_URL block to import the default (no personal URL):
  ```ts
  import { DEFAULT_WS_URL } from '@/gateway-defaults';
  export const WS_URL: string = ec?.isDev
    ? (import.meta.env.VITE_WS_URL as string | undefined) ?? DEFAULT_WS_URL
    : ec?.wsUrl ?? DEFAULT_WS_URL;
  ```
  (This edit is NOT committed — config.ts is now untracked. It just makes the local dev copy correct.)

### 3. `services/frontend/src/config.example.ts` (NEW, committed)
A byte-for-byte template of `config.ts` EXCEPT the WS_URL block imports `DEFAULT_WS_URL`
(as in §2) — i.e. the clean, personal-URL-free version. Fresh clones + CI copy this to
`config.ts`. (It necessarily duplicates config.ts's type declarations — inherent to the
`.example` pattern; a header comment says "copied to config.ts on setup; edit config.ts,
not this file, for local overrides — or better, use .env VITE_WS_URL".)

### 4. Auto-provision `config.ts` from the example (build + CI)
- **`services/frontend/package.json`** — add a `predev` and `prebuild` script that copies
  the example when config.ts is absent (cross-platform via a tiny node one-liner):
  ```json
  "ensure-config": "node -e \"const f=require('fs');const p='src/config.ts';if(!f.existsSync(p))f.copyFileSync('src/config.example.ts',p)\"",
  "predev": "npm run ensure-config",
  "prebuild": "npm run ensure-config"
  ```
  (Add `predev`/`prebuild` only if `dev`/`build` scripts exist; otherwise wire `ensure-config`
  into whatever the dev/build entry is.)
- **`.github/workflows/ci.yml`** `frontend-typecheck` job — add, before `npx tsc`:
  ```yaml
          cp -n src/config.example.ts src/config.ts
  ```
  so CI has a config.ts to type-check. (`-n`: don't clobber if present.)

### 5. `OnboardingScreen.tsx` hint localization (kept, not wired)
Update the hint + placeholder to lead with the LOCAL example, RunPod secondary:
- hint: `Looks like  ws://localhost:8787/ws  (local)  ·  or  wss://your-pod.runpod.net/ws  (remote)`
- placeholder: `ws://localhost:8787/ws`
No behavioral change (still dead code); just correct-by-default for when it's later wired.

### 6. Scripts surface the admin seeding (the user's ask)
- **`infrastructure/local/start.sh`** — after the harness reports healthy, echo the seeded
  admin so the user knows how to log in:
  ```bash
  info "admin login: ${ADMIN_EMAIL:-<unset>} (password from local.env ADMIN_PASSWORD)"
  [ -z "${ADMIN_PASSWORD:-}" ] && info "  ⚠ ADMIN_PASSWORD unset — no admin will be seeded; login will be impossible until you set it"
  ```
- **`infrastructure/local/install.sh`** — in the post-install summary, document: set
  `ADMIN_EMAIL`/`ADMIN_PASSWORD` in `local.env`; the admin is auto-seeded on first boot
  (only when the auth store is empty); additional users are created by an admin via
  `POST /auth/users` (7c will add a CLI). Also copy `services/frontend/src/config.example.ts`
  → `config.ts` if the frontend is set up here (else leave to the frontend `predev`).
- Keep the `local.env` comment near `ADMIN_EMAIL`/`ADMIN_PASSWORD`.

## Data Flow

Fresh clone → `npm run dev`/`build` (or install.sh) copies `config.example.ts` → `config.ts`
→ `WS_URL` resolves to `DEFAULT_WS_URL` (`ws://localhost:8787/ws`) unless `VITE_WS_URL`
(.env) or a saved `config.json` overrides → frontend connects to the local gateway →
LoginScreen → user logs in with the seeded admin creds the start.sh banner just printed.

## Testing

- **CI `frontend-typecheck` stays green:** the `cp -n` step provides `config.ts`; `tsc --noEmit`
  passes. Verify `gateway-defaults.ts` + `config.example.ts` type-check and the `@/gateway-defaults`
  path alias resolves (tsconfig `paths` maps `@/*` → `src/*`).
- **Frontend unit tests:** if a vitest test asserts `WS_URL`, update it for the local default;
  add a small test that `DEFAULT_WS_URL === 'ws://localhost:8787/ws'` and that `config.example.ts`
  contains no `runpod`/hardcoded-pod string (guard against reintroduction).
- **Scripts:** `bash -n` + `shellcheck` clean on the edited scripts; a grep confirms start.sh
  echoes `ADMIN_EMAIL` and warns when `ADMIN_PASSWORD` is empty. No live boot (model off).
- **Python suites unaffected** (no Python touched) — but run `tests/services/ws_gateway` to
  confirm nothing references the removed hardcoded URL.
- **Grep gate:** no committed file (post-change) contains a hardcoded `*.runpod.net` gateway
  URL except `OnboardingScreen`/docs as a labeled *remote* example; `config.ts` is untracked
  (`git ls-files` does not list it).

## Risks

- **CI without config.ts:** if the `cp -n` step is missing/misordered, `tsc` fails on the
  missing `@/config` module. Mitigation: the copy is the first line of the type-check step;
  a test asserts the example exists.
- **Stale duplication:** `config.example.ts` duplicates `config.ts` type decls and can drift.
  Mitigation: header comment directs edits to `.env`/config.ts, not the example; the type
  decls rarely change. (A future refactor could split the electronAPI types into a committed
  module so the example only carries the WS_URL block — out of scope here.)
- **Existing users with a saved `config.json`** keep their URL (prod branch unchanged:
  `ec?.wsUrl ?? DEFAULT_WS_URL` only defaults when unset) — no regression.
