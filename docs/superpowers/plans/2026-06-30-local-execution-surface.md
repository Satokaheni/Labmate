# Brief: Move Labmate's file-discovery surface client-side (local-execution architecture)

You are implementing a cross-cutting architecture change in the Labmate repo. Read this
whole brief, then produce a short plan and confirm the decision points before writing code.
Deliver in two phases (Phase 1 first, fully green, before Phase 2).

## Background (already investigated — trust these findings)
Labmate is meant to work like Claude Code / hermes / openclaw: the model + orchestrator run
on the server (RunPod), but the user's repo lives on their MACHINE (the frontend's local
workspace). Tool calls that touch files must execute on the client and stream results back —
the server must never hold or read the user's repo.

The migration is HALF DONE. Confirmed by reading the code:
- DONE: `LOCAL_TOOL_NAMES = {"read_file","write_file","list_dir"}` (services/orchestrator/
  local_tools.py:30) already round-trip to the client. The protocol (mirror it for every new
  client-side tool): orchestrator `request_local_tool()` (local_tools.py:101) emits a
  `tool.request` event → ws_gateway relays to the frontend → frontend executes against its
  local workspace → frontend returns a `tool.result` WS frame → ws_gateway `write_tool_result`
  (services/ws_gateway/server.py:227) writes to Redis stream `labmate:tool-results:<task_id>`
  → `request_local_tool` blocks on `XREAD` and matches `tool_request_id`. Dispatch branch:
  coding_orchestrator.py:996 (`elif name in LOCAL_TOOL_NAMES`). Schemas advertised to the
  model: prompt_assembler.py `_static_tail_schemas()` (line 98).
- NOT DONE: every tool that DISCOVERS code still runs on the pod against `self.workspace`
  (= `/workspace`):
    * run_bash             — coding_orchestrator.py:1041 → self.mcp exec_run, cwd=self.workspace, 30s cap
    * code_semantic_search — coding_orchestrator.py:1116 → self.codegraph_mcp.call_tool(
                             "code_semantic_search", {query,k}) (pod Chroma index)
    * run_tests            — coding_orchestrator.py:1079 → skill_router code-sandbox (pytest on pod)
    * call_skill_tool      — coding_orchestrator.py:944 → skill_router repo-reading skills (ast-search,
                             ast-repo-map, repo-fault-localize, code-review, test-gen) read the pod repo

This is why a "find where X is handled" query fails: the model reaches for code_semantic_search
(empty pod index) then run_bash grep (greps 4.4GB on the pod → 30s timeout), and never falls
through to the client-side read_file because it can't LOCATE the file — discovery is on the
wrong machine.

CodeGraph note: `services/codegraph_embedder/` is only a CONSUMER — indexer.py:55 reads a
pre-built `.codegraph/codegraph.db` (`SELECT … FROM nodes`). The builder is the EXTERNAL
CodeGraph CLI (v0.9.9), which in this architecture must run on the CLIENT (where the files
are). So semantic search done right == a client-side query against the client's CodeGraph,
not a pod index.

Skill note: repo-reading skills do raw local filesystem I/O rooted at a `path` arg
(e.g. services/skills/ast-search/searcher.py:84 `Path.rglob`, :97 `read_text`). They run as
SkillRunner subprocess MCP servers (services/skill_runner/) on the pod. "Client-side skills"
therefore means running the skill RUNTIME where the files are — not patching every open().

## The invariant you are enforcing
When a local-tool client is attached (task has a connected frontend; `self.redis` present),
NO model-driven tool call may read, write, search, test, or exec against the pod's
`/workspace`. The pod filesystem is never the target. When NO client is attached, existing
pod-side behavior remains as a fallback (don't break headless/eval paths).

## Capability handshake (build this FIRST — both phases route off it)
On client attach, the frontend declares its capabilities (e.g.
`{ search_files: true, run_tests: true, codegraph: false, skills: false }`). Persist this per
task. The orchestrator advertises/gates tools based on it: a tool whose execution requires a
capability the client lacks is either gated off or falls back to the pod path. This replaces
ad-hoc "is self.redis set" checks with one explicit capability gate, and is what lets a tool
move from "gated off" (Phase 1) to "client-routed" (Phase 2) without touching the model's
mental model. Keep the advertised tool list deterministic per goal (prefix-cache stability,
prompt_assembler.py:189) — capabilities are known at goal start, so resolve them once.

## Phase 1 — make find → read → edit → verify run on the client
Where a decision is called out, choose, justify briefly, and note it in your plan BEFORE coding:

1. `search_files` — new client-side tool (ripgrep over the frontend workspace). Add to
   LOCAL_TOOL_NAMES + a schema in _static_tail_schemas() (params: query[regex], path?, glob?,
   max_results?; description steers the model to use it FIRST to locate code). Rides the
   existing local-tool dispatch branch — no new orchestrator dispatch code. Frontend handler
   mirrors the read_file handler.
2. run_bash — DECISION: when a client is attached, either (a) route bash to the client
   (security: arbitrary shell on the user's machine — gate explicitly), or (b) drop run_bash
   from advertised tools and force discovery through search_files/list_dir. Recommend (b) for
   Phase 1 unless you justify (a).
3. run_tests — when a client is attached, run the project's test command in the frontend
   workspace via a local-tool round-trip; keep the pod code-sandbox path as the no-client fallback.
4. code_semantic_search — when a client is attached AND the client lacks codegraph capability,
   gate it OFF (rely on search_files+read_file). Do not let it hit the pod index when a client
   is attached. (Client-routed version is Phase 2.)
5. Repo-reading skills — when a client is attached AND the client lacks skill capability, gate
   them so the model prefers client-side primitives.

Phase 1 acceptance:
- With a client attached (search_files+run_tests caps, no codegraph/skills), "find where
  WebSocket auth is handled, show the token-verify function" resolves via search_files →
  read_file against the FRONTEND workspace, zero pod-/workspace access in the trace. Ground
  truth for a sanity check: services/ws_gateway/auth.py:101 `verify_token`.
- Regression test: no tool dispatch in _run_react_loop targets self.workspace while a client
  is attached.
- request_local_tool round-trip test for search_files (mirror existing read_file/write_file tests).
- Suite green: PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q

## Phase 2 — true client-side discovery (semantic search + skills)
Only start after Phase 1 is merged and green.

P2-A. Client-side semantic search. The frontend hosts/queries the local CodeGraph daemon
   (the external CodeGraph CLI v0.9.9 — same one whose socket the Mac already runs). Make
   `code_semantic_search` a client-routed tool: when the client declares `codegraph: true`,
   the orchestrator forwards `{query, k}` over the local-tool round-trip (same Redis/ws_gateway
   seam) to a frontend handler that queries the local daemon and returns ranked
   `{file_path, name, kind, start_line, snippet}` hits. Retire the pod-side codegraph_embedder
   + Chroma indexing path to a no-client-only fallback (do NOT index the pod when a client is
   attached). Document the client setup: user installs CodeGraph and runs its init/index in
   their workspace; the frontend points at `<workspace>/.codegraph/daemon.sock`.

P2-B. Client-side skill execution. Repo-reading skills must run where the files are.
   DECISION per skill class:
   - Pure-FS-walk skills (ast-search, ast-repo-map, repo-fault-localize): run the SkillRunner
     subprocess in the FRONTEND environment against its workspace (preferred), OR — interim —
     a file-materialization shim where the orchestrator ships the scoped files down from the
     client into a pod temp dir, runs the skill, discards. State which you chose and why;
     materialization does not scale to whole-repo rglob, so it's only an interim for scoped calls.
   - Content-operating skills (code-review, test-gen, critique): these operate on passed-in
     content, so they can stay pod-side IF fed client-fetched content (read_file/search_files
     results) instead of reading the repo themselves. Verify each actually takes content vs.
     a path before keeping it server-side.
   Whichever path: when a client declares `skills: true`, call_skill_tool for repo-reading
   skills must not read the pod repo.

P2-C. Decommission pod discovery infra. When client execution is the default, the pod-side
   codegraph_embedder service and WORKSPACE_PATH-rooted file access exist ONLY as the
   no-client/headless fallback. Don't delete the fallback (eval + headless paths use it);
   do ensure it's never reached while a capable client is attached.

Phase 2 acceptance:
- With a client declaring codegraph+skills, the same WS-auth query can be answered via
  code_semantic_search routed to the client's CodeGraph (file_path points into the client
  workspace), and an ast-search/ast-repo-map call returns results computed from the client's
  files — zero pod-/workspace access in the trace.
- No-client (headless/eval) path still works against the pod (fallback intact); existing
  eval/seq_ab and tests/ suites stay green.

## Constraints / gotchas (from CLAUDE.md — non-negotiable)
- MCP servers: never write to stdout (JSON-RPC). stderr only.
- Tokenizer: transformers AutoTokenizer, never tiktoken.
- Redis: Streams (XADD/XREAD), redis>=5,<6.
- PromptAssembler prefix must stay byte-stable per goal (prefix cache) — any added tool schema
  must be deterministic, fixed-order, no time/uuid/randomness (prompt_assembler.py:189).
- Tests live in tests/ mirroring services/; @pytest.mark.asyncio on async tests; assert
  structure not literal text.
- Per-task implementation loop (CLAUDE.md "Implementation Workflow"): implement → review/judge
  → fix until pass; run the live skill contract suite (tests/live, LIVE_TESTS=1) when any skill
  execution path changes; rebuild node skills (npm run build) before testing them.

## Deliverable shape
First reply with: (1) the capability-handshake design, (2) the decision calls for Phase 1
items 2 & 4 and Phase 2 item B, (3) a file-by-file change list with the exact anchors above,
(4) the new/changed tests, (5) which deliverables are frontend-branch vs orchestrator-branch
(the frontend handlers — search_files, run_tests, codegraph query, possibly skill hosting —
must be called out so they're not lost). Then implement Phase 1 to green before touching
Phase 2. Report every gate/route decision and every pod-fallback you preserved.
