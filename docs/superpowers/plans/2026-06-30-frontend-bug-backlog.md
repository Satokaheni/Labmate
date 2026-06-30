# Frontend bug backlog (defer & verify later)

> Captured 2026-06-30 during the local-execution work, to be worked when we circle back to
> frontend polish. Branch where the partial fix lives: `feat/local-execution-phase2`.

## 1. Session continuity — PARTIAL FIX committed, NEEDS LIVE VERIFICATION
**Symptom:** clicking an old chat in the sidebar did nothing (blank); context strip appeared to
reset on each message.
**Fix shipped (`413535e`):** `session.open` now replays a `session.history` frame with the
session's stored turns; the frontend merges them (deduped by turn id); added `ctx-carry` /
`ctx-persist` diagnostic logs in the orchestrator.
**Still TODO — verify live** (pull branch → restart orchestrator → reload frontend):
- Clicking an old chat loads its history (not blank).
- Staying in ONE chat across messages → context strip holds its value (doesn't drop to 0).
- Orchestrator log shows `ctx-carry: session=<same id> carried=<nonzero on 2nd+ msg>`.
- If any of these still fail → debug further (the carry code itself is verified-correct, so
  suspect a deploy/branch mismatch or a frontend display reset).

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
