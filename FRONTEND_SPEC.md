# Labmate Frontend — Build Spec for Claude Code

This document specifies the Labmate desktop frontend (`Labmate.dc.html` is the visual reference / source of truth for layout and interaction) and **everything it needs from the LLM and orchestrator** to function. It is written so an implementer (Claude Code) can build the real app and the orchestrator-side contracts without re-deriving the design.

The prototype is a single HTML "Design Component". The production target is a normal cross-platform frontend (React web app, or the same wrapped in Tauri/Electron for a desktop binary). All data shapes below are framework-agnostic TypeScript interfaces.

---

## 1. What this app is

A chat client for **Labmate** — a local autonomous agent (`Brain → Nervous System → Hands`: Gemma 4 via llama.cpp → MCP bridge → polyglot skills, backed by MongoDB/Chroma/Redis). See the repo `README.md`, `CLAUDE.md`, and `research/llm-harness-research/specs/` for the agent internals.

The frontend is "mission control" for that agent. Unlike a generic chatbot UI it must expose, in real time:

- **Three work modes** — `chat`, `paper`, `code` — each a distinct workspace, like Claude's projects but typed by task.
- **Sessions** — recency-ordered, switchable, persisted.
- **Per-turn reasoning** — the model's `reasoning_content`, collapsed by default.
- **Tool/skill calls** — each expandable to show *why* the model called it, its inputs, and its result.
- **Live context-window accounting** — what is filling the 16,384-token window, by category.
- **Agent subsystem status** — Brain / Nervous System / Hands health and current node.
- **Generated file artifacts** — shown as cards in the chat, previewed/downloaded in the right panel.
- **Debug mode** — a live trace (node transitions, JSON-RPC frames, token accounting, request params) plus a per-call inspector.

---

## 2. Layout (three columns + top bar)

```
┌──────────────────────────────────────────────────────────────────────┐
│ TOP BAR: app mark · session breadcrumb · inference health · debug ⏻   │
├────────────┬────────────────────────────────┬────────────────────────┤
│ LEFT       │ CENTER (conversation)          │ RIGHT (inspector)       │
│            │                                │                         │
│ mode tabs  │ [debug ribbon — debug only]    │ default: file preview   │
│ new session│ user / assistant turns         │   of the active artifact│
│ ──────────│  · reasoning block (collapsible)│   (or empty state)      │
│ CHATS list │  · tool-call group (each row    │ debug ON: live trace    │
│ (scroll,   │    expands → reasoning/in/out)  │   + per-call inspector  │
│  recency)  │  · answer text                  │                         │
│ ──────────│  · file artifact cards          │                         │
│ SYSTEM     │ composer                        │                         │
│  footer    │                                │                         │
│ (compact + │                                │                         │
│  expand)   │                                │                         │
└────────────┴────────────────────────────────┴────────────────────────┘
```

Design tokens (dark "mission control"): background `#0f1115`, rails `#0a0c10`, panels `#13161c`, borders `#20242c`/`#2a2f39`, text `#e6e8ec`/`#939ba7`/`#5e6671`. Accents: blue `#6aa6ff` (Brain/primary), purple `#a78bfa` (Nervous System/reasoning), green `#56c08d` (Hands/success), amber `#e0a458` (warnings/reasoning budget). Type: IBM Plex Sans (UI) + IBM Plex Mono (telemetry, code, identifiers).

### Modes
`chat` (💬), `paper` (📄 writing), `code` (⌘ coding). The active mode determines: which sessions are highlighted/created, the composer placeholder, the default LangGraph node + thinking budget, and which skills the Hands panel lists. Switching mode selects that mode's most-recent session.

---

## 3. Core data model

```ts
type Mode = 'chat' | 'paper' | 'code';

interface Session {
  id: string;
  title: string;            // human title (auto-generated from first turn, editable)
  mode: Mode;
  turnCount: number;
  contextTokens: number;    // current window fill for this session
  updatedAt: string;        // ISO — list is sorted desc by this
  createdAt: string;
}

interface Turn {
  id: string;
  sessionId: string;
  role: 'user' | 'assistant';
  text: string;             // markdown
  createdAt: string;
  // assistant-only:
  reasoning?: Reasoning;
  toolCalls?: ToolCall[];
  artifacts?: Artifact[];
  status?: 'streaming' | 'complete' | 'error';
}

interface Reasoning {
  // From llama.cpp `message.reasoning_content` (server run with --reasoning-format deepseek).
  summary: string;          // short one-liner shown on the collapsed row
  text: string;             // full reasoning, shown when expanded
  node: NodeName;           // which LangGraph node produced it
  tokens: number;           // reasoning tokens used
  budget: number;           // thinking_budget_tokens for this call
  durationMs: number;
}

type NodeName = 'plan_node' | 'execute_node' | 'check_node' | 'reflect_node' | 'chat_node';

interface ToolCall {
  id: string;
  name: string;             // 'ast-repo-map', 'read_file', 'code_search', 'writing', ...
  kind: 'skill' | 'tool';   // skill = MCP child-process skill; tool = builtin bridge tool
  status: 'running' | 'done' | 'error';
  summary: string;          // one-line result shown on the row, e.g. "scanned services/ — 18 files"
  durationMs: number;

  // Detail (lazy-loadable; needed when the user expands the row):
  reasoningWhy: string;     // why the model chose this call at this step
  args: unknown;            // the tool/call arguments (rendered as JSON)
  result: unknown;          // structured result or summary
  trace?: ToolTrace;        // fuller data for the debug inspector (section 7)
}

interface Artifact {
  id: string;
  name: string;             // 'server.ts', 'results-section.md'
  path: string;             // logical path, e.g. 'services/mcp-bridge/src/'
  language: string;         // 'TypeScript', 'Markdown', ...
  mime: string;
  sizeBytes: number;
  lineCount?: number;
  preview: 'code' | 'doc';  // how the right panel renders it
  content: string;          // file body (or a URL to fetch it)
  downloadUrl: string;
}
```

### Context accounting

The frontend must render a live breakdown of the model's context window. Token counts **must** come from the server side using the Gemma SentencePiece tokenizer (`AutoTokenizer.from_pretrained("google/gemma-4-9b-it")`) — never tiktoken (see `CLAUDE.md` §3). Counts are wrong otherwise and the bar will lie.

```ts
interface ContextWindow {
  max: number;              // 16384 (matches llama-server --ctx-size)
  used: number;
  segments: {               // sum(values) === used; order is the stack order in the bar
    systemPrompt: number;
    skillInstructions: number;  // injected SKILL.md instructions for active skills
    conversation: number;
    workingMemory: number;      // RAG / Chroma retrievals injected this turn
    reasoning: number;          // current turn's reasoning_content
  };
  free: number;             // max - used
}
```

The compact view (sidebar footer) shows the segmented bar + `used/max`. Expanding it lists each segment with its token count. A second compact readout sits in the composer status row (just `% used`).

### Agent subsystem status

```ts
interface AgentStatus {
  brain: {
    model: string;          // 'Gemma 4 31B · Q4_K_XL'
    endpoint: string;       // 'llama.cpp :8000'
    state: 'idle' | 'active' | 'error';
    node: NodeName;         // current node
    thinkingBudget: number; // thinking_budget_tokens for current node
  };
  nervousSystem: {
    name: 'MCP bridge';
    transport: 'JSON-RPC 2.0 · stdio';
    state: 'connected' | 'disconnected' | 'error';
    toolsRegistered: number;
  };
  hands: {
    skills: Array<{ name: string; state: 'idle' | 'active' | 'done' | 'error' }>;
  };
  memory?: {                // optional, shown in expanded system view
    mongoMessages: number;
    chromaVectors: number;
    redisQueueDepth: number;
  };
}
```

Node → thinking-budget mapping the Brain panel should reflect (from `CLAUDE.md` §6):

| Node | Model role | `thinking_budget_tokens` |
|------|-----------|--------------------------|
| `plan_node` | architect() | 3000 |
| `execute_node` | editor() | 2048 |
| `check_node` | architect() | 1000 |
| `reflect_node` | architect() | 3000 |
| `chat_node` | direct | ~1000 |
| MCP tool dispatch | direct | 0 (reasoning OFF) |

---

## 3.5 Authentication (login gate)

The app is gated by a login screen (`Labmate Login.dc.html` is the visual reference) — the chat UI never mounts until there is a valid session. Labmate is **local single-user** by default, but the contract below is the same shape a hosted/multi-user deployment needs, so auth is a swap of the identity backend, not a rewrite. **The current mock is username/password only** — SSO is deferred (the provider button was intentionally removed; re-add it later as another `/auth/*` grant type without touching the data model).

```ts
interface AuthUser {
  id: string;
  email: string;
  displayName: string;
  createdAt: string;
}

interface AuthSession {
  token: string;       // opaque bearer; sent on every WS connect + REST call
  user: AuthUser;
  expiresAt: string;   // ISO; client refreshes or re-prompts before this
}

interface Credentials {
  email: string;
  password: string;
  remember: boolean;   // "keep me signed in on this machine"
}
```

### REST

```
POST /auth/login    { email, password }  → 200 { session: AuthSession } | 401 { error: 'invalid_credentials' | 'locked' }
POST /auth/logout   (bearer)             → 204
GET  /auth/me       (bearer)             → 200 { user: AuthUser } | 401
```

- **Local mode**: validate against a single bootstrap credential (env/`config.toml`, hashed with argon2id) and mint a long-lived local token. No network identity provider involved.
- **Lockout**: after N failed attempts, return `locked` with a retry-after; the login screen surfaces it in its error row.

### WebSocket gating

The session socket (section 4) **must authenticate before any other frame**. Connect with the token, then the orchestrator replies `auth.ok` (proceed) or `auth.error` (close):

```ts
// first client→server message on a fresh socket:
type AuthMsg = { type: 'auth'; token: string };

// server→client, prepended to the StreamEvent union:
//   | { type: 'auth.ok'; user: AuthUser }
//   | { type: 'auth.error'; reason: 'expired' | 'invalid' }
```

A `401` on any REST call or an `auth.error` on the socket **drops the user back to the login screen** (clear in-memory token, keep the URL session params for restore after re-auth).

### Token storage & lifecycle

- **Never put the token in the URL** (URL holds only `session`/`mode` — section 8).
- `remember = false` → token in memory only; closing the window requires re-login.
- `remember = true` → persist to **OS keychain** (Tauri/Electron `safeStorage`) for desktop, or an **httpOnly, Secure, SameSite=Strict cookie** for the web build. Never `localStorage` for the token.
- On launch: if a stored token exists, call `GET /auth/me`; success → skip login and mount the app, failure → show login.
- **Logout** clears stored token + closes the socket, then routes to login.

### UI states the login screen must cover

`idle` · `submitting` (button shows a spinner + “Authenticating…”, inputs disabled) · `error` (empty-field validation or `invalid_credentials`/`locked` from the server) · `success` (route to `Labmate.dc.html` with the prior/last `?session&mode`). Enter submits; password has a show/hide toggle; the "local instance reachable · llama.cpp :8000" readout reflects backend health (`GET /healthz`) — a down backend should disable submit.

---

## 4. Real-time protocol (orchestrator → frontend)

The agent loop is long-running and streams. **Transport: a single WebSocket per session** (committed choice for the current local / RunPod, single-orchestrator deployment). WebSocket is preferred over SSE here because the channel is bidirectional — `send`, `cancel`, and `debug.set` flow client→server on the same socket as the server→client event stream, with no proxy/HTTP infrastructure in the path to special-case. Every event carries `{ type, sessionId, turnId?, seq }`. The frontend reduces these into the data model above. The orchestrator is the LangGraph `StateGraph`; emit an event at each node boundary and each token/tool step.

> Scale-out note (deferred — not built yet): if this is ever hosted behind an ingress/proxy or run multi-pod (k8s, etc.), keep WebSocket and add WS upgrade passthrough + generous idle timeouts, plus sticky sessions so a socket stays pinned to the pod that owns the session's loop. Only reconsider SSE for serverless platforms that won't hold persistent connections. None of this applies to the current local/RunPod setup.

```ts
type StreamEvent =
  | { type: 'turn.created'; turn: Turn }                                  // user msg accepted / assistant turn opened
  | { type: 'node.enter'; turnId: string; node: NodeName; thinkingBudget: number }
  | { type: 'reasoning.delta'; turnId: string; text: string }            // streamed reasoning_content
  | { type: 'reasoning.done'; turnId: string; reasoning: Reasoning }
  | { type: 'tool.start'; turnId: string; toolCall: Omit<ToolCall,'result'|'durationMs'|'status'> }
  | { type: 'tool.frame'; turnId: string; toolId: string; frame: JsonRpcFrame }  // debug only
  | { type: 'tool.done'; turnId: string; toolId: string; status: ToolCall['status']; summary: string; result: unknown; durationMs: number; trace?: ToolTrace }
  | { type: 'answer.delta'; turnId: string; text: string }               // streamed final content
  | { type: 'artifact.created'; turnId: string; artifact: Artifact }
  | { type: 'turn.done'; turnId: string; status: 'complete' | 'error' }
  | { type: 'context.update'; window: ContextWindow }                    // push whenever it changes
  | { type: 'agent.status'; status: AgentStatus }                        // push on subsystem change
  | { type: 'session.updated'; session: Session };                       // title/turnCount/updatedAt changed
```

Notes:
- `reasoning.delta` and `answer.delta` are separate because llama.cpp separates `reasoning_content` from `content`. Do not merge them; the UI renders them in different places.
- `context.update` should fire at least at `node.enter`, after each `tool.done`, and at `turn.done`. The bar animating up as the turn progresses is intentional.
- `tool.frame` is only sent while debug mode is active for that session (the frontend signals this — section 8). Never log JSON-RPC to stdout in any MCP server; these frames come from the bridge's structured channel (`CLAUDE.md` §1).

### Frontend → orchestrator

```ts
type ClientMsg =
  | { type: 'send'; sessionId: string; mode: Mode; text: string }
  | { type: 'session.new'; mode: Mode }
  | { type: 'session.open'; sessionId: string }
  | { type: 'session.rename'; sessionId: string; title: string }
  | { type: 'debug.set'; sessionId: string; enabled: boolean }   // gates tool.frame emission
  | { type: 'cancel'; sessionId: string; turnId: string };       // interrupt the running loop
```

REST equivalents (if not all over the socket): `GET /sessions`, `GET /sessions/:id/turns`, `GET /artifacts/:id` (download), `POST /sessions`, `PATCH /sessions/:id`.

---

## 5. Component → data mapping

| UI element | Source | Behavior |
|---|---|---|
| Mode tabs | local state | switch mode → select most-recent session of that mode (`session.open`) |
| New session button | `session.new` | label reflects current mode noun |
| Chats list | `Session[]` | sorted by `updatedAt` desc, **no date grouping**; clicking opens + moves to top (server bumps `updatedAt`) |
| Session breadcrumb (top bar) | active `Session.title` | — |
| Inference health pill | `AgentStatus.brain.endpoint/state` | green dot when reachable |
| Reasoning block | `Turn.reasoning` | collapsed → `summary` + node + `tokens/budget`; expanded → `text` |
| Tool-call row | `ToolCall` | dot color by `kind`/`status`; shows `name`, `summary`, `durationMs`; click expands **and** sets the debug "selected call" |
| Tool-call expansion | `ToolCall.reasoningWhy/args/result` | "LLM reasoning" + Input (args JSON) + Result |
| Answer | `Turn.text` | markdown; streamed via `answer.delta` |
| Artifact card | `Turn.artifacts[]` | shows name/lang/size; "Preview" sets right panel; ⤓ = download |
| Right panel (default) | active `Artifact` | code view (line-numbered) or doc view; Download button |
| Right panel (empty) | — | shown when the active session has produced no artifact |
| System footer (compact) | `AgentStatus` + `ContextWindow` | Brain/MCP/Hands one-liners + segmented bar + `% used` |
| System footer (expanded) | `ContextWindow.segments` | per-segment token list |
| Composer status row | mode + `AgentStatus.brain.node` + `ContextWindow` | node chip, thinking budget, `% context` |
| Debug toggle | `debug.set` | swaps right panel to live trace; gates `tool.frame` |

---

## 6. Modes — concrete differences

These drive copy and which subsystems light up; the orchestrator decides the actual graph path.

- **code** — default node `plan_node` (budget 3000). Typical skills: `ast-repo-map` (skill), `read_file` (tool), `code_search` (skill, codegraph/Rust), `code_tool`. Artifacts: source files (`preview: 'code'`).
- **paper** — default node `reflect_node` (budget 3000). Skills: `writing` (IMRaD), `cite-validate`, `critique` (adversarial). Artifacts: `.md`/`.docx` sections (`preview: 'doc'`). See `spec_writing_skills.md`.
- **chat** — default node `chat_node` (budget ~1000). Skills: `memory-search` (Chroma), `web-fetch`. Usually no artifacts → right panel empty state.

---

## 7. Debug inspector (per-call trace)

When a tool row is clicked, the frontend records it as the **focused call** and, in debug mode, the right-panel inspector shows its full trace. Provide this via `ToolCall.trace` (lazily, on `tool.done`, or fetchable by `toolId`):

```ts
interface ToolTrace {
  node: NodeName;
  server: string;              // spawned process, e.g. 'skills/ast-repo-map · TS'
  tokens: { reasoning: number; args: number; result: number };
  timing: { queueMs: number; execMs: number; totalMs: number };
  frames: JsonRpcFrame[];      // the actual request/response over the bridge
}

interface JsonRpcFrame {
  dir: 'out' | 'in';           // → request / ← response
  method?: string;             // 'tools/list', 'tools/call'
  payload: unknown;            // params or result; rendered as JSON
  ts: string;
}
```

The debug panel also shows session-level trace independent of focus: **node transitions** (timeline of `node.enter` with token deltas), **JSON-RPC frames** (recent), **token accounting** (`prompt_in / reasoning / completion / throughput`), and **request params** (`model`, `thinking_budget`, `temperature`, `top_p`, `ctx_size`). Source these from the stream and the inference request the orchestrator sent.

---

## 8. State, persistence, and lifecycle

- **Session position**: keep the open `sessionId` and `mode` in the URL (`?session=…&mode=…`) so reloads restore the view.
- **Debug flag**: per-session; send `debug.set` on toggle so the bridge starts/stops emitting `tool.frame` (avoids overhead when off).
- **Optimistic send**: on `send`, append the user `Turn` immediately, then open the assistant turn on `turn.created`.
- **Cancellation**: the composer's send button becomes a stop button while `status === 'streaming'`; emit `cancel`.
- **Reconnect**: on socket drop, re-`session.open` and reconcile by `seq`/`turnId` (the orchestrator persists turns via the MongoDB checkpointer, `CLAUDE.md` §8 — replay missed events from last `seq`).
- **Never** block first paint on data: render the shell, then fill from the stream.

---

## 9. Hard rules carried over from the agent (don't violate in the UI/BFF layer)

1. **Token counts use the Gemma tokenizer**, server-side. The context bar is meaningless with tiktoken.
2. **Reasoning and answer are distinct fields** (`reasoning_content` vs `content`). Render separately.
3. **stdout is sacred** in any MCP server — debug frames come from a structured side channel, not stdout scraping.
4. **`thinking_budget_tokens` is always set explicitly** per call; surface it in Brain status + debug params (post-April-2026 llama.cpp hangs if unset).
5. Memory tiers are **MongoDB (sessions/messages) · Chroma (vectors) · Redis (queues)**; the optional memory readout maps to these.

---

## 10. Build order (suggested)

1. **Shell + design tokens** — three-column layout, top bar, empty states (static).
2. **Session list + open/new** — REST + `session.*` events.
3. **Turn rendering** — user/assistant, markdown, streamed `answer.delta`.
4. **Reasoning block** — `reasoning.*` events, collapse/expand.
5. **Tool-call group** — `tool.*` events, expand → why/in/out.
6. **Context bar + system footer** — `context.update`, `agent.status`.
7. **Artifacts** — cards + right-panel preview/download.
8. **Debug mode** — ribbon, live trace, per-call inspector, `debug.set` gating.
9. **Modes** — wire copy/node/skill differences per mode.
10. **Resilience** — URL state, reconnect/replay, cancel.

The HTML prototype (`Labmate.dc.html`) is the pixel/interaction reference for steps 1–9; match its information hierarchy, not necessarily its sample data.
