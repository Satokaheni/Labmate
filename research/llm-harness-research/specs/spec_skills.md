# Skills Layer Spec (Hands)

**Labmate — Skills Layer Engineering Specification**
Version 0.1 | 2026-06-15

---

## 1. Overview

The Skills Layer is the effector surface of Labmate: the subsystem through which the agent
acquires, activates, and exercises capabilities beyond raw text generation. It is called "Hands"
because it is how the agent reaches out and acts on the world.

Skills in Labmate are defined by two complementary mechanisms:

1. **SKILL.md files** — human-authored (or agent-authored) markdown files with YAML frontmatter
   that describe a skill's purpose, trigger, and metadata. The SkillRunner discovers and exposes
   them to the model as a lazy, progressive context layer.

2. **Child-process MCP servers** — long-lived subprocesses that implement executable skill logic
   as MCP (Model Context Protocol) servers. The SkillRegistry spawns, supervises, and routes calls
   to these processes over JSON-RPC 2.0 via stdin/stdout.

Three built-in AST skills ship as first-party MCP servers: a tree-sitter parsing and repo-map
skill, an ast-grep structural search/rewrite skill, and a ts-morph TypeScript semantic refactor
skill. These give the agent structured, language-aware code intelligence without relying on
the model to parse source text.

**Primary design goals:**

- **Polyglot**: Skills may be written in TypeScript, Python, or Rust. The MCP protocol is the
  lingua franca; each language uses its native schema mechanism (Zod, Pydantic, schemars) to emit
  JSON Schema. Language is a pure implementation detail.
- **Process-isolated**: Each skill runs as its own subprocess. A crashing skill cannot take down
  the agent loop.
- **Context-efficient**: Skill bodies load lazily, on demand. The model sees only a compact
  catalog (name + one-line description) until it explicitly requests a skill.
- **Supervised**: Every skill subprocess is health-monitored. Crashes trigger exponential-backoff
  restart. Every tool call has a per-call timeout.

---

## 2. Architecture

### 2.1 SKILL.md Format Standard

SKILL.md is the discovery artifact for instruction-bearing skills. It consists of:

- **YAML frontmatter** (between `---` delimiters): machine-readable metadata consumed at discovery
  time. Only this section is read during the startup scan.
- **Markdown body**: the human-readable (and model-readable) skill instructions, loaded lazily
  when the model requests activation.

The SkillRunner treats SKILL.md files as the authoritative index of available capabilities. It
does not require or read any other manifest; the frontmatter IS the manifest for instruction
skills.

### 2.2 SkillRunner: Three-Tier Progressive Disclosure

The SkillRunner operates in three stages that mirror the Claude Code / superpowers architecture:

**Stage 1 — Discovery (eager, cheap):** At harness startup the SkillRunner recursively scans
skill root directories in strict descending priority:

```
project root  (./.claude/skills/ or ./skills/)       <- highest priority
personal root (~/.claude/skills/)
bundled root  (<harness>/skills/)                     <- lowest priority
```

For each `SKILL.md` found, only the YAML frontmatter is parsed. No body is read. The result is
an in-memory catalog: `dict[name -> SkillMeta]`. On name collision, the highest-priority tier
wins and a shadowing warning is logged.

**Stage 2 — Catalog injection:** The catalog (name + one-line description) is rendered into the
Gemma 4 system prompt. A single meta-tool `load_skill(name: str)` is exposed via
`apply_chat_template(tools=...)`. The model can see what skills exist and what each does, but
no body tokens have been consumed.

**Stage 3 — Lazy activation:** When the model determines a skill is relevant it emits a
`load_skill` tool call. The SkillRunner resolves the name, validates path confinement, reads and
parses the body, and returns it as the tool response. The body enters context for subsequent
turns. Helper assets referenced by the body are loaded on a third tier, only when the body
instructs the model to read or execute them.

### 2.3 SkillRegistry: Child-Process MCP Servers

The SkillRegistry manages the lifecycle of executable skill subprocesses. Each skill is a
long-lived MCP server communicating over stdin/stdout via JSON-RPC 2.0. The registry:

- Spawns one persistent subprocess per skill at registration time (never spawn-per-call).
- Runs the MCP `initialize` handshake and `tools/list` once, caching the result.
- Namespaces every tool as `{skill_name}.{tool_name}` to prevent collisions.
- Validates all call arguments against the cached JSON Schema before dispatching.
- Wraps every `call_tool` in a per-call timeout.
- Detects crashes and restarts with exponential backoff.
- Supports hot-reload via a drain-in-flight then swap-process protocol.

### 2.4 Built-in: AST Code Analysis Tools

Three first-party skills are shipped as built-in MCP servers:

| Skill MCP Server | Language | Primary Library | Role |
|---|---|---|---|
| `ast.repo-map` | Python | tree-sitter + networkx | Parse repo, rank symbols by PageRank, emit JSONL context |
| `ast.search` | Rust/Python | ast-grep | Polyglot structural search and rewrite |
| `ast.ts-refactor` | TypeScript | ts-morph | Type-aware cross-file TypeScript rename/move |

These are registered in the SkillRegistry at harness startup alongside user-defined skills.

### 2.5 ASCII Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Labmate Host (Python)                            │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  Gemma 4 Brain (LLM inference loop)                                  │  │
│  │                                                                      │  │
│  │  system prompt: [catalog: skill-a, skill-b, ...]                    │  │
│  │  tool: load_skill(name)          tool: call_tool(ns.tool, args)     │  │
│  └────────────────────┬──────────────────────────┬───────────────────── ┘  │
│                       │                          │                          │
│         ┌─────────────▼─────────────┐ ┌─────────▼──────────────────────┐  │
│         │       SkillRunner         │ │        SkillRegistry            │  │
│         │  (instruction skills)     │ │    (executable MCP skills)      │  │
│         │                           │ │                                 │  │
│         │  Stage 1: glob SKILL.md   │ │  register(manifest)            │  │
│         │  Stage 2: inject catalog  │ │    -> spawn child process       │  │
│         │  Stage 3: lazy body load  │ │    -> initialize handshake      │  │
│         │           via load_skill  │ │    -> cache tools/list          │  │
│         └───────────────────────────┘ │                                 │  │
│                                       │  call_tool(ns.tool, args)       │  │
│                                       │    -> jsonschema gate           │  │
│                                       │    -> timeout wrapper           │  │
│                                       │    -> route to subprocess       │  │
│                                       │    -> crash supervision         │  │
│                                       └───────────┬─────────────────────┘  │
│                                                   │ stdin/stdout            │
│                                                   │ JSON-RPC 2.0            │
└───────────────────────────────────────────────────┼─────────────────────────┘
                                                    │
                  ┌─────────────────────────────────┼───────────────────────┐
                  │                                 │                       │
     ┌────────────▼──────────┐       ┌──────────────▼────────┐  ┌──────────▼────────┐
     │  skill: ast.repo-map  │       │  skill: git-ops (TS)  │  │  skill: embed (Py)│
     │  Python MCP server    │       │  Node MCP server       │  │  Python MCP server │
     │  (tree-sitter/nx)     │       │  (rmcp/zod)           │  │  (pydantic)       │
     │  logs -> stderr ONLY  │       │  logs -> stderr ONLY   │  │  logs -> stderr   │
     └───────────────────────┘       └───────────────────────┘  └───────────────────┘
```

---

## 3. SKILL.md Format

### 3.1 Frontmatter Schema

All fields are parsed with `yaml.safe_load` (never `yaml.load`). Unknown fields are preserved
in `SkillMeta.frontmatter` for forward compatibility.

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | YES | Unique identifier. Must be unique across all tiers. Used as the `load_skill(name)` argument and the catalog key. Lowercase, hyphens allowed: `ast-grep-search`. |
| `description` | string | YES | **The routing signal.** The model reads ONLY this field to decide whether to call `load_skill`. Must encode: what the skill does, when to use it, and key capabilities. Aim for 1-3 sentences. Vague descriptions mean the skill never triggers or triggers on everything. |
| `trigger` | string | NO | Optional trigger hint surfaced alongside the description. Can be a natural-language "use when" statement or a regex the harness can match against the task. |
| `tools` | list[string] | NO | Allowed MCP tools this skill may call when active. When present, the harness restricts tool availability to this list during the skill's activation. Mirrors Claude Code's `allowed-tools` semantics. |
| `model` | string | NO | Preferred model for this skill (e.g., `gemma-4-27b-it`). A skill runner may select a different model for skill-specific steps. |
| `license` | string | NO | SPDX license identifier for the skill (e.g., `MIT`, `Apache-2.0`). |
| `version` | string | NO | Semver string. Used by the registry for version-skew guards and future side-by-side loading. |
| `requires` | list[string] | NO | Prerequisite skill names. When present, the runner auto-loads these in topological order before activating this skill (cycle-guarded). |

### 3.2 Body Structure

The body is a standard markdown document. Conventions:

- **Keep bodies under 500 lines.** Bodies persist in context across turns after activation;
  longer bodies inflate per-turn token cost. Push fine detail into reference files that the body
  instructs the model to read on demand (third-tier disclosure).
- **No Claude-specific markup in bodies.** Do not use `<function_calls>` XML, `<search_quality_reflection>`,
  or any Anthropic-internal format in skill bodies. Skill bodies must be model-agnostic markdown.
- **Do not reference absolute paths.** Use paths relative to the project root or skill directory.
- **Executable assets live alongside the body.** Scripts, config templates, and reference files
  should be in the same directory as the SKILL.md or in a `resources/` subdirectory.

### 3.3 Example SKILL.md

```markdown
---
name: ast-repo-map
description: >
  Builds a ranked repository map for code navigation. Use when the agent needs
  to understand the structure of a codebase, locate symbols, or select which
  files to edit. Emits a token-budgeted JSONL of the most important function
  and class definitions ranked by PageRank over the call graph.
trigger: "Use when starting a new task requiring codebase orientation"
tools:
  - ast.repo-map.get_repo_map
  - ast.repo-map.get_symbols
  - read_file
version: "0.2.0"
license: MIT
requires: []
---

# AST Repo Map Skill

You have access to the `ast.repo-map` MCP server which provides structured,
language-aware codebase navigation without reading raw source files.

## When to Use

Use this skill at the beginning of any task that requires understanding existing
code structure:

- Locating a function or class by name across a large codebase
- Identifying which files will be affected by a change
- Building context before editing so you do not over-read files

## Available Tools

### `ast.repo-map.get_repo_map`

Returns a JSONL list of the most important symbols in the repository, ranked
by personalized PageRank and bounded by a configurable token budget.

```json
{
  "chat_files": ["src/service.py"],
  "max_tokens": 2000
}
```

Each output line: `{"name": "...", "kind": "function|class|method", "signature": "...", "parent": "...", "loc": "path/to/file:42"}`

### `ast.repo-map.get_symbols`

Returns all symbols defined in a specific file.

```json
{ "file": "src/service.py" }
```

## Workflow

1. Call `get_repo_map` with the files you are actively editing in `chat_files`.
2. Review the JSONL output to understand the symbol landscape.
3. Use `loc` fields to target `read_file` calls precisely — read only the
   functions you need, not entire files.
4. Re-call `get_repo_map` after edits so the map reflects current state.

## Limitations

- Does not resolve types across files — use the `ts-refactor` skill for
  type-aware TypeScript operations.
- Symbol map may lag by ~1 second after file edits (cache is mtime-keyed).
- Token budget is hard-capped; large monorepos will see truncation.
```

---

## 4. SkillRunner Implementation

### 4.1 Discovery (glob scan of skill roots)

At startup, `SkillRunner.discover()` iterates roots in priority order and calls
`root.rglob("SKILL.md")`. For each file:

1. Resolve the real path and check it is within the allowed root (symlink-escape guard).
2. Parse frontmatter ONLY using `frontmatter.parse(text)` — do not read the body into memory.
3. Validate required fields (`name`, `description`) via a Pydantic `SkillMeta` model.
4. On name collision, skip and log a shadowing warning; the first (highest-priority) entry wins.
5. Add to `self.catalog: dict[str, SkillMeta]`.

Frontmatter parsing uses `python-frontmatter` which calls `yaml.safe_load` internally. Never
override this to `yaml.load` or `yaml.FullLoader` — SKILL.md files are community/user content
and unsafe loading allows arbitrary Python object instantiation.

**Hot-reload:** The SkillRunner supports optional hot-reload via `watchfiles` or `watchdog`. On
a filesystem change event within any skill root, `discover()` is re-run. Newly added skills
appear without a harness restart.

### 4.2 Metadata-Only Index (catalog for system prompt)

`SkillRunner.catalog_prompt()` renders the catalog as a compact system-prompt block:

```
Available skills (call load_skill(name) to activate one):
- ast-repo-map: Builds a ranked repository map for code navigation. Use when...
- git-ops: Performs git operations (commit, branch, log). Use when...
- test-driven-development: Guides TDD workflow for Python and TypeScript. Use when...
```

This block is injected into the Gemma 4 system prompt on every turn. The token cost scales
linearly with number of skills but skill descriptions are short. For large libraries (> ~50
skills) consider the embedding-retrieval fallback described in Section 11.

`SkillRunner.tool_schema()` returns the `load_skill` tool definition in OpenAI-format JSON
Schema, passed to `apply_chat_template(tools=[...])`:

```python
{
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "Load the full instructions for a named skill.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": sorted(self.catalog)}
            },
            "required": ["name"],
        },
    },
}
```

The `enum` constraint limits the model to valid skill names and surfaces as a validation error
if an unknown name is requested.

### 4.3 Lazy Activation via load_skill meta-tool

When the model emits a `load_skill` tool call:

1. `SkillRunner.dispatch(tool_call)` extracts the `name` argument.
2. Look up `name` in `self.catalog`. Return a structured error if not found (including the list
   of valid names).
3. Re-validate that `meta.path.resolve()` is still within an allowed root (path confinement).
4. Check `self.loaded` — if the body is already cached, return a `status: already_loaded` note
   instead of re-appending the body (deduplication guard).
5. Read the file, parse frontmatter + body with `frontmatter.parse`, and store body in
   `self.loaded[name]`.
6. Return `{"status": "loaded", "name": name, "body": body}` as the tool response.

The body enters context as a standard tool response in the conversation history. It persists
across turns until the session ends or the context is compacted.

### 4.4 Trigger-Based Activation

In addition to LLM-mediated `load_skill` calls, the harness may perform trigger-based
pre-loading:

- If the `trigger` frontmatter field is present and the task description matches it (regex or
  embedding similarity), the SkillRunner may call `load_skill` proactively before the first
  model turn.
- This is optional and should be used sparingly to avoid pre-loading skills the model would
  not have selected, inflating context unnecessarily.

### 4.5 Deduplication and Cycle Guard

- **Deduplication**: `self.loaded: dict[str, str]` tracks already-activated skill names. A
  second `load_skill` call for a name already in `self.loaded` returns `status: already_loaded`
  without re-appending the body.
- **Chain limit**: `self._activations` counts total activations in the session. When it exceeds
  `self.max_chain` (default: 8), `load_skill` returns an error. This prevents unbounded
  skill-chaining loops where a skill body instructs the model to load another skill, which loads
  another, ad infinitum.
- **Cycle detection**: When `requires` is used for auto-loading prerequisites, the runner
  performs a topological sort of the dependency graph and rejects cycles with an actionable
  error message listing the cycle members.

**Python stub — SkillRunner:**

```python
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter  # python-frontmatter; yaml.safe_load by default

# CRITICAL: configure ALL handlers to sys.stderr. Never log to stdout.
log = logging.getLogger("skill_runner")


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: Path                      # resolved, confined path to SKILL.md
    tier: str                       # 'project' | 'personal' | 'bundled'
    frontmatter: dict[str, Any] = field(default_factory=dict)


class SkillRunner:
    """Discovers, catalogs, and lazily activates markdown skills.

    Catalog (frontmatter only) is built eagerly at startup.
    Skill bodies load lazily on an LLM-issued load_skill(name) tool call.

    CRITICAL: SkillRunner itself must never write to stdout.
    All logging goes to sys.stderr via the configured log handler.
    """

    def __init__(self, roots: list[Path], max_chain: int = 8) -> None:
        # roots ordered HIGHEST precedence first: project, personal, bundled
        self.roots: list[Path] = [r.expanduser().resolve() for r in roots]
        self.catalog: dict[str, SkillMeta] = {}
        self.loaded: dict[str, str] = {}    # name -> body (activation cache)
        self.max_chain = max_chain
        self._activations = 0

    # ---------- STAGE 1: discovery (frontmatter only) ----------

    def discover(self) -> None:
        """Scan all roots, parse frontmatter only, build catalog."""
        self.catalog.clear()
        tier_names = ["project", "personal", "bundled"]
        for tier, root in zip(tier_names, self.roots):
            if not root.is_dir():
                continue
            for skill_md in sorted(root.rglob("SKILL.md")):
                self._index(skill_md, tier, root)
        log.info("cataloged %d skills", len(self.catalog))   # -> stderr

    def _index(self, skill_md: Path, tier: str, root: Path) -> None:
        real = skill_md.resolve()
        if not self._within(real, root):            # symlink escape guard
            log.warning("skipping out-of-root skill: %s", skill_md)
            return
        try:
            meta, _body = frontmatter.parse(real.read_text(encoding="utf-8"))
        except Exception as exc:                     # malformed YAML or IO error
            log.warning("bad frontmatter in %s: %s", real, exc)
            return
        name = meta.get("name")
        desc = meta.get("description")
        if not name or not desc:
            log.warning("skill %s missing required name/description, skipping", real)
            return
        if name in self.catalog:
            log.warning(
                "skill name '%s' shadowed: %s overrides %s",
                name, self.catalog[name].path, real,
            )
            return
        self.catalog[name] = SkillMeta(name, desc, real, tier, dict(meta))

    # ---------- STAGE 2: catalog -> system prompt + tool schema ----------

    def catalog_prompt(self) -> str:
        lines = ["Available skills (call load_skill(name) to activate one):"]
        for m in sorted(self.catalog.values(), key=lambda s: s.name):
            lines.append(f"- {m.name}: {m.description}")
        return "\n".join(lines)

    def tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "Load the full instructions for a named skill.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": sorted(self.catalog),
                        }
                    },
                    "required": ["name"],
                },
            },
        }

    # ---------- STAGE 3: lazy activation ----------

    def load_skill(self, name: str) -> dict[str, Any]:
        self._activations += 1
        if self._activations > self.max_chain:
            return self._err("skill activation limit reached")
        meta = self.catalog.get(name)
        if meta is None:
            return self._err(
                f"unknown skill: {name}",
                available=sorted(self.catalog),
            )
        if name in self.loaded:
            return {"name": "load_skill",
                    "response": {"status": "already_loaded", "name": name}}
        if not self._within(meta.path, *self.roots):  # re-validate after potential FS change
            return self._err(f"path confinement violation for skill: {name}")
        _meta, body = frontmatter.parse(meta.path.read_text(encoding="utf-8"))
        self.loaded[name] = body
        return {"name": "load_skill",
                "response": {"status": "loaded", "name": name, "body": body}}

    def dispatch(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """Entry point for model-issued tool calls."""
        if tool_call.get("name") != "load_skill":
            return self._err(f"unknown tool: {tool_call.get('name')}")
        args = tool_call.get("arguments") or tool_call.get("parameters") or {}
        if isinstance(args, str):
            args = json.loads(args)
        return self.load_skill(args.get("name", ""))

    # ---------- helpers ----------

    @staticmethod
    def _within(path: Path, *roots: Path) -> bool:
        real = path.resolve()
        return any(real.is_relative_to(r.resolve()) for r in roots)

    @staticmethod
    def _err(msg: str, **extra: Any) -> dict[str, Any]:
        return {"name": "load_skill",
                "response": {"status": "error", "message": msg, **extra}}
```

---

## 5. SkillRegistry Implementation

### 5.1 register() — spawn child process MCP server

`register(manifest: SkillManifest)` is the entry point for registering an executable skill.
It:

1. Constructs `StdioServerParameters(command=manifest.command, args=manifest.args, env=manifest.env)`.
2. Opens an `AsyncExitStack`, enters `stdio_client(params)` to get `(read, write)` streams.
3. Enters `ClientSession(read, write)` and calls `await session.initialize()` — the MCP
   handshake, run once and kept warm.
4. Calls `await session.list_tools()` and caches the result. Each tool is stored as
   `{tool_name: inputSchema}`. Tools are registered in `self._tool_index` as
   `"{manifest.name}.{tool_name}" -> manifest.name` for collision-free routing.
5. Sets `sp.state = "READY"`.

The subprocess remains alive. The session is reused for all subsequent calls to that skill.
There is **no spawn-per-call**. Cold-start cost (Python import, Node V8 init, Rust binary load)
is paid once at registration.

### 5.2 call_tool() — jsonschema gate + timeout + routing

`call_tool(qualified_name: str, arguments: dict)`:

1. Parse the namespace: `ns, _, tool = qualified_name.partition(".")`.
2. Look up `self._skills[ns]`. Raise `SkillUnavailable` if not found or state is `DEAD`.
3. **Validate `arguments` against `sp.tools[tool]`** using `jsonschema.validate()` before
   dispatching. This gate fires before any subprocess round-trip. Malformed tool calls return
   a structured validation error to the model; the subprocess never sees them.
4. Increment `sp.inflight`.
5. `await asyncio.wait_for(sp.session.call_tool(tool, arguments), timeout=self._call_timeout)`.
6. On `asyncio.TimeoutError` or any exception: log to stderr, schedule `_maybe_restart(sp)`,
   re-raise.
7. Decrement `sp.inflight` in a `finally` block.

### 5.3 Supervision Loop (crash detection + exponential backoff)

`_maybe_restart(sp: SkillProcess)` is called on any call failure AND periodically by the
background `supervise()` loop:

1. Acquire `self._lock`. If already `DEAD`, return early.
2. Set `sp.state = "DEAD"` and remove its tool names from `self._tool_index`.
3. `await sp.stack.aclose()` to clean up the MCP session and streams.
4. Compute `backoff = min(2 ** sp.restarts, 30)` seconds. Increment `sp.restarts`.
5. `await asyncio.sleep(backoff)`.
6. Call `_spawn(sp)` to re-establish the subprocess, redo `initialize` + `list_tools`, and
   restore `sp.state = "READY"`.

`supervise(interval: float = 5.0)` is a background `asyncio` task that checks each registered
skill process every `interval` seconds. If a process has exited (detected via a dead `session`
or SIGCHLD/`_process_alive()` check), it triggers `_maybe_restart`.

### 5.4 Hot-Reload (drain in-flight -> swap process)

`reload(name: str)`:

1. Fetch `sp = self._skills[name]`.
2. Set `sp.state = "DRAINING"`. The call router stops accepting new calls to this skill
   (returns `SkillDraining` to the model, which retries later).
3. `while sp.inflight > 0: await asyncio.sleep(0.1)` — wait for all in-flight calls to
   complete. In-flight calls finish normally against the old process.
4. Spawn the new process version via `_spawn(sp)` (which also re-caches `list_tools`).
5. `await old_stack.aclose()` to shut down the old subprocess cleanly.
6. Set `sp.state = "READY"`.

No in-flight request is cancelled or dropped during a hot-reload.

**Python stub — SkillRegistry:**

```python
import asyncio
import logging
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

import jsonschema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# CRITICAL: log handler must write to sys.stderr, NEVER sys.stdout.
# The host's stdout is the JSON-RPC channel for any parent MCP server.
log = logging.getLogger("skillregistry")


class SkillUnavailable(Exception):
    pass


class SkillDraining(Exception):
    pass


@dataclass
class SkillManifest:
    name: str           # namespace prefix, e.g. "ast.repo-map"
    command: str        # "node" | "python" | absolute path to rust binary
    args: list[str]
    env: dict | None = None
    version: str | None = None
    language: str | None = None


@dataclass
class SkillProcess:
    manifest: SkillManifest
    session: ClientSession | None = None
    stack: AsyncExitStack | None = None
    tools: dict[str, dict] = field(default_factory=dict)  # tool_name -> inputSchema
    state: str = "INIT"   # INIT | READY | DRAINING | DEAD
    inflight: int = 0
    restarts: int = 0


class SkillRegistry:
    """Manages long-lived MCP skill subprocesses.

    CRITICAL: ALL logging must go to sys.stderr. This class may itself
    run as an MCP server (parent stdio channel); stdout must be reserved
    for JSON-RPC framing exclusively.
    """

    def __init__(self, call_timeout: float = 30.0) -> None:
        self._skills: dict[str, SkillProcess] = {}
        self._tool_index: dict[str, str] = {}  # "ns.tool" -> skill name
        self._call_timeout = call_timeout
        self._lock = asyncio.Lock()

    async def register(self, m: SkillManifest) -> None:
        sp = SkillProcess(manifest=m)
        await self._spawn(sp)
        self._skills[m.name] = sp
        log.info("registered skill: %s (%d tools)", m.name, len(sp.tools))

    async def _spawn(self, sp: SkillProcess) -> None:
        m = sp.manifest
        params = StdioServerParameters(command=m.command, args=m.args, env=m.env)
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()                       # MCP handshake, once per lifetime
        listed = await session.list_tools()
        sp.session = session
        sp.stack = stack
        sp.tools = {}
        sp.state = "READY"
        for t in listed.tools:
            qualified = f"{m.name}.{t.name}"
            sp.tools[t.name] = t.inputSchema            # live schema from tools/list
            self._tool_index[qualified] = m.name         # namespace routing table

    async def call_tool(self, qualified_name: str, arguments: dict) -> object:
        ns, _, tool = qualified_name.partition(".")
        sp = self._skills.get(ns)
        if sp is None or sp.state == "DEAD":
            raise SkillUnavailable(qualified_name)
        if sp.state == "DRAINING":
            raise SkillDraining(qualified_name)
        schema = sp.tools.get(tool)
        if schema is None:
            raise SkillUnavailable(f"no tool {tool!r} in skill {ns!r}")
        # jsonschema gate: validates BEFORE subprocess round-trip
        jsonschema.validate(instance=arguments, schema=schema)
        sp.inflight += 1
        try:
            return await asyncio.wait_for(
                sp.session.call_tool(tool, arguments),
                timeout=self._call_timeout,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            log.error("call %s failed: %r", qualified_name, exc)   # -> stderr only
            asyncio.create_task(self._maybe_restart(sp))
            raise
        finally:
            sp.inflight -= 1

    async def _maybe_restart(self, sp: SkillProcess) -> None:
        async with self._lock:
            if sp.state == "DEAD":
                return
            sp.state = "DEAD"
            # remove dead skill's tools from routing index
            dead_keys = [k for k, v in self._tool_index.items()
                         if v == sp.manifest.name]
            for k in dead_keys:
                self._tool_index.pop(k, None)
            if sp.stack:
                try:
                    await sp.stack.aclose()
                except Exception:
                    pass
            backoff = min(2 ** sp.restarts, 30)
            sp.restarts += 1
            log.warning("restarting skill %s in %ds (attempt %d)",
                        sp.manifest.name, backoff, sp.restarts)
            await asyncio.sleep(backoff)
            await self._spawn(sp)

    async def reload(self, name: str) -> None:
        """Hot-reload: drain in-flight calls, swap to new process."""
        sp = self._skills[name]
        old_stack = sp.stack
        sp.state = "DRAINING"
        while sp.inflight > 0:
            await asyncio.sleep(0.05)
        await self._spawn(sp)           # new process is now READY
        if old_stack:
            await old_stack.aclose()   # shut down old process after swap

    async def supervise(self, interval: float = 5.0) -> None:
        """Background health loop. Run as an asyncio.Task at harness startup."""
        while True:
            await asyncio.sleep(interval)
            for sp in list(self._skills.values()):
                if sp.state == "READY" and not _process_alive(sp):
                    log.warning("skill %s died unexpectedly", sp.manifest.name)
                    await self._maybe_restart(sp)


def _process_alive(sp: SkillProcess) -> bool:
    """Check if the subprocess underlying the MCP session is still running."""
    # Implementation: inspect sp.stack for the underlying transport process object.
    # Exact API depends on mcp SDK internals; may use sp.session._transport._process.poll()
    return sp.session is not None and sp.state not in ("DEAD", "INIT")
```

---

## 6. AST Code Analysis Tools

The three built-in AST skills are registered in the SkillRegistry at harness startup as
first-party MCP servers. They implement the "parse once, query many" pattern: trees are cached
by file mtime, updated incrementally, and served to the model as structured data (JSONL, not
raw source).

### 6.1 Parsing Layer (tree-sitter)

**Role:** Universal, error-tolerant parser for all supported languages. Returns a concrete
syntax tree even for broken/in-progress code.

**Implementation:** The `ast.repo-map` Python MCP server embeds `py-tree-sitter` with grammars
from `tree-sitter-language-pack`. A `RepoMapper` class maintains `cache: dict[path -> {mtime, tree, tags}]`.

- On every `get_repo_map` or `get_symbols` call, changed files (mtime differs from cache entry)
  are re-parsed. Unchanged files hit the cache — tree-sitter's incremental parsing means only
  the edited region is re-processed.
- `tags.scm` / `locals.scm` S-expression queries (shipped per grammar) extract definitions
  (`function`, `class`, `method`, `struct`, `trait`) and references (calls, imports, type uses).
- All log output goes to `sys.stderr`. **Never `print()` or write anything to stdout** — that
  channel is reserved for JSON-RPC framing.

**Key library versions:**
- `py-tree-sitter` 0.25.x / 0.26.x (verify ABI match with grammar packages)
- `tree-sitter-language-pack` 0.7.2+ (replaces deprecated `tree-sitter-languages`)

### 6.2 Symbol Map / Repo Map (PageRank-ranked JSONL)

**Role:** Give the model a compact, high-signal view of the repository's symbol structure within
a fixed token budget.

**Algorithm:**

1. Extract all `Tag` objects (definitions and references) from the parsed tree cache.
2. Build a directed file-level graph in `networkx`: for each reference `r` to a symbol `s`,
   add an edge from `r.file` to `d.file` for each definition `d` of `s`.
3. Run **personalized PageRank** (`nx.pagerank`) with a personalization vector that weights
   `chat_files` (the files the agent is currently editing) at approximately 50x vs. other files.
   The multiplier should be tuned empirically for Labmate's repos — do not blindly copy
   Aider's constant.
4. Sort all definition `Tag` objects by descending `ranks[tag.file]`.
5. Serialize to JSONL, emitting records until the token budget (`max_tokens`) is exhausted.
   Each record: `{"name": "...", "kind": "...", "signature": "...", "parent": "...", "loc": "file:line"}`.
6. Truncate with an `// ... N symbols omitted` marker rather than silently dropping.

**Tool exposed:** `ast.repo-map.get_repo_map(chat_files: list[str], max_tokens: int) -> str`

**Critical pitfall:** Never dump all symbols without ranking and budgeting. A 100k-symbol
monorepo at ~100 tokens/symbol overflows Gemma's context window. The budget is a hard cap, not
a guideline.

### 6.3 Structural Search (ast-grep)

**Role:** Fast polyglot structural search and rewrite. Operates on syntax (AST nodes), not
text, so it cannot match inside string literals or comments.

**The `ast.search` skill** wraps `ast-grep` (CLI binary `sg` or Python bindings `ast-grep-py`).
It exposes:

- `ast.search.find_code(pattern: str, language: str, path: str) -> list[Match]`
  — finds all AST nodes matching `pattern` in the given file or directory.
  Pattern supports meta-variables: `$VAR` (single node), `$$$MULTI` (zero-or-more nodes).
  Example: `requests.get($URL)` matches all GET calls regardless of the URL expression.

- `ast.search.rewrite(pattern: str, replacement: str, language: str, path: str) -> Diff`
  — rewrites matched nodes. Returns a unified diff for model review before application.
  **Always preview before saving.** The `--dry-run` / `--json` output is the pre-save gate.

- `ast.search.find_by_rule(rule_yaml: str, path: str) -> list[Match]`
  — accepts a YAML rule with `pattern`, `kind`, `inside`, `has`, `not` constraints for
  surgical, context-aware matches.

**Language routing:** ast-grep supports Python, TypeScript, JavaScript, Rust, Go, and more.
Pass `language` explicitly; do not rely on file-extension detection.

**Pitfall:** ast-grep is a syntactic tool. It does not resolve types, scopes, or cross-file
references. Do not use it for "rename all uses of function X" when X may be shadowed by a
local variable or overloaded. Route those to ts-morph (TypeScript) or an LSP (Python/Rust).

### 6.4 TypeScript Semantic Refactors (ts-morph)

**Role:** Type-aware, cross-file refactoring for TypeScript and JavaScript. The only tool that
correctly resolves all references to a symbol across a project, including through re-exports
and type aliases.

**The `ast.ts-refactor` skill** is a TypeScript MCP server using `ts-morph`. It exposes:

- `ast.ts-refactor.rename_symbol(tsconfig: str, file: str, symbol: str, new_name: str) -> Diff`
  — finds the declaration of `symbol` in `file`, calls `declaration.rename(new_name)` which
  resolves all cross-file references via the TS type checker, and returns a diff preview.
  `tsconfig` **must be an absolute path** — ts-morph misinterprets relative paths.

- `ast.ts-refactor.find_references(tsconfig: str, file: str, symbol: str) -> list[Reference]`
  — returns all usage sites of a symbol across the project, including re-exports and
  barrel imports.

- `ast.ts-refactor.move_symbol(tsconfig: str, source_file: str, symbol: str, dest_file: str) -> Diff`
  — moves a symbol to another file, rewriting import statements in all affected files.

**In-memory safety:** ts-morph holds all edits in memory until `project.save()`. The `Diff`
return value represents the pending in-memory changes. The model must explicitly trigger a
`save` call to write to disk. Never auto-save without model confirmation.

**Logging note:** The `ast.ts-refactor` MCP server is a TypeScript Node.js process. Use
`console.error()` for all logging. **Never `console.log()`** — that writes to stdout and
corrupts the JSON-RPC stream.

**Tool-selection decision table:**

| Need | Tool |
|---|---|
| Parse any language, extract symbols, build repo map | tree-sitter (ast.repo-map) |
| Search for a code pattern across files | ast-grep (ast.search.find_code) |
| Replace/rewrite a code pattern | ast-grep (ast.search.rewrite) |
| Rename a TypeScript/JS symbol across the project | ts-morph (ast.ts-refactor.rename_symbol) |
| Find all references to a TS symbol | ts-morph (ast.ts-refactor.find_references) |
| Move a symbol between TS files | ts-morph (ast.ts-refactor.move_symbol) |
| Security taint / dataflow analysis | semgrep (separate skill, not built-in) |
| Python semantic rename | rope or jedi (separate skill) |
| Rust cross-file rename | rust-analyzer LSP (separate skill) |

---

## 7. BDD Test Scenarios

```gherkin
Feature: SKILL.md discovery across layered roots
  As the Labmate harness
  I want to discover skills from project, personal, and bundled roots
  So that the model has a catalog without paying body-token cost

  Scenario: Discover skills parsing frontmatter only
    Given a personal root "~/.claude/skills" containing "web-search/SKILL.md"
    And a project root "./.claude/skills" containing "deploy/SKILL.md"
    And each SKILL.md has valid YAML frontmatter with name and description
    When the SkillRunner performs discovery at startup
    Then the catalog contains entries "web-search" and "deploy"
    And each catalog entry holds only name, description, and resolved path
    And no skill body has been read into memory

  Scenario: Skip and warn on malformed frontmatter
    Given a skill file with frontmatter missing the required "description" field
    When the SkillRunner performs discovery
    Then that skill is excluded from the catalog
    And a warning naming the offending file path is logged to stderr
    And discovery of all other skills still succeeds

  Scenario: Project tier overrides personal tier on name collision
    Given a personal skill "deploy" at "~/.claude/skills/deploy/SKILL.md"
    And a project skill "deploy" at "./.claude/skills/deploy/SKILL.md"
    When discovery resolves the name "deploy"
    Then the catalog entry "deploy" points to the project-tier path
    And a shadowing warning identifies the overridden personal-tier path

Feature: Lazy LLM-mediated skill activation via load_skill
  As the harness orchestrator
  I want the model to activate a skill by emitting a load_skill tool call
  So that the body is loaded only when relevant

  Scenario: Model loads a skill body on demand
    Given the catalog is injected into the Gemma 4 system prompt
    And a single tool "load_skill(name)" is exposed via apply_chat_template
    When the model emits a tool call load_skill with name "deploy"
    Then the runner resolves "deploy" to its confined path
    And reads and parses the SKILL.md body
    And returns the body text as the tool_response for the next turn
    And the catalog injection remains unchanged in size

  Scenario: Activation of an unknown skill is rejected gracefully
    When the model emits a tool call load_skill with name "does-not-exist"
    Then the runner returns a tool_response error "unknown skill: does-not-exist"
    And the available skill names are listed in the error
    And no file read is attempted

  Scenario: Re-activating an already-loaded skill returns cache note
    Given skill "deploy" is already in self.loaded
    When the model emits a second load_skill for "deploy"
    Then the runner returns status "already_loaded" without re-reading the file
    And the body is not appended to context a second time

  Scenario: Skill chaining loop is capped
    Given max_chain is set to 3
    When the model issues 4 sequential load_skill calls in one session
    Then the 4th call returns an error "skill activation limit reached"
    And no further skill bodies are loaded

Feature: Path confinement and safe parsing
  As a security-conscious skill runner
  I want to confine all file access to allowed roots
  So that malicious skill names cannot escape the sandbox

  Scenario: Reject directory traversal in a skill name
    Given allowed roots "~/.claude/skills" and "./.claude/skills"
    When the model emits load_skill with name "../../../../etc/passwd"
    Then the resolved real path is checked with is_relative_to
    And because it is outside all roots the request is rejected
    And no file outside the allowed roots is opened

  Scenario: Frontmatter is parsed with a safe loader
    Given a SKILL.md whose frontmatter contains "!!python/object/apply:os.system"
    When the runner parses the frontmatter
    Then yaml.safe_load is used and the tag is not executed
    And parsing yields plain data or raises a handled error

Feature: SkillRegistry child-process MCP server lifecycle

  Scenario: Successful registration of a TypeScript skill
    Given a manifest declaring command "node" and args ["skills/git-ops/dist/index.js"]
    When the registry spawns the subprocess and completes the MCP initialize handshake
    And calls tools/list
    Then each tool has a self-contained inputSchema (no external $ref)
    And tools are indexed as "git-ops.commit", "git-ops.log", etc.
    And the subprocess remains alive for reuse

  Scenario: Bad input is rejected before subprocess dispatch
    Given a registered skill with tool "resize_image" requiring integer "width" and "height"
    When the agent issues a call with width="big" (a string) and no height
    Then jsonschema.validate fails against the cached inputSchema
    And a structured validation error is returned
    And no tools/call is sent to the subprocess

  Scenario: Crashed skill subprocess is restarted with exponential backoff
    Given skill "embed" is READY
    When the subprocess exits unexpectedly
    Then the registry detects the dead process within 5 seconds
    And sets state to DEAD and removes tools from the routing index
    And waits backoff seconds (2^restarts, capped at 30)
    And re-spawns the subprocess and re-runs initialize + tools/list

  Scenario: Hot-reload drains in-flight calls before swapping
    Given skill "summarizer" is READY with two tool calls in flight
    When a reload is requested
    Then state is set to DRAINING and no new calls are routed to the old process
    And the two in-flight calls complete normally
    And the new subprocess is spawned and becomes READY
    And the old subprocess is shut down cleanly

Feature: AST repo map and structural editing

  Scenario: Token-budgeted repo map ranked by importance
    Given a Python repository containing 500 extracted symbols
    And the active working set is ["app/service.py"]
    When get_repo_map is called with max_tokens=2000
    Then symbols are ranked by personalized PageRank with chat files boosted
    And only top-ranked symbols fitting within 2000 tokens are emitted as JSONL
    And lower-ranked symbols are dropped with a truncation marker

  Scenario: Tree-sitter error tolerance on broken code
    Given a TypeScript file containing a syntax error mid-edit
    When the parsing layer parses the file
    Then tree-sitter returns a partial tree containing an ERROR node
    And no exception is raised
    And symbols outside the error region are still extracted

  Scenario: Cross-file TypeScript rename via ts-morph
    Given a function "computeTotal" referenced across 5 TypeScript files
    When rename_symbol is called with new_name="computeOrderTotal"
    Then all 5 files are updated via the TS type checker's reference resolution
    And import statements are corrected
    And an identically named symbol in an unrelated scope is NOT renamed
    And changes are held in memory until save() is confirmed

  Scenario: Polyglot structural search with ast-grep
    Given a Python codebase making HTTP calls
    When find_code is called with pattern "requests.get($URL)" and language "python"
    Then all HTTP GET call sites are returned with file and line
    And matches inside string literals and comments are NOT returned
```

---

## 8. Common Pitfalls

### Stdout Pollution Corrupts JSON-RPC (Critical — #1 stdio skill killer)

The stdio transport **reserves stdout exclusively for JSON-RPC 2.0 framing**. Any non-protocol
write to stdout — a `print()` in Python, a `console.log()` in Node, a `println!()` in Rust,
an ANSI color escape sequence, a library startup banner, or a debug emoji — interleaves with
the framing bytes and breaks the host's JSON parser. Symptoms are misleading:
`"Parse error: Unexpected token"`, `"JSON Parse error: Unexpected identifier Server"`, or
`"Method not found: notifications/initialized"` rather than an obvious "stdout pollution"
error.

**Enforce stderr-only logging in every skill subprocess in every language:**

```python
# Python skill MCP server — REQUIRED setup
import logging, sys
logging.basicConfig(
    stream=sys.stderr,    # MUST be stderr
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
# NEVER use bare print() in a skill process. Use log.info() / log.error() etc.
```

```typescript
// TypeScript/Node.js skill MCP server — REQUIRED
// NEVER: console.log(...)      // writes to stdout -> corrupts JSON-RPC
// ALWAYS: console.error(...)   // writes to stderr -> safe
```

```rust
// Rust skill MCP server — REQUIRED
// NEVER: println!(...) or dbg!(...)
// ALWAYS: eprintln!(...) or tracing to stderr
use tracing_subscriber::fmt;
fn main() {
    // direct tracing output to stderr
    fmt().with_writer(std::io::stderr).init();
}
```

Check dependency libraries too. A Python package that prints a banner on import will corrupt
the stream before `initialize` is even called.

### Eager Skill Body Loading Blows Context Budget

Loading skill bodies into the system prompt at startup is the canonical progressive-disclosure
mistake. With Gemma 4's context window, even five skills at 400 lines each will consume context
that should be used for actual task context. Parse frontmatter only at discovery. Load bodies
only on explicit `load_skill` calls.

### Cycle in Skill Chaining

A skill body that instructs the model to `load_skill("skill-a")`, which in turn loads
`skill-b`, which loads `skill-a`, creates an infinite loop that grows context until it
overflows. Two guards are required:
- **Deduplication**: `self.loaded` tracks activated skills; re-requesting an already-loaded
  skill returns `already_loaded` without re-appending the body.
- **Chain limit**: `self.max_chain` caps total activations per session.

### Claude-Specific Markup in Skill Bodies

Skill bodies must be model-agnostic. Do not include:
- `<function_calls>` XML blocks (Claude Code internal format)
- `<search_quality_reflection>` or similar Anthropic-internal tags
- `HUMAN_TURN` / `AI_TURN` delimiters from old Anthropic prompt formats

These will either be misinterpreted by Gemma 4 or confuse the parser.

### Process Isolation Is Not Security Sandboxing

OS subprocess skills inherit the full ambient authority of the host user: filesystem access,
network access, environment variables (including API keys and secrets). Treating a subprocess
skill as "sandboxed" is incorrect. For untrusted or model-generated skills, use the WASM
Component Model path (Wasmtime / Extism) where capabilities are explicitly granted, not assumed.
For subprocess skills, at minimum: scrub the environment (`env={}` or an explicit allowlist),
set a confined `cwd`, and restrict with OS tools (seccomp/landlock on Linux, sandbox-exec on
macOS) where available.

### Tool Name Collisions Across Skills

MCP provides no namespace isolation. Two skills each exposing a tool named `search` or `run`
collide in the flat tool list presented to the model. The SkillRegistry must prefix every tool
with the skill namespace: `{skill_name}.{tool_name}`. Never expose raw, unqualified tool names
from multiple skills simultaneously.

### Hand-Authored JSON Schemas Drift

The `inputSchema` cached from `tools/list` is the live, authoritative schema. Never hand-write
JSON Schema separately in a manifest — it will silently diverge from the skill's actual
parameter handling as the skill evolves. Use native schema generators (Zod/Standard Schema in
TypeScript, Pydantic `.model_json_schema()` in Python, `schemars` derive in Rust) and emit from
code.

### Self-Contained Schema Violation (External $ref)

MCP tool `inputSchema` must be self-contained JSON Schema. Generators that emit `$ref` pointing
to sibling files or external URLs produce schemas the host cannot resolve. Inline all
`$defs`/definitions into each tool schema (bundle/dereference before publishing).

### No Supervision Means One Dead Skill Freezes the Loop

A skill subprocess that crashes mid-call leaves `sp.session.call_tool(...)` awaiting forever if
there is no timeout and no supervision. Every `call_tool` must be wrapped in
`asyncio.wait_for(timeout=...)`. The `supervise()` background task catches dead processes
between calls. Fail the specific tool call fast; never let one bad skill freeze the whole
agent loop.

### Grammar/Binding Version Mismatch in tree-sitter

`py-tree-sitter` and a grammar package built against a different C ABI version cause
`ImportError` or, worse, silent tree corruption. Always pin `py-tree-sitter` and grammar
packages together. Use `tree-sitter-language-pack` (which ships a coherent pre-built set) rather
than mixing individual grammar packages built at different times.

### yaml.load() on Skill Frontmatter

Never use `yaml.load()` or `yaml.FullLoader` on skill files. Skill content is user/community
content. The unsafe loader can instantiate arbitrary Python objects via YAML tags like
`!!python/object/apply:os.system`. Use `yaml.safe_load` only. The `python-frontmatter` library
does this by default — do not override it.

---

## 9. Dependencies

### Python (SkillRunner and SkillRegistry)

| Package | Version | Purpose |
|---|---|---|
| `python-frontmatter` | >=1.1.0 | YAML frontmatter parsing; safe_load by default |
| `PyYAML` | >=6.0 | Dependency of python-frontmatter; always use `yaml.safe_load` |
| `mcp` | ^1.x | Official Python MCP SDK; `ClientSession`, `StdioServerParameters`, `stdio_client` |
| `pydantic` | v2 | Validate/normalize parsed frontmatter into typed `SkillMeta` |
| `jsonschema` | latest | Validate tool call arguments against cached inputSchema before dispatch |
| `anyio` | latest | Structured-concurrency backbone of the mcp SDK; per-call timeout support |
| `networkx` | latest | Symbol graph construction and personalized PageRank for repo map |
| `tree-sitter` (py-tree-sitter) | 0.25.x / 0.26.x | Tree-sitter Python bindings for parsing |
| `tree-sitter-language-pack` | >=0.7.2 | Pre-compiled grammar pack (replaces deprecated tree-sitter-languages) |
| `ast-grep-py` | latest (May 2026+) | Python bindings for ast-grep structural search/rewrite (in-process, no subprocess) |
| `watchfiles` or `watchdog` | latest | Optional hot-reload: re-scan skill roots on filesystem change |
| `sentence-transformers` + `faiss-cpu` | latest | Optional embedding-retrieval fallback for large skill catalogs |

### TypeScript / Node.js (MCP skill template)

| Package | Version | Purpose |
|---|---|---|
| `@modelcontextprotocol/sdk` | ^1.x | Official TS SDK; `Server`, `StdioServerTransport`, Standard Schema tool I/O |
| `zod` | ^4 (or Valibot / ArkType) | Define tool input/output schemas; emit JSON Schema as the cross-language contract |
| `ts-morph` | ^28.0.0 | TypeScript Compiler API wrapper; type-aware rename, move, find-references |
| `typescript` | latest | Underlying TS type checker required by ts-morph |

### Rust (rmcp — skill MCP servers)

| Crate | Version | Purpose |
|---|---|---|
| `rmcp` | latest (modelcontextprotocol/rust-sdk) | Official Rust MCP SDK; `#[tool_router]`, `#[tool]` macros, `transport::stdio` |
| `serde` + `serde_json` | ^1 | JSON-RPC payload serialization |
| `schemars` | ^0.8 / ^1 | Derive JSON Schema from Rust param structs |
| `tokio` | ^1 | Async runtime backing rmcp stdio servers |
| `tracing` + `tracing-subscriber` | latest | Structured logging — configured to write to stderr ONLY |

---

## 10. Reference Papers and Repos

### Papers

| Citation | ArXiv | Relevance |
|---|---|---|
| Ehtesham et al. (2025) — Survey of Agent Interoperability Protocols | 2505.02279 | MCP as JSON-RPC client-server interface; canonical choice-of-protocol reference |
| Yang et al. (2025) — Survey of AI Agent Protocols | 2504.16736 | Pre-MCP fragmentation problem; positions MCP as standardizing layer |
| ScaleMCP (2025) — Dynamic MCP Tool Registries | 2505.06416 | Dynamic auto-synchronizing skill registries; direct model for SkillRegistry design |
| Unified Tool Integration (2025) — Protocol-Agnostic Function Calling | 2508.02979 | Protocol-agnostic host registry interface design |
| Wang et al. (2023) — Voyager | 2305.16291 | Ever-growing skill library with embedding retrieval and self-verification |
| Yao et al. (2022) — ReAct | 2210.03629 | Thought-action-observation loop; load_skill as ReAct action |
| SkillFlow (2025) — Scalable Skill Retrieval | 2504.06188 | SKILL.md bundle-as-unit retrieval; closest published analogue to this design |
| Tool-to-Agent Retrieval (2025) | 2511.01854 | +19.4% Recall@5 for embedding-based skill routing at scale |
| SkillRouter (2026) — Retrieve-and-Rerank at Scale | 2603.22455 | Full skill body is decisive routing signal at scale; retrieve-then-rerank pattern |
| Ouyang et al. (2024) — RepoGraph | 2410.14684 | Repo-level code graph for multi-hop agent queries; extends PageRank approach |
| Liu et al. (2024) — CodexGraph | 2408.03910 | Graph DB for LLM code queries; model for advanced repo-map queries |
| Zhang et al. (2025) — cAST | 2506.15655 | AST-aware structural chunking for code RAG |
| Schick et al. (2023) — Toolformer | 2302.04761 | LLM-mediated tool invocation; foundational framing for load_skill |
| Packer et al. (2023) — MemGPT | 2310.08560 | Tiered context management; skill body eviction for long-horizon tasks |
| Alon et al. (2019) — code2vec | 1803.09473 | AST-path code representations; foundational for symbol/code embedding |

> Note: arxiv:2603.22862 and arxiv:2512.01939 (cited in source research) are flagged uncertain —
> verify existence and IDs before citing in production documentation.

### Repositories

| Repo | URL | Relevance |
|---|---|---|
| modelcontextprotocol/typescript-sdk | github.com/modelcontextprotocol/typescript-sdk | Official TS SDK; `StdioServerTransport`, `StdioClientTransport` |
| modelcontextprotocol/python-sdk | github.com/modelcontextprotocol/python-sdk | Official Python SDK; `mcp` package; `FastMCP`, stdio transport |
| modelcontextprotocol/rust-sdk | github.com/modelcontextprotocol/rust-sdk | Official Rust SDK (`rmcp`); `#[tool_router]`, `#[tool]` macros |
| modelcontextprotocol/registry | github.com/modelcontextprotocol/registry | RESTful skill catalog over MongoDB; direct model for manifest + registry |
| modelcontextprotocol/servers | github.com/modelcontextprotocol/servers | Reference MCP servers (filesystem, git, fetch); canonical skill examples |
| obra/superpowers | github.com/obra/superpowers | Reference SKILL.md framework; discovery/tier precedence algorithm |
| sst/opencode | github.com/sst/opencode | Multi-host SKILL.md consumer; layered root resolution reference |
| MineDojo/Voyager | github.com/MineDojo/Voyager | Ever-growing skill library; `SkillManager` with vector store |
| agent0ai/agent-zero | github.com/agent0ai/agent-zero | Markdown-driven agentic framework; relevant for small-model tool parsing |
| eyeseast/python-frontmatter | github.com/eyeseast/python-frontmatter | Frontmatter parser; `frontmatter.parse()` for catalog scanning |
| tree-sitter/tree-sitter | github.com/tree-sitter/tree-sitter | Core parser; S-expression query language |
| tree-sitter/py-tree-sitter | github.com/tree-sitter/py-tree-sitter | Python bindings for tree-sitter |
| Goldziher/tree-sitter-language-pack | github.com/Goldziher/tree-sitter-language-pack | Maintained pre-built grammar pack for Python 3.9-3.13 |
| ast-grep/ast-grep | github.com/ast-grep/ast-grep | Polyglot structural search/rewrite CLI and library |
| ast-grep/ast-grep-mcp | github.com/ast-grep/ast-grep-mcp | Official MCP server for ast-grep (`dump_syntax_tree`, `find_code`, etc.) |
| dsherret/ts-morph | github.com/dsherret/ts-morph | TypeScript Compiler API wrapper; type-aware cross-file refactors |
| Aider-AI/aider | github.com/Aider-AI/aider | Reference implementation of tree-sitter + PageRank repo map |
| pdavis68/RepoMapper | github.com/pdavis68/RepoMapper | Standalone Aider-style repo map; CLI and MCP server |
| joseph-wortmann/hyper-mcp | github.com/joseph-wortmann/hyper-mcp | MCP server with WASM skill plugins; "MCP outside, WASM inside" pattern |
| extism/extism | github.com/extism/extism | Cross-language WASM plugin framework on Wasmtime; alternative sandbox |
| bytecodealliance/wasmtime | github.com/bytecodealliance/wasmtime | WASM runtime implementing Component Model + WASI Preview 2 |
| intellectronica/skillz | github.com/intellectronica/skillz | Small MCP skill-discovery/registry reference |

---

## 11. SOTA Improvements

The following improvements move the Skills Layer from a correct baseline toward state-of-the-art
for autonomous local agents. They are ordered by expected impact.

### 1. WASM Component Model as the Sandbox Tier

Move performance-sensitive and untrusted skills from OS subprocesses to WASI Preview 2
components described by WIT interfaces, run in Wasmtime (or via Extism). The "MCP outside,
WASM inside" pattern (hyper-mcp) presents the skill to the agent as a normal MCP server while
enforcing deny-by-default capability sandboxing inside. Benefits: portable single-artifact
distribution, near-native speed, deterministic resource caps, true security isolation. Reserve
for model-generated or third-party skills where OS-process trust is inappropriate.

### 2. Embedding-Retrieval Fallback in Front of load_skill (Voyager-Style)

When the skill catalog grows beyond the system prompt budget, replace the full catalog injection
with a retrieval step: embed the current task description, retrieve top-k skill names by
description embedding similarity (sentence-transformers + faiss-cpu or chromadb), and inject
only those into the prompt. `load_skill` remains the activation primitive. Implements the
Voyager SkillManager pattern for large, evolving skill libraries.

### 3. Retrieve-then-Rerank over Full Skill Bodies (SkillRouter Pattern)

For large libraries with overlapping skill descriptions, name+description matching alone
degrades routing accuracy. Implement a retrieve-then-rerank stage (SkillRouter, arxiv:2603.22455):
shortlist candidates by description embedding, then rerank using full body text as the signal.
The model still sees only name+description; the reranker inspects bodies without loading them
into the model's context. Reported gains: +19.4% Recall@5 over description-only retrieval.

### 4. Graph-of-Skills Dependency-Aware Activation

Extend SKILL.md frontmatter `requires` to declare prerequisite skills. On activation, the
SkillRunner auto-loads the transitive dependency closure in topological order, guarded by the
existing deduplication set and `max_chain` limit. This removes the burden on the model to
manually discover and load each prerequisite before using a composed skill.

### 5. Tiered/Paged Context Management for Skill Bodies (MemGPT-Inspired)

For long-horizon ReAct tasks, skill bodies accumulate in context until the window overflows.
Implement eviction: track the last-accessed turn for each loaded skill body, and compact or
evict the least-recently-used bodies once a token budget threshold is crossed. Keep a compact
"skills currently active" summary rather than appending every body permanently.

### 6. Self-Verification and Ever-Growing Skill Library (Voyager)

Allow the harness to WRITE new SKILL.md files when a task is solved and self-verified.
Auto-generate frontmatter (name + description) from the task outcome, persist to the project
skill root, and add to the catalog. Over multiple sessions, the agent's capability set compounds
without manual authoring.

### 7. Versioned Registry with Semver Resolution

Extend `SkillManifest` to carry semver and resolve skills by version range, supporting
side-by-side major versions (`skill-name@1` and `skill-name@2` as distinct namespaces). This
neutralizes runtime-version skew and enables gradual migration when a skill's API changes
incompatibly.

### 8. Streamable HTTP Transport for Remote/Cloud Skills

Build the SkillRegistry's MCP client transport-agnostic (following the official MCP
`2025-03-26` spec which made Streamable HTTP the recommended remote transport, replacing SSE).
Local skills use stdio (default); heavy or GPU-bound skills use Streamable HTTP to run off-box
without changing the agent-facing contract.

### 9. Schema-First Codegen Instead of Per-Language Hand-Written Clients

Generate typed client bindings and the host's call surface from a single source of truth: WIT
interfaces for WASM skills, JSON Schema / OpenAPI for MCP skills. Adding a fourth language
(e.g., Go) becomes a code-generation step, not a re-implementation. This eliminates schema drift
at the source.

### 10. Code Graph DB Beyond PageRank (CodexGraph / RepoGraph)

Index the full repository into a graph database (NetworkX or Neo4j) with definition, reference,
and call edges. Enable multi-hop queries that flat PageRank ranking cannot answer ("what calls
what calls what"). Evaluated on SWE-bench-style tasks in CodexGraph (arxiv:2408.03910) and
RepoGraph (arxiv:2410.14684). This is the natural next step after the basic Aider-style
PageRank repo map.

---

*End of Skills Layer Spec (Hands) — Labmate v0.1*
