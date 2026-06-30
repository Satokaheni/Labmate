# Brief: Wire the UI mode (Chat / Paper writing / Coding) into actual backend behavior

> **Status: DEFERRED.** Pick this up AFTER the local-execution work (Phase 1 + live test,
> likely Phase 2) is merged — the mode hint and the client manifest both influence tool
> advertisement, so they should be designed together, not in conflict. This is a brief to
> brainstorm into a spec later, not a ready-to-execute plan.

## Problem (current state — verified 2026-06-30)
The mode tabs (Chat / Paper writing / Coding) are **cosmetic**. They change UI labels and the
displayed thinking budget (`MODE_META`), but do NOT change how a task is processed:
- The frontend **hardcodes `mode: 'chat'`** on every send (`useLabmateWS.ts:474`) — the active
  tab is ignored on the send path.
- `mode` is **never put in the goal payload** (`push_task` has no mode field), so it never
  reaches the orchestrator.
- Nothing in `main.py` / `graph.py` branches on a mode — confirmed by grep.

So a "write the results section of my paper" task and a "fix this bug" task are routed
identically regardless of the tab. The mode is a missed signal.

## Goal
Make the mode a real routing hint that biases (not hard-forces) behavior:
- **Coding** → bias toward code tools/skills (search_files, read/write, run_tests, code-review,
  test-gen, ast-* ); with a client attached, prefer local primitives (already steered).
- **Paper writing** → bias toward the writing/academic skills (academic-writing, citation-check,
  critique, paper-rag, arxiv-prep, results-analysis).
- **Chat** → general/default; lightest path, conversational; candidate for skipping heavy gates.

## Sketch of the wiring (file-by-file — flesh out in the spec)
1. **Frontend** — send the ACTUAL active mode, not hardcoded `'chat'` (`useLabmateWS.ts` `send`
   should read the current mode; thread it from `ChatScreen` state).
2. **ws_gateway** — `push_task` gains a `mode` kwarg; include it in the goal payload (mirror how
   `client_capabilities` was threaded in the local-execution work).
3. **main.py `_handle`** — parse `payload.get("mode")`; expose it to routing (a contextvar like
   `client_context`, or pass-through into the graph state).
4. **graph / orchestrator** — consume the mode as a BIAS:
   - feed a one-line mode hint into `assess_ambiguity` + the ReAct system prompt (gated, like the
     `CLIENT_PRIMITIVES_STEER` clause), e.g. "This is a coding session — prefer code tools."
   - optionally weight/filter the advertised skill catalog by mode (soft preference, keep prefix
     deterministic per goal).
   - optionally wire `MODE_META`'s per-mode thinking budgets through to the architect calls.

## Decisions to make in the spec
- **Soft bias vs hard filter:** prompt hint + catalog weighting (safer, recommended) vs hard-
  removing off-mode skills (riskier). Default to soft.
- **Interaction with the local-execution manifest/steer:** mode hint must COMPOSE with the
  client-primitives steer, not contradict it. Resolve precedence (client-attached file work
  always uses local tools regardless of mode).
- **Prefix-cache stability:** the mode is known at goal start, so any mode-dependent prompt/tool
  changes must be folded into the once-per-goal prefix (deterministic) — same constraint the
  manifest work respected.
- **Thinking budgets:** whether to route `MODE_META` budgets to the backend or keep them
  UI-only.

## Acceptance (rough)
- A coding task in Coding mode visibly prefers code tools/skills; a writing task in Paper mode
  routes to the writing skills; Chat mode stays general. No regression when mode is absent
  (headless/CLI/Discord default to today's behavior).

## Dependencies / ordering
Do AFTER local-execution Phase 1 is merged + live-tested. Ideally after Phase 2 (manifest + steer
settled), since mode and the manifest both shape tool advertisement and should be unified, not
layered ad-hoc. See [[project-local-execution-surface]] context.
