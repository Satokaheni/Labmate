# Frontend bug backlog (defer & verify later)

> Captured 2026-06-30 during the local-execution work, to be worked when we circle back to
> frontend polish. Branch where the partial fix lives: `feat/local-execution-phase2`.

## 1. Session continuity — clicking an old chat STILL does nothing (fix `413535e` did NOT resolve it)
**Symptom:** clicking an old chat in the sidebar does nothing (no switch / blank). Re-confirmed
broken AFTER the fix below.
**Fix shipped (`413535e`) — insufficient:** `session.open` replays a `session.history` frame with
the session's stored turns; frontend merges them (dedup by turn id); added `ctx-carry`/`ctx-persist`
diagnostic logs. This addressed history-replay, but the click is still dead.
**Code wiring VERIFIED CORRECT (so the bug is elsewhere):**
- `openSession` IS passed Root → ChatScreen (`Root.tsx:104`) → Sidebar `onOpenSession` → the
  session `<button>` `onClick`.
- `openSession(sid)` dispatches `SET_ACTIVE_SESSION` (reducer updates `activeSessionId` in phase
  'ready') + sends a `session.open` frame.
- The session store is APP-LEVEL (`server.py:374`, shared via `app.state.store`), so old sessions'
  turns persist across connections; `session.open` replays them.
**Debug leads for the later fix (in order):**
1. **Rule out a stale frontend first** — hard-reload / rebuild the Electron app and retest; the
   test may have run pre-`413535e` code.
2. If still dead: add temp logging in `openSession` + the session-item `onClick` — does the click
   fire? does `activeSessionId` actually change in state? (Suspect the click handler on the
   converted `<button>`, or the `activeSessionId = state.activeSessionId ?? sessions[0]?.id`
   render fallback masking the change.)
3. Then confirm the `session.history` frame arrives and the turns render under the new
   `activeSessionId` filter.
**Also verify (same fix):** staying in ONE chat across messages → context strip holds its value;
orchestrator log shows `ctx-carry: session=<same id> carried=<nonzero on 2nd+ msg>`.

## 2. Context strip resets per message — likely a SYMPTOM of #1
**Hypothesis:** because old chats didn't reload, the user kept starting NEW chats, and each new
chat's first message correctly shows 0 (no prior context to carry). Fixing #1 should resolve
this. **Verify after #1.** If it genuinely resets *within a single stable session* → real bug
(re-investigate; carry is keyed on a stable `session_id`, so look at the frontend `contextWindow`
handling and whether the orchestrator deploy actually has commit `4772182`).

## 3. Skills chip possibly unbounded — likely the PANEL, not the chip
The chat **chip** dedupes + caps distinct skill names (`[...new Set(...)]` in `ChatScreen.tsx`,
present on the branch). The **`View →` panel** intentionally shows EVERY tool call in order (the
full timeline). **Confirm which is unbounded.** If the *chip line* itself grows without bound →
real regression, fix. If it's the panel → working as designed (chip = summary, panel = full log).

## Related future-work (separate docs, cross-referenced)
- [[wire-ui-mode-to-behavior]] — make the Chat/Paper/Coding tabs actually affect routing.
- Token-cost reduction (`2026-06-30-token-cost-reduction.md`) — incl. real per-segment context
  accounting, which would also make the context strip's segment breakdown accurate.

> NOTE: these are FRONTEND/UX bugs, distinct from the local-execution **skill-rooting** work
> (the `tsconfig must be an absolute path` failures) — that's tracked in the Phase-2 plan as
> dispatch-side path resolution, not here.
