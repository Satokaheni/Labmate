# Code Intelligence Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Give the Labmate orchestrator cheap, accurate code navigation (callers/callees/impact/context over a tree-sitter knowledge graph) so the LLM stops burning context on raw `fs_read_file` + `exec_run grep` calls, and decide whether the external `agentmemory` sidecar is worth adopting against Labmate's existing MongoDB + Chroma + MemoryConsolidator stack.

**Architecture:** Add a **second MCP server** (a code-intelligence graph server) alongside the existing TypeScript bridge. The current `MCPClientManager` owns exactly one stdio session; it becomes a router (`MCPRouter`) holding N named `MCPClientManager` instances and dispatching `call_tool` by tool-name prefix. A small **tool interceptor** inside the LangGraph `execute_node` rewrites file-read/grep-shaped commands into graph queries before they hit the sandbox. `agentmemory` is **skipped** — Labmate already has the architecture it sells — with two targeted additions to `MemoryConsolidator` to close the only real gaps.

**Tech Stack:** Python 3.12 · asyncio · `mcp` (stdio JSON-RPC) · LangGraph StateGraph · litellm → llama.cpp (Gemma 4 31B) · MongoDB + Chroma + Redis · existing TypeScript MCP bridge. New: one Rust binary (`codegraph` or `tokensave`) run as a child-process MCP server.

---

## Recommendation Summary

| Tool | Recommendation | One-line reason |
|------|----------------|-----------------|
| **codegraph** | **Integrate** (as the code-intelligence MCP server) | TypeScript-native like the rest of the stack, self-contained binary, FSEvents/inotify auto-sync, 4 clean tools — lowest integration friction for the same graph payoff. |
| **tokensave** | **Skip** | Near-identical purpose to codegraph; its headline feature (native Rust `PreToolUse` hook) is Claude-Code-specific and useless inside LangGraph, and a Rust binary adds a toolchain Labmate doesn't otherwise need. |
| **agentmemory** | **Skip** (close gaps in `MemoryConsolidator` instead) | Labmate already has 3-tier memory + triple-stream RRF retrieval + LLM fact consolidation in MongoDB/Chroma; adopting a second sidecar duplicates the stack and violates the single-source-of-truth (transactional outbox) rule. |

---

## Section 1: Tool Recommendations

### 1a. codegraph vs tokensave — pick **codegraph**

These two are functionally the same product: tree-sitter → SQLite knowledge graph, exposed over MCP, sold on "~99% / ~92% token reduction on code navigation." The decision is purely about integration cost and operational fit on Labmate's single-GPU host.

| Criterion | tokensave | codegraph | Winner for Labmate |
|-----------|-----------|-----------|--------------------|
| Runtime | Single Rust binary, no deps | TS binary w/ bundled Node | codegraph — stack is already TS (bridge + skills); no new toolchain |
| Tool surface | 80+ MCP tools | 4 tools (`codegraph_explore`, `_node`, `_search`, `_callers`) | codegraph — 4 tools fit a tool-dispatch LLM (Gemma 4) far better; 80 tools blow up the tool-selection prompt |
| Staleness model | On-demand check, 30 s cooldown per trigger | OS file watcher (FSEvents/inotify) auto-sync | codegraph — push-based; the orchestrator never has to remember to re-index |
| Claude-Code coupling | Native Rust `PreToolUse` hook (Labmate has no such layer) | Plain MCP server, no harness assumptions | codegraph — Labmate replaces the hook with its own interceptor (Section 3) either way; tokensave's marquee feature is dead weight here |
| Build-order fit | Adds Rust to the build | Spawns like any other child-process MCP server (matches CLAUDE.md build step 4) | codegraph |

**The 80-vs-4 tool count is the deciding factor.** Labmate dispatches MCP tools with `thinking_budget_tokens: 0` (CLAUDE.md rule #6 — reasoning OFF for tool selection). Dumping 80 tool schemas into that prompt wrecks selection accuracy and token cost; 4 tools is tractable. codegraph's file-watcher auto-sync also removes an entire failure mode (stale graph because nobody triggered a re-index) that tokensave's 30 s-cooldown on-demand model leaves open.

The marginal benchmark differences (tokensave's "99%" vs codegraph's "~16% cheaper / ~58% fewer calls") are not comparable numbers and not worth weighing against the integration-cost gap.

> **Caveat to verify during implementation:** codegraph is distributed as "TypeScript binary with bundled Node runtime." Confirm it ships a stdio MCP transport (not only HTTP/SSE) and that it can be launched as `command + args` exactly like the bridge (`StdioServerParameters`). If it is HTTP-only, the router in Section 2 needs an HTTP-transport variant. This is the one open question blocking concrete launch-command tasks.

### 1b. agentmemory — **Skip**, supplement `MemoryConsolidator`

Map agentmemory's pitch onto what Labmate already runs (`memory_consolidator.py`, `storage_manager.py`, `spec_memory.md`):

| agentmemory capability | Labmate equivalent today | Verdict |
|------------------------|--------------------------|---------|
| 4-tier: working → episodic → semantic → procedural | Working (Redis cache), Episodic (`EpisodicMemory` over MongoDB `episodes`), Semantic (`SemanticMemory` w/ Zep temporal validity in `memories`). Procedural collection specced in `spec_memory.md` (`procedural` Chroma collection, Voyager pattern). | **Overlap ~90%.** Only procedural is partly unbuilt. |
| Triple-stream retrieval: BM25 + vector cosine + KG, fused via RRF | `spec_memory.md` already specifies BM25 + Chroma dense + RRF + cross-encoder rerank. | **Overlap.** Labmate's design is the same algorithm. |
| LLM compresses raw observations → structured facts | `MemoryConsolidator._extract_memories` + `_self_edit` + `_apply_edits` (Mem0 extract / reconcile / apply). | **Overlap.** Same pattern, already wired to Gemma. |
| Memory decay/strengthen (Ebbinghaus) | **Not present.** Labmate has Zep temporal validity (valid_from/valid_to) but no recency-weighted decay score. | **Genuine gap.** |
| Knowledge-graph stream in retrieval fusion | **Partially absent.** Labmate fuses BM25 + dense, not a memory KG. | Minor gap; low value given Section-2 code-graph already covers code structure. |

**Why skip rather than adopt:**

1. **Single source of truth / transactional outbox (CLAUDE.md rule #7).** Every cross-store write in Labmate goes MongoDB-first with an outbox marker; the `OutboxWorker` is the only writer to Chroma/Redis. Dropping in a second memory sidecar with its own store breaks that invariant — you'd have two systems of record and no atomic write across them.
2. **Tokenizer rule (#3).** agentmemory's compression/token accounting won't use the Gemma SentencePiece tokenizer. Labmate already standardized on `AutoTokenizer` in `memory_consolidator.py`. A sidecar reintroduces tiktoken-style miscounts.
3. **53 tools.** Same blow-up problem as tokensave for the tool-dispatch prompt.
4. It is also listed as a *deprecated M2 dependency* in CLAUDE.md ("Memory: AgentMemory HTTP + Codegraph" under the **Current (M2)** column). Re-adopting it is moving backward in the migration.

**Targeted additions to close the real gaps** (spec'd in Section 4) — both land inside `MemoryConsolidator` / `StorageManager`, no new service:

- **Decay/strengthen score** on semantic facts: store `last_accessed`, `access_count`, and a computed `strength` on each `memories` doc; bump on retrieval, decay by age at consolidation time; let retrieval rank blend `strength` into the RRF fusion.
- **Procedural tier**: finish the `procedural` Chroma collection from `spec_memory.md` (Voyager-style verified-skill store) — only if a concrete need appears; otherwise leave as specced-not-built.

---

## Section 2: Multi-MCP Architecture

### Current state

`MCPClientManager` (`services/mcp-bridge/mcp_client_manager.py`) owns exactly **one** stdio `ClientSession` in one dedicated task (the anyio cancel-scope rule, CLAUDE.md #2). `main.py` builds one via `_build_mcp_params()` and passes it to `CodingOrchestrator(mcp=...)`. The **only** caller of `mcp.call_tool` is `CodingOrchestrator.run_in_sandbox` → `exec_run`.

### Design: keep `MCPClientManager` unchanged, add a thin `MCPRouter` over N of them

Do **not** turn `MCPClientManager` into a multiplexer. Its single-session + single-owning-task design is exactly what satisfies the anyio cancel-scope invariant — preserve it verbatim and run **one instance per server**. Add a router that holds a dict of named managers and dispatches by **tool-name prefix** (auto-routing), with an explicit-server override for ambiguity.

**Why prefix auto-routing, not `call_tool("bridge", name, args)`:** every existing call site (`run_in_sandbox`) passes only `(name, args)`. Tool names are already disjoint by prefix — bridge tools are `fs_*`, `git_*`, `exec_*`; codegraph tools are `codegraph_*`. Auto-routing means **zero call-site changes** for existing code and the LLM never has to know which server owns a tool. Keep an optional explicit `server=` kwarg only as an escape hatch.

```python
# services/mcp-bridge/mcp_router.py  (new file, lives beside mcp_client_manager.py)
from __future__ import annotations
import asyncio
from typing import Any
from mcp import StdioServerParameters
from mcp_client_manager import MCPClientManager, CircuitOpenError  # same dir


class MCPRouter:
    """Owns several single-session MCPClientManagers and routes call_tool by
    tool-name prefix. Each underlying manager keeps the anyio cancel-scope
    invariant (one session, one owning task) — the router never touches a
    session directly.
    """

    def __init__(self, servers: dict[str, StdioServerParameters]) -> None:
        # name -> manager (e.g. {"bridge": ..., "codegraph": ...})
        self._mgrs: dict[str, MCPClientManager] = {
            name: MCPClientManager(params) for name, params in servers.items()
        }
        # tool name -> manager name, built after wait_ready()
        self._route: dict[str, str] = {}

    async def start(self) -> None:
        for m in self._mgrs.values():
            await m.start()

    async def wait_ready(self, timeout: float = 30.0) -> None:
        # Wait for each independently; a slow/optional server must not block the
        # others. Build the routing table from each manager's advertised tools.
        results = await asyncio.gather(
            *(self._wait_one(name, m, timeout) for name, m in self._mgrs.items()),
            return_exceptions=True,
        )
        # results entries are (name, ok); routing table built in _wait_one
        _ = results

    async def _wait_one(self, name: str, m: MCPClientManager, timeout: float):
        try:
            await m.wait_ready(timeout=timeout)
            for t in m.tools:
                self._route[t.name] = name
            return (name, True)
        except asyncio.TimeoutError:
            return (name, False)

    @property
    def tools(self) -> list:
        """Flattened tool list across all ready servers (back-compat with the
        single-manager .tools property main.py logs)."""
        out: list = []
        for m in self._mgrs.values():
            out.extend(m.tools)
        return out

    async def call_tool(
        self, name: str, args: dict[str, Any],
        *, server: str | None = None, timeout: float | None = None,
    ) -> Any:
        target = server or self._route.get(name)
        if target is None:
            # Unknown prefix: fall back to bridge so behavior degrades to today's.
            target = "bridge"
        return await self._mgrs[target].call_tool(name, args, timeout=timeout)

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(m.shutdown() for m in self._mgrs.values()),
            return_exceptions=True,
        )
```

Re-export it from the orchestrator shim alongside the manager:

```python
# services/orchestrator/mcp_client_manager.py  (append)
from mcp_router import MCPRouter  # noqa: F401
__all__ = ["MCPClientManager", "CircuitOpenError", "MCPRouter"]
```

### How the cross-server contract methods behave

- **`wait_ready()`** — `gather` over each manager with `return_exceptions=True`. **A timeout on the codegraph server must NOT kill the bridge** (and vice-versa); the router stays up with whatever became ready, exactly like `main.py` already tolerates a non-ready bridge ("continuing"). The routing table is populated per-server as each becomes ready.
- **`tools`** — flattened list across ready servers, so `len(self._mcp.tools)` in `main.py` still works and now reports the combined count.
- **`call_tool`** — prefix lookup → unknown-prefix falls back to `"bridge"` (preserves today's behavior for any tool the router didn't see). Optional `server=` override for forcing a target.
- **`shutdown()`** — `gather` over all managers; each cancels its own owning task (the existing, correct shutdown).

### `main.py` instantiation changes

`_build_mcp_params()` becomes `_build_mcp_servers()` returning a dict, and the field type changes from `MCPClientManager` to `MCPRouter`. `CodingOrchestrator(mcp=...)` is unchanged because `MCPRouter` is duck-compatible (`call_tool`, `tools`, `wait_ready`, `start`, `shutdown`).

```python
# services/orchestrator/main.py
from services.orchestrator.mcp_client_manager import MCPRouter

def _build_mcp_servers() -> dict[str, StdioServerParameters]:
    servers: dict[str, StdioServerParameters] = {}

    # 1) existing TypeScript bridge (unchanged)
    bridge_cmd = os.getenv("MCP_BRIDGE_CMD", "node")
    default_js = str(Path(__file__).resolve().parent.parent
                     / "mcp-bridge" / "dist" / "index.js")
    servers["bridge"] = StdioServerParameters(
        command=bridge_cmd, args=[os.getenv("MCP_BRIDGE_ARGS", default_js)]
    )

    # 2) code-intelligence server (codegraph) — opt-in via env
    cg_cmd = os.getenv("CODEGRAPH_CMD")           # e.g. "codegraph"
    if cg_cmd:
        cg_args = os.getenv("CODEGRAPH_ARGS", "mcp").split()  # confirm subcmd
        servers["codegraph"] = StdioServerParameters(command=cg_cmd, args=cg_args)

    return servers

# in OrchestratorProcess.run():
self._mcp = MCPRouter(_build_mcp_servers())
await self._mcp.start()
try:
    await self._mcp.wait_ready(timeout=30.0)
    _log.info("MCP servers ready (%d tools)", len(self._mcp.tools))
except asyncio.TimeoutError:
    _log.warning("MCP servers not all ready within 30 s — continuing")
```

Field annotation: `self._mcp: MCPRouter | None = None`. Everything else in `main.py` (`run_in_sandbox` path, shutdown) works untouched. codegraph is **opt-in**: if `CODEGRAPH_CMD` is unset, the router holds only the bridge and the system behaves exactly as today.

---

## Section 3: Tool Interceptor (Labmate's `PreToolUse` equivalent)

Claude Code redirects grep/Explore to the graph with a native Rust `PreToolUse` hook. Labmate has no hook layer, but it has a natural choke point: `execute_node` in `graph.py` generates exactly one bash command per goal (`orch.editor(...)` → `cmd` → `orch.run_in_sandbox(cmd)`). **That single line is the interception point** — no new node needed.

### Where: between command generation and `run_in_sandbox` in `execute_node`

Do not add a separate pre-execute graph node. A graph node forces a new super-step + checkpoint write for what is a pure, synchronous string rewrite. Intercept **in-line** in `execute_node`, right after `cmd = cmd.strip()` and before `obs = await orch.run_in_sandbox(cmd)`.

### What to intercept: a small lookup table, not a parser

Match the generated shell command against a handful of patterns. If it matches and a `codegraph` route exists, rewrite the action into an `mcp.call_tool("codegraph_*", ...)` call. Otherwise fall through to the sandbox unchanged.

```python
# services/orchestrator/tool_interceptor.py  (new file)
from __future__ import annotations
import re
from typing import Any

# (regex on the generated bash cmd) -> (codegraph tool, arg-builder)
# Ordered: first match wins. Keep this table SHORT and obvious.
_RULES: list[tuple[re.Pattern, str, Any]] = [
    # ripgrep / grep for a symbol across the repo -> graph search
    (re.compile(r"""^\s*(rg|grep)\b.*?['"]?(?P<q>[A-Za-z_][\w]+)['"]?\s*$"""),
     "codegraph_search", lambda m: {"query": m.group("q")}),
    # "who calls X" idioms the model tends to emit as grep -> callers
    (re.compile(r"""\bcallers?\s+of\s+(?P<sym>[A-Za-z_]\w+)"""),
     "codegraph_callers", lambda m: {"symbol": m.group("sym")}),
    # reading a whole source file to understand it -> graph context for that file
    (re.compile(r"""^\s*(cat|less|head|tail)\s+(?P<path>\S+\.(py|ts|tsx|js|rs|go))\s*$"""),
     "codegraph_explore", lambda m: {"query": m.group("path")}),
]

# Escape hatch: if the model explicitly wants raw bytes, never intercept.
_RAW_INTENT = re.compile(
    r"#\s*raw\b|--raw\b|\bsed\b|\bawk\b|>\s|>>\s|\btee\b|\bpatch\b", re.IGNORECASE
)


def maybe_redirect(cmd: str, *, codegraph_available: bool) -> tuple[str, dict] | None:
    """Return (tool_name, args) to call instead of running cmd, or None to run
    the command unchanged. Conservative: only redirects read/search-shaped
    commands, never writes/edits/pipes."""
    if not codegraph_available:
        return None
    if _RAW_INTENT.search(cmd):
        return None  # model wants real file content or is mutating — leave it
    for pat, tool, build in _RULES:
        m = pat.search(cmd)
        if m:
            return tool, build(m)
    return None
```

Wire into `execute_node` (`graph.py`), minimal delta:

```python
        cmd = cmd.strip()

        from .tool_interceptor import maybe_redirect
        cg_ready = "codegraph_search" in {t.name for t in (orch.mcp.tools if orch.mcp else [])}
        redirect = maybe_redirect(cmd, codegraph_available=cg_ready)
        if redirect is not None:
            tool, args = redirect
            result = await orch.mcp.call_tool(tool, args)
            text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            obs = {"stdout": text, "stderr": "", "exit_code": 0, "ok": not result.isError}
        else:
            obs = await orch.run_in_sandbox(cmd)

        result_text = obs["stdout"] or obs["stderr"]
```

### Don't-intercept rules (avoid over-eager redirection)

- **Writes/mutations never intercept** — the `_RAW_INTENT` regex bails on `>`, `>>`, `sed`, `awk`, `tee`, `patch`. Graph queries are read-only; a redirected write would silently drop the user's change.
- **Explicit raw-content requests** — if the model writes `# raw` or `--raw`, honor it. This is the equivalent of Claude Code's "user asked for the actual file" carve-out.
- **`codegraph_available` gate** — if codegraph isn't a live route (env unset, server crashed, circuit open), `maybe_redirect` returns `None` and everything runs in the sandbox as today. The interceptor can never make the system *worse* than no-codegraph.
- **First-match-wins, short table** — keep `_RULES` to a handful of obvious idioms. Resist building a shell parser; an unmatched command simply runs normally, which is always safe.

### Optional upgrade (later, not now)

Once the graph is trusted, bias the model *up front* by adding the codegraph tools to the `editor()` system prompt with a one-line instruction ("prefer codegraph_* over grep/cat for navigation"). That reduces the number of commands that need rewriting at all. Defer until the interceptor proves the redirects are landing.

---

## Section 4: Memory Integration

**Recommendation: skip agentmemory** (justified in Section 1b). Labmate's `MemoryConsolidator` + `StorageManager` already implement agentmemory's core ideas on top of the transactional-outbox stack. Below are the two targeted additions that close the only genuine gaps, both inside existing files — **no sidecar, no new service.**

### Gap 1 — Decay / strengthen (Ebbinghaus-style recency weighting)

Labmate has Zep temporal validity (`valid_from`/`valid_to`) but treats every currently-valid fact as equally relevant. Add a recency/usage-weighted `strength` and blend it into ranking.

- **Schema** — in `StorageManager.store_memory`, add to the `memories` doc: `last_accessed` (= `_utcnow()` at write), `access_count: 0`, `strength: 1.0`.
- **Strengthen on retrieval** — new `StorageManager.touch_memories(ids: list[str])`: `$inc access_count`, set `last_accessed = now`, recompute `strength` (e.g. `strength = min(1.0, strength + 0.1)`). Call it from `SemanticMemory.search` after results return.
- **Decay at consolidation** — in `MemoryConsolidator.maybe_consolidate`, before `_self_edit`, apply exponential decay by age: `strength *= exp(-Δdays / HALF_LIFE_DAYS)`. Facts whose `strength` drops below a floor become eligible for `close_memory` (soft delete via `valid_to`), not hard delete — preserves episodic source of truth (rule: MongoDB is canonical).
- **Rank blend** — `search_memories` already returns Chroma distance; fold `strength` into the score (`final = α·(1−distance) + β·strength`). This is the same idea agentmemory's RRF + decay achieves, expressed in Labmate's existing fusion.

### Gap 2 — Procedural tier (only if needed)

`spec_memory.md` already specs a `procedural` Chroma collection (Voyager verified-skill pattern). It is specced-not-built. Build it **only when a concrete consumer exists** (e.g. the orchestrator wants to recall a known-good command sequence). Until then, leave as-is — do not pre-build speculative memory tiers.

### If a future decision reverses this and agentmemory IS adopted

For completeness, were agentmemory ever integrated as the sidecar it's designed to be, the wiring would be: run it as a child-process MCP server registered in `MCPRouter` under `"memory"` (same mechanism as codegraph), capture observations by calling `memory_save` from `execute_node` after each tool result, and run session start/end hooks in `OrchestratorProcess._handle` (start: `memory_recall` to seed context; end: `memory_sessions` to summarize). The 5–8 tools worth exposing — **`memory_smart_search`, `memory_save`, `memory_recall`, `memory_sessions`** plus reconcile/forget — and the rest of the 53 suppressed from the dispatch prompt. **This path is not recommended** because it duplicates the outbox-backed store and reintroduces a non-Gemma tokenizer; it is documented here only so the trade-off is explicit.

---

## Section 5: Implementation Order

Follows CLAUDE.md's "each layer depends on the one before" rule. Each step is independently shippable and leaves the system runnable (codegraph is opt-in throughout).

1. **`MCPRouter`** (`services/mcp-bridge/mcp_router.py`) + shim re-export.
   *Prerequisite:* none — wraps the existing, unchanged `MCPClientManager`.
   *Done when:* router with a single `"bridge"` entry passes the existing `test_mcp_client_manager` behavior (exec_run still routes). No codegraph yet.

2. **`main.py` switch to `MCPRouter`** (still bridge-only).
   *Prerequisite:* step 1.
   *Done when:* orchestrator boots, `len(self._mcp.tools)` logs the bridge's tools, `run_in_sandbox`/`exec_run` unchanged. This de-risks the router before adding a second server.

3. **codegraph as a second MCP server** (env-gated `CODEGRAPH_CMD`/`CODEGRAPH_ARGS`).
   *Prerequisite:* step 2 **and** the open question resolved — confirm codegraph exposes a **stdio** MCP transport and the exact launch subcommand. (Research task below.)
   *Done when:* with `CODEGRAPH_CMD` set, `wait_ready` lists `codegraph_*` tools and a manual `call_tool("codegraph_search", {...})` returns; with it unset, behavior is identical to step 2.

4. **Tool interceptor** (`tool_interceptor.py` + `execute_node` wiring).
   *Prerequisite:* step 3 — needs a live codegraph route to redirect to.
   *Done when:* a grep-for-symbol command is rewritten to `codegraph_search` and returns graph output; a write command (`echo ... > f`) is **not** intercepted; with codegraph absent, all commands run in the sandbox.

5. **Memory: decay/strengthen** (`StorageManager` + `MemoryConsolidator`).
   *Prerequisite:* none on codegraph (independent track); can proceed in parallel with 1–4. Sequenced last only because it is lower priority than navigation.
   *Done when:* facts carry `strength`/`access_count`/`last_accessed`, retrieval strengthens, consolidation decays, and `search_memories` ranking blends strength. Existing memory tests still pass.

6. **Procedural tier** — **deferred.** Build only on concrete demand (Section 4, Gap 2).

---

## Open Research Question (blocks step 3's concrete tasks)

- **codegraph transport + launch contract.** The brief describes codegraph as a "TypeScript binary (self-contained, bundled Node runtime)" exposing `codegraph_explore/_node/_search/_callers`, but does not state whether the MCP transport is **stdio** or **HTTP/SSE**, nor the exact CLI invocation (subcommand, project-path flag, where the SQLite/FTS5 index is written, how it's told which workspace to watch). `StdioServerParameters` + `MCPRouter` assume a stdio child process matching the bridge's model. **Verify against the codegraph README/`--help` before writing step-3 tasks.** If it is HTTP-only, add an HTTP-transport `MCPClientManager` variant (the router design is unaffected; only the per-manager transport changes). Everything in steps 1, 2, 4, 5 is independent of this answer.

---

## Tasks

Tasks for steps 1, 2, 4, 5 are concrete (real files, real code shapes above). Step 3 tasks are intentionally deferred until the transport question is answered — writing a launch command now would be a guess.

### Task 1 — Add `MCPRouter`
- **Files:** create `services/mcp-bridge/mcp_router.py` (code in Section 2); append `MCPRouter` export to `services/orchestrator/mcp_client_manager.py`.
- **Tests:** `services/mcp-bridge/tests/test_mcp_router.py` — (a) router with one fake manager routes `exec_run` to it; (b) `tools` flattens across managers; (c) unknown tool prefix falls back to `"bridge"`; (d) `wait_ready` with one manager timing out still readies the other (`return_exceptions=True`). Mock `MCPClientManager` as in existing `test_mcp_client_manager.py`.
- **Done:** new tests green; existing `test_mcp_client_manager.py` untouched and green.

### Task 2 — Switch `main.py` to `MCPRouter` (bridge-only)
- **Files:** `services/orchestrator/main.py` — replace `_build_mcp_params()` with `_build_mcp_servers()` (dict), change `self._mcp` type to `MCPRouter`, update instantiation + ready-log (code in Section 2).
- **Tests:** boot-path test that `_build_mcp_servers()` returns `{"bridge": ...}` when `CODEGRAPH_CMD` unset, and `{"bridge","codegraph"}` when set.
- **Done:** orchestrator boots bridge-only; `run_in_sandbox` → `exec_run` works exactly as before.

### Task 4 — Tool interceptor
- **Files:** create `services/orchestrator/tool_interceptor.py` (code in Section 3); edit `execute_node` in `services/orchestrator/graph.py` to call `maybe_redirect` before `run_in_sandbox`.
- **Tests:** `tests/services/orchestrator/test_tool_interceptor.py` — table of (cmd → expected redirect | None): `grep Foo` → `codegraph_search`; `cat src/x.py` → `codegraph_explore`; `echo x > f` → None; `sed -i ...` → None; any cmd with `codegraph_available=False` → None.
- **Done:** redirect table passes; `execute_node` path covered by a mocked-`mcp` test asserting a redirected command calls `call_tool("codegraph_search", ...)` and a write command calls `run_in_sandbox`.

### Task 5 — Memory decay/strengthen
- **Files:** `services/orchestrator/storage_manager.py` — add `last_accessed`/`access_count`/`strength` in `store_memory`, add `touch_memories()`, blend `strength` in `search_memories`. `services/orchestrator/memory_consolidator.py` — call `touch_memories` from `SemanticMemory.search`; add age-decay + sub-floor `close_memory` in `maybe_consolidate`.
- **Tests:** `tests/services/orchestrator/test_memory_decay.py` — new fact has `strength==1.0`; `touch_memories` increments `access_count` and `last_accessed`; a fact aged past `HALF_LIFE_DAYS` decays below floor and gets `valid_to` set (not hard-deleted); ranking blend orders a high-strength fact above an equal-distance low-strength one. Use injected clients via `StorageManager.from_clients`.
- **Done:** new tests green; existing memory tests green.

### Task 3 — codegraph second server — **DEFERRED**
Blocked by the Open Research Question (transport + launch contract). Once answered: add the codegraph entry to `_build_mcp_servers()` (env-gated, code shape in Section 2) and an integration test that, with `CODEGRAPH_CMD` pointed at the real binary, asserts `codegraph_*` tools appear in `router.tools` and a `codegraph_search` call returns content.
