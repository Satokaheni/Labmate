# Local-Harness Re-Architecture — Design Spec

> **Status:** design approved at the approach level (strangler migration, "A"). Detailed
> decisions below are recommendations pending the user's review of this spec + each migration
> PR. Brainstormed 2026-07-03. Next: `writing-plans` → subagent-driven execution, piece by piece.

## Goal
Turn Labmate from a **pod-hosted service** (orchestrator daemon + networked Redis/Mongo/Chroma +
tool-delegation back to the client) into a **self-contained local harness** that each user runs
on their own machine — the Claude Code / hermes / openclaw topology. The **only remote dependency
is the model** (llama-server on the user-owned GPU box), reached via the already-pluggable
`GEMMA_BASE` OpenAI-compatible endpoint.

**Target users:** single-user now; 2-3 people who each **download and run their own local harness**
later (each calling the shared GPU). No shared always-on service, no headless-pod agent execution,
no multi-tenant server.

**Why:** the reference harnesses (Claude Code = local files; openclaw = embedded SQLite; hermes =
no DB) all keep state **local + lightweight** and run the harness where the files are. Labmate's
three networked databases + tool-delegation are the source of its hardest bugs (the file-read
mis-route and the continuity reset both live in the infra/delegation tier). Going local **dissolves
that entire bug class by construction**: local tools = no delegation; local state = trivial
continuity.

## Reframe: blow up the *infra tier*, keep the *brain*
Labmate's **reasoning tier is good and infra-agnostic** and must be preserved: the ReAct loop,
the harness-robustness guards, the agentic-fix-loop, the repeat-analysis guard, and the **1413
passing orchestrator tests**. What changes is the **infra tier**: the pod daemon, `ws_gateway`,
Redis queues/streams, Mongo, Chroma, and `request_local_tool` delegation. This is a **strangler
migration** — swap the infra behind the interfaces the brain already uses, one piece at a time,
with both modes coexisting behind a flag until the local path is complete.

## Target architecture
```
  EACH USER'S MACHINE (local)                      REMOTE (user-owned)
  ┌───────────────────────────────────────┐       ┌────────────────────────┐
  │ frontend (Electron)  ── ws://localhost │       │ llama-server :8000     │
  │        │                               │       │ (GPU; OpenAI-compatible│
  │ local gateway (localhost, in-proc/IPC) │──────▶│  GEMMA_BASE endpoint)  │
  │        │                               │ HTTPS └────────────────────────┘
  │ orchestrator (in-process, NO Redis)    │
  │        │  direct local tool calls      │
  │ skills + read_file/write_file/run_tests│ → operate on the user's real files
  │        │                               │
  │ local state:  SQLite  (+ local files)  │ → sessions, chat_turns, checkpoints, telemetry
  └───────────────────────────────────────┘
```
Gone: pod orchestrator daemon, Redis (queues/events/steer/tool-results), Mongo, Chroma-as-service,
`request_local_tool` delegation, capability manifest, workspace-root threading.

## Design decisions (recommendations — flag any you'd change)
1. **State backend → SQLite (embedded, single file per user).** Matches openclaw; structured for
   sessions/turns/checkpoints/telemetry; no server. LangGraph checkpointer
   `AsyncMongoDBSaver → SqliteSaver` (`langgraph-checkpoint-sqlite`). This also **fixes continuity**
   (a local read). *Alternative considered:* JSONL files (Claude Code) — simpler but weaker for the
   relational checkpoint/session data; SQLite preferred.
2. **Chroma → dropped initially; agentic search instead** (grep/read tools + `AGENTS.md`), matching
   the references. If semantic recall is later wanted, use **SQLite FTS** (or `sqlite-vec`), queried
   as an on-demand tool — never a networked service. `memory_search`/`session_search`/codegraph
   adapt to the local index or become no-ops behind a flag.
3. **Redis → in-process.** A single local process needs no broker: goals become direct calls (no
   `labmate:goals` queue); events become an in-process emitter feeding the local gateway/frontend;
   steer becomes a local flag the loop reads; tool-results become direct returns. The Redis Streams
   machinery collapses to function calls.
4. **Tool execution → direct local calls.** `read_file`/`write_file`/`list_dir`/`run_tests` run
   directly on the local FS/shell. Drop `request_local_tool`, the capability manifest, and
   workspace-root threading. **Fixes the file-read bug.** `code-sandbox` stays as an optional
   sandboxed-exec skill but is no longer the test path (local `run_tests` is).
5. **Interface → keep the Electron frontend, point it at a LOCAL gateway.** The frontend already
   speaks WebSocket to a gateway; bind that gateway to **localhost**, co-located with the local
   orchestrator, reading local SQLite. Minimal frontend change. The `cli/` remains a valid entry.
6. **Routing → keep as-is for this migration** (do NOT bundle the flat-tools/routing overhaul).
   Fold in **fix-B's routing change only**: file-access tasks (read/show/list/inspect a file) route
   into the ReAct loop so the local file tools are reached (broaden `ROUTE_EDIT_TO_REACT`'s gate
   from "requires editing" to "requires local tools"). The larger "drop the router for flat native
   tool-selection like the references" is a **separate future decision**, out of scope here.
7. **Model → unchanged.** `GEMMA_BASE` stays a pluggable OpenAI-compatible endpoint (the user's
   remote GPU; or local llama.cpp; or a hosted API). No architectural change.

## Strangler strategy (the safety rail)
Introduce a **`LABMATE_LOCAL_MODE`** config. Each piece adds a local implementation **behind an
interface**, selected by the flag, while the pod path keeps working. **The full orchestrator suite
(1413) stays green after every piece.** Only after all pieces land and local-mode is validated do
we flip the default and remove the pod/Redis/Mongo/Chroma/delegation paths (last step). At no point
is `main` broken.

## Migration decomposition & order (each = one PR, tests green)
0. **Seams:** confirm/extract the interfaces the brain uses for state, events, and tools
   (`StorageManager`, the event emitter, the tool dispatch) so local impls can slot in. Add
   `LABMATE_LOCAL_MODE`.
1. **Checkpointer:** `AsyncMongoDBSaver → SqliteSaver` behind the flag.
2. **Sessions/turns → SQLite** (`StorageManager` local impl). **Folds in fix-A (continuity).**
3. **Chroma → optional/agentic-search** (memory/session/codegraph search adapt or no-op locally).
4. **Redis → in-process** (goals/events/steer/tool-results as direct calls; orchestrator runs as a
   local process, not a Redis consumer). *Largest piece.*
5. **Tools → direct local execution** (drop delegation/manifest). **Folds in fix-B (routing +
   local file tools reached).** **Fixes file-read.**
6. **Local gateway + entry point** (localhost gateway, in-proc orchestrator; frontend → localhost).
7. **Packaging/distribution** (install script so the 2-3 users each run the local harness +
   configure `GEMMA_BASE`); then **flip default to local-mode and remove the pod tier**.

> The two standing bugs (A continuity, B file-read) are **absorbed into pieces 2 and 5** — no
> separate fixes needed; the migration fixes them by construction.

## Testing
- Keep the **1413** orchestrator suite green after every piece (behind the flag, pod path intact).
- Each piece adds **local-mode unit tests** (SQLite checkpointer round-trip, local session/turn
  persistence + continuity load, in-process event/steer, direct tool execution, file-read reaches
  tools).
- A **local end-to-end smoke** once pieces 1-6 land: fresh local harness, attach a real project,
  "read bot.py / edit it / run its tests," multi-turn continuity — all on the local machine against
  the remote `GEMMA_BASE`.

## Non-goals (explicit)
- **Not** the routing/flat-tools overhaul (keep the router; only fold in fix-B). Separate future spec.
- **Not** a rewrite of the reasoning tier (guards, agentic-fix-loop, ReAct loop) — it's preserved.
- **Not** headless-pod / multi-tenant / background-agent operation (dropped per the single-user +
  distribute-local goal).
- **Not** changing the model or `GEMMA_BASE` contract.

## Open decisions for the user (defaults chosen; change if you disagree)
- SQLite vs JSONL for state (recommended: **SQLite**).
- Chroma: drop now vs port to SQLite-FTS (recommended: **drop now**, agentic search; add FTS later
  only if recall gaps show).
- Keep the Electron frontend (recommended: **keep**, point local) vs CLI-first.
- Whether to remove the pod/Redis/Mongo/Chroma code at the end (recommended: **yes**, once local is
  validated) vs keep it flag-gated for a possible future headless mode.
