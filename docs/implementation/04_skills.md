# 04 — Skills Framework

**Labmate Implementation Plan**
Layer: Skills (Hands) — SKILL.md format, SkillRunner, SkillRegistry, example skill

---

## 1. What This Layer Does

The Skills Framework is the capability surface of Labmate. It has three distinct tiers, each with its own job:

**Tier 1 — SKILL.md format (Contract F)**
A YAML-frontmatter + Markdown standard for describing skills. The frontmatter is the machine-readable manifest (name, description, tools list, version). The Markdown body is the human/model-readable instructions. Only frontmatter is read at startup; bodies are lazy. This file IS the manifest — no separate registry file is needed.

**Tier 2 — SkillRunner (discovery and lazy activation)**
A Python class that recursively scans three root directories for `SKILL.md` files in strict priority order (project > personal > bundled), parses frontmatter only into an in-memory catalog, injects a compact name+description summary into the LLM system prompt on every turn, and handles the `load_skill(name)` tool call by reading and returning the full body on demand. Skills are never loaded into context until the model explicitly requests them.

**Tier 3 — SkillRegistry (child-process lifecycle)**
A Python class that manages long-lived MCP server subprocesses. Each executable skill is a subprocess communicating over stdin/stdout via JSON-RPC 2.0 (Contract B). The registry spawns the subprocess once, runs the MCP `initialize` handshake plus `tools/list` once (caching the result), namespaces every tool as `{skill_name}.{tool_name}`, validates call arguments against the cached JSON Schema before dispatching, wraps every call in a per-call timeout, and restarts crashed processes with exponential backoff.

The three tiers are independent: a skill can have a SKILL.md body only (instruction skill), an executable subprocess only (registered programmatically), or both.

---

## 2. Dependencies

### Python (SkillRunner and SkillRegistry host)

| Package | Version | Purpose |
|---|---|---|
| `python-frontmatter` | >=1.1.0 | YAML frontmatter parsing; wraps `yaml.safe_load` |
| `PyYAML` | >=6.0 | Transitive dep of python-frontmatter; never call `yaml.load` directly |
| `pydantic` | v2 | Typed `SkillMeta` model for frontmatter validation |
| `mcp` | ^1.x | Official Python MCP SDK: `ClientSession`, `StdioServerParameters`, `stdio_client` |
| `jsonschema` | latest | Validate tool call arguments against cached `inputSchema` before dispatch |
| `anyio` | latest | Structured-concurrency backbone of the MCP SDK; `fail_after` for per-call timeout |
| `networkx` | latest | Symbol graph + personalized PageRank for ast-repo-map |
| `py-tree-sitter` | 0.25.x / 0.26.x | Tree-sitter Python bindings |
| `tree-sitter-language-pack` | >=0.7.2 | Pre-compiled grammar pack (replaces deprecated `tree-sitter-languages`) |
| `watchfiles` | latest | Optional hot-reload: re-scan skill roots on filesystem events |

### TypeScript (skill subprocess template)

| Package | Version | Purpose |
|---|---|---|
| `@modelcontextprotocol/sdk` | ^1.x | Official TS SDK: `Server`, `StdioServerTransport` |
| `zod` | ^4 | Tool input schema definition; emits self-contained JSON Schema |
| `typescript` | latest | Compiler |

### Rust (if a skill subprocess is written in Rust)

| Crate | Version | Purpose |
|---|---|---|
| `rmcp` | latest | Official Rust MCP SDK; `#[tool_router]`, `#[tool]` macros, `transport::stdio` |
| `serde` + `serde_json` | ^1 | JSON serialization |
| `schemars` | ^0.8 | Derive JSON Schema from Rust structs |
| `tokio` | ^1 | Async runtime |
| `tracing` + `tracing-subscriber` | latest | Structured logging to stderr ONLY |

---

## 3. File Structure

```
services/
├── orchestrator/
│   └── skills/
│       ├── runner.py          — SkillRunner class (discovery + lazy activation)
│       └── registry.py        — SkillRegistry class (subprocess lifecycle)
└── skills/
    ├── ast-repo-map/
    │   ├── SKILL.md           — frontmatter + body for the repo map skill
    │   ├── package.json       — Node.js package manifest
    │   └── src/
    │       └── index.ts       — TypeScript MCP server (the subprocess)
    └── python-executor/
        ├── SKILL.md
        └── server.py          — Python MCP server (the subprocess)
```

The `skills/` directory under the repo root serves as the **bundled** tier. Users may add skills at `~/.claude/skills/` (personal tier) or `./.claude/skills/` relative to the project root (project tier).

---

## 4. Interface Contracts

### 4.1 SKILL.md YAML Frontmatter Schema (Contract F)

The authoritative schema. All fields are parsed with `yaml.safe_load`. Unknown fields are preserved for forward compatibility.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "SkillFrontmatter",
  "type": "object",
  "required": ["name", "description"],
  "additionalProperties": true,
  "properties": {
    "name": {
      "type": "string",
      "description": "Unique identifier used as the load_skill(name) argument and catalog key. Lowercase, hyphens allowed. Must be unique across all tiers.",
      "pattern": "^[a-z][a-z0-9-]*$",
      "examples": ["ast-repo-map", "git-ops", "python-executor"]
    },
    "description": {
      "type": "string",
      "description": "THE routing signal — the only field the model reads to decide whether to call load_skill. Must encode: what the skill does, when to use it, key capabilities. Aim for 1-3 sentences. Vague descriptions mean the skill never triggers or triggers on everything."
    },
    "trigger": {
      "oneOf": [
        { "type": "string" },
        { "type": "array", "items": { "type": "string" } }
      ],
      "description": "Optional trigger hint. Can be a natural-language 'use when' statement or a list of keyword strings the harness matches against the task. Enables proactive pre-loading without LLM mediation."
    },
    "tools": {
      "type": "array",
      "items": {
        "oneOf": [
          { "type": "string", "description": "Allowed tool name, e.g. 'ast.repo-map.get_repo_map'" },
          {
            "type": "object",
            "required": ["name", "description", "inputSchema"],
            "properties": {
              "name": { "type": "string" },
              "description": { "type": "string" },
              "inputSchema": { "type": "object" }
            }
          }
        ]
      },
      "description": "When present, restricts available tools to this list during skill activation. Mirrors Claude Code's allowed-tools semantics. String items are tool name allowlist; object items are full tool definitions for documentation purposes."
    },
    "model": {
      "type": "string",
      "description": "Preferred model for skill-specific steps, e.g. 'gemma-4-27b-it'. Optional — defaults to the session model.",
      "examples": ["gemma-4-27b-it", "any"]
    },
    "version": {
      "type": "string",
      "description": "Semver string. Used for version-skew guards.",
      "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$",
      "examples": ["1.0.0", "0.2.0"]
    },
    "license": {
      "type": "string",
      "description": "SPDX license identifier.",
      "examples": ["MIT", "Apache-2.0"]
    },
    "requires": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Prerequisite skill names. The runner auto-loads these in topological order before activating this skill. Cycle-guarded via topological sort. Default: []."
    }
  }
}
```

### 4.2 JSON-RPC Messages (Contract B) — SkillRegistry → Skill Subprocess

Every skill subprocess speaks MCP JSON-RPC 2.0 over newline-delimited stdin/stdout.

**Initialize handshake (sent once at registration time, kept warm):**

```json
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"labmate-orchestrator","version":"1.0.0"}}}

← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"ast-repo-map","version":"0.2.0"}}}

→ {"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
```

**Tool discovery (sent once at registration, result is cached):**

```json
→ {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}

← {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
      "tools": [
        {
          "name": "get_repo_map",
          "description": "Return PageRank-ranked symbols within a token budget",
          "inputSchema": {
            "type": "object",
            "properties": {
              "chat_files": { "type": "array", "items": { "type": "string" } },
              "max_tokens": { "type": "integer", "default": 2000 }
            },
            "required": ["chat_files"]
          }
        }
      ]
    }
  }
```

**Tool call (sent per request, namespaced by SkillRegistry before dispatch):**

```json
→ {
    "jsonrpc": "2.0",
    "id": "call-uuid-001",
    "method": "tools/call",
    "params": {
      "name": "get_repo_map",
      "arguments": { "chat_files": ["src/service.py"], "max_tokens": 2000 }
    }
  }

← {
    "jsonrpc": "2.0",
    "id": "call-uuid-001",
    "result": {
      "content": [{ "type": "text", "text": "{\"name\":\"sort_dicts\",\"kind\":\"function\",...}" }],
      "isError": false
    }
  }
```

Note: tool execution errors use `isError: true` in the result body, NOT a JSON-RPC `error` object. JSON-RPC `error` objects signal protocol-level failures (malformed request, unknown method), not tool failures.

### 4.3 Metadata-Only Catalog (injected into system prompt every turn)

This is the ONLY thing the model sees about skills unless it calls `load_skill`. Generated by `SkillRunner.catalog_prompt()`:

```
Available skills (call load_skill(name) to activate one):
- ast-repo-map: Builds a ranked repository map for code navigation. Use when the agent needs to understand the structure of a codebase, locate symbols, or select which files to edit.
- git-ops: Performs git operations (commit, branch, log, diff). Use when committing changes, creating branches, or inspecting history.
- python-executor: Executes Python code in a sandboxed subprocess. Use when running scripts, testing snippets, or performing computations.
```

Token cost scales linearly with the number of skills. For catalogs larger than ~50 skills, embed descriptions and retrieve top-k by similarity before injecting.

### 4.4 The load_skill Tool Call

The `load_skill` meta-tool is the only tool injected at catalog time. It is passed to the LLM via `apply_chat_template(tools=[...])`:

```python
{
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "Load the full instructions for a named skill.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": sorted(runner.catalog)  # validated at call time
                }
            },
            "required": ["name"],
        },
    },
}
```

The model emits this tool call when it determines a skill is relevant. The harness calls `SkillRunner.load_skill(name)` and returns the body as the tool response, which enters conversation history for all subsequent turns.

---

## 5. Implementation Steps

### Step 1 — Define the SKILL.md JSON Schema

Document the schema as shown in Section 4.1. Implement it as a Pydantic v2 model in `services/orchestrator/skills/runner.py`:

```python
from pydantic import BaseModel, field_validator
from typing import Any

class SkillMeta(BaseModel):
    name: str
    description: str
    trigger: str | list[str] | None = None
    tools: list[Any] | None = None
    model: str | None = None
    version: str | None = None
    license: str | None = None
    requires: list[str] = []
    # preserve unknown fields
    model_config = {"extra": "allow"}
```

Add path and tier fields for internal tracking (not in frontmatter):

```python
from dataclasses import dataclass, field
from pathlib import Path

@dataclass(frozen=True)
class SkillCatalogEntry:
    meta: SkillMeta
    path: Path    # resolved, confined path to the SKILL.md
    tier: str     # "project" | "personal" | "bundled"
```

### Step 2 — SkillRunner.discover()

Scan all roots in priority order. Parse frontmatter only. Build `self.catalog: dict[str, SkillCatalogEntry]`.

```python
def discover(self) -> None:
    self.catalog.clear()
    tier_names = ["project", "personal", "bundled"]
    for tier, root in zip(tier_names, self.roots):
        if not root.is_dir():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            self._index(skill_md, tier, root)
    log.info("cataloged %d skills", len(self.catalog))  # -> stderr only

def _index(self, skill_md: Path, tier: str, root: Path) -> None:
    real = skill_md.resolve()
    if not real.is_relative_to(root.resolve()):
        log.warning("symlink escape rejected: %s", skill_md)
        return
    try:
        raw_meta, _body = frontmatter.parse(real.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("bad frontmatter in %s: %s", real, exc)
        return
    try:
        meta = SkillMeta(**raw_meta)
    except Exception as exc:
        log.warning("invalid frontmatter in %s: %s", real, exc)
        return
    if meta.name in self.catalog:
        log.warning(
            "skill '%s' shadowed: keeping %s, ignoring %s",
            meta.name, self.catalog[meta.name].path, real,
        )
        return
    self.catalog[meta.name] = SkillCatalogEntry(meta=meta, path=real, tier=tier)
```

Key constraints:
- Use `frontmatter.parse()` (which calls `yaml.safe_load` internally). Never override to `yaml.load` or `yaml.FullLoader`.
- Resolve symlinks before checking `is_relative_to` — this is the symlink escape guard.
- First (highest-priority) entry wins on name collision.
- Warn and skip on any parse or validation failure; never abort the whole scan.

### Step 3 — SkillRunner.get_catalog_prompt()

Render the catalog as the compact text block injected into the system prompt on every turn.

```python
def get_catalog_prompt(self) -> str:
    if not self.catalog:
        return ""
    lines = ["Available skills (call load_skill(name) to activate one):"]
    for entry in sorted(self.catalog.values(), key=lambda e: e.meta.name):
        lines.append(f"- {entry.meta.name}: {entry.meta.description}")
    return "\n".join(lines)
```

Also expose the `load_skill` tool schema for `apply_chat_template`:

```python
def get_tool_schema(self) -> dict:
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
```

### Step 4 — SkillRunner.load_skill(name)

Called when the model emits a `load_skill` tool call. Returns the body as the tool response.

```python
def load_skill(self, name: str) -> dict:
    self._activations += 1
    if self._activations > self.max_chain:
        return {"status": "error", "message": "skill activation limit reached"}

    entry = self.catalog.get(name)
    if entry is None:
        return {
            "status": "error",
            "message": f"unknown skill: {name}",
            "available": sorted(self.catalog),
        }

    if name in self.loaded:
        return {"status": "already_loaded", "name": name}

    # Re-validate path confinement (guard against FS changes since discovery)
    if not any(entry.path.is_relative_to(r.resolve()) for r in self.roots):
        return {"status": "error", "message": f"path confinement violation: {name}"}

    _meta, body = frontmatter.parse(entry.path.read_text(encoding="utf-8"))
    self.loaded[name] = body

    # Auto-load prerequisites declared in requires[], topologically ordered
    if entry.meta.requires:
        self._load_requires(entry.meta.requires, visited={name})

    return {"status": "loaded", "name": name, "body": body}
```

Deduplication guard: `self.loaded` tracks activated names. Second call returns `already_loaded` without re-appending the body to context.

Chain limit: `self._activations` counts total activations in the session. When it exceeds `self.max_chain` (default 8), returns an error. This caps skill-chaining loops where a body instructs the model to load another skill which loads another.

Cycle guard for `requires`: `_load_requires` performs a topological sort. If a cycle is detected (a skill appears in its own transitive `requires` closure), return an error listing the cycle members.

### Step 5 — SkillRegistry.register(name, command)

Registers an executable MCP skill. Spawns the subprocess once, runs the initialize handshake and `tools/list`, caches the result.

```python
async def register(self, manifest: SkillManifest) -> None:
    sp = SkillProcess(manifest=manifest)
    await self._spawn(sp)
    self._skills[manifest.name] = sp
    log.info("registered skill %s: %d tools", manifest.name, len(sp.tools))

async def _spawn(self, sp: SkillProcess) -> None:
    m = sp.manifest
    params = StdioServerParameters(command=m.command, args=m.args, env=m.env)
    stack = AsyncExitStack()
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()               # MCP handshake (one per subprocess lifetime)
    listed = await session.list_tools()      # cache tool schemas once
    sp.session = session
    sp.stack = stack
    sp.tools = {}
    sp.state = "READY"
    for tool in listed.tools:
        qualified = f"{m.name}.{tool.name}"
        sp.tools[tool.name] = tool.inputSchema       # live schema from tools/list
        self._tool_index[qualified] = m.name          # namespace routing table
```

No spawn-per-call. The subprocess stays alive. `_spawn` is also called by the restart path.

The `env` field on `SkillManifest` should be an explicit allowlist, not `None` (which inherits the full parent environment including secrets). At minimum, pass `{"PATH": os.environ["PATH"]}`.

### Step 6 — SkillRegistry.call_tool(skill_name, tool_name, arguments)

Routes a tool call to the correct subprocess. Validates before dispatching.

```python
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

    # Validate BEFORE the subprocess round-trip. Malformed calls never reach the process.
    jsonschema.validate(instance=arguments, schema=schema)

    sp.inflight += 1
    try:
        return await asyncio.wait_for(
            sp.session.call_tool(tool, arguments),
            timeout=self._call_timeout,
        )
    except (asyncio.TimeoutError, Exception) as exc:
        log.error("skill call %s failed: %r", qualified_name, exc)  # stderr only
        asyncio.create_task(self._maybe_restart(sp))
        raise
    finally:
        sp.inflight -= 1
```

The namespace prefix routing: `qualified_name` is always `{skill_name}.{tool_name}`. The registry strips the prefix before dispatching to `session.call_tool(tool, arguments)`.

### Step 7 — SkillRegistry Supervision Loop

Background task that detects dead skill processes between calls and restarts them with exponential backoff.

```python
async def _maybe_restart(self, sp: SkillProcess) -> None:
    async with self._lock:
        if sp.state == "DEAD":
            return   # already handled
        sp.state = "DEAD"
        # Remove dead tools from routing index
        dead_keys = [k for k, v in self._tool_index.items() if v == sp.manifest.name]
        for k in dead_keys:
            self._tool_index.pop(k, None)
        if sp.stack:
            try:
                await sp.stack.aclose()
            except Exception:
                pass
        backoff = min(2 ** sp.restarts, 30)   # 1, 2, 4, 8, 16, 30, 30, ...
        sp.restarts += 1
        log.warning(
            "restarting skill %s in %ds (attempt %d)",
            sp.manifest.name, backoff, sp.restarts,
        )
        await asyncio.sleep(backoff)
        await self._spawn(sp)    # re-runs initialize + tools/list; sets state READY

async def supervise(self, interval: float = 5.0) -> None:
    """Background health loop. Start as asyncio.create_task at harness startup."""
    while True:
        await asyncio.sleep(interval)
        for sp in list(self._skills.values()):
            if sp.state == "READY" and not self._process_alive(sp):
                log.warning("skill %s died unexpectedly", sp.manifest.name)
                asyncio.create_task(self._maybe_restart(sp))

@staticmethod
def _process_alive(sp: SkillProcess) -> bool:
    # The MCP Python SDK's stdio transport exposes the underlying process.
    # Access it via the session's transport. Exact attribute path depends on SDK internals.
    # Fallback: check sp.session is not None and sp.state is READY.
    try:
        proc = sp.session._read_stream._transport._proc  # SDK internal — verify against mcp ^1.x
        return proc.returncode is None
    except AttributeError:
        return sp.session is not None and sp.state == "READY"
```

Start the supervision loop at harness startup:

```python
asyncio.create_task(registry.supervise(interval=5.0))
```

### Step 8 — Example Skill: ast-repo-map (TypeScript MCP server)

Full implementation in `services/skills/ast-repo-map/src/index.ts`. See Section 6 for the complete code pattern.

The SKILL.md for ast-repo-map is at `services/skills/ast-repo-map/SKILL.md`. Register it in the SkillRegistry at harness startup:

```python
await registry.register(SkillManifest(
    name="ast.repo-map",
    command="node",
    args=["services/skills/ast-repo-map/dist/index.js"],
    env={"PATH": os.environ["PATH"]},
    version="0.2.0",
    language="typescript",
))
```

---

## 6. Key Code Patterns

### SkillRunner.discover() — complete implementation

```python
# services/orchestrator/skills/runner.py
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter  # python-frontmatter — uses yaml.safe_load internally
from pydantic import BaseModel

# CRITICAL: ALL log output goes to stderr. Never to stdout.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("skill_runner")


class SkillMeta(BaseModel):
    name: str
    description: str
    trigger: str | list[str] | None = None
    tools: list[Any] | None = None
    model: str | None = None
    version: str | None = None
    license: str | None = None
    requires: list[str] = []
    model_config = {"extra": "allow"}


@dataclass(frozen=True)
class SkillCatalogEntry:
    meta: SkillMeta
    path: Path
    tier: str


class SkillRunner:
    """Discovers, catalogs, and lazily activates markdown skills.

    Catalog (frontmatter only) is built eagerly at startup.
    Skill bodies load lazily on LLM-issued load_skill(name) tool calls.

    CRITICAL: This class must never write to stdout.
    All logging goes to stderr via the configured log handler.
    """

    ROOT_PRIORITY = ["project", "personal", "bundled"]

    def __init__(self, roots: list[Path], max_chain: int = 8) -> None:
        # roots[0] = project (./.claude/skills/ or ./skills/)
        # roots[1] = personal (~/.claude/skills/)
        # roots[2] = bundled (<harness>/skills/)
        self.roots = [r.expanduser().resolve() for r in roots]
        self.catalog: dict[str, SkillCatalogEntry] = {}
        self.loaded: dict[str, str] = {}   # name -> body text (activation cache)
        self.max_chain = max_chain
        self._activations = 0

    def discover(self) -> None:
        """Scan all roots, parse frontmatter only, build catalog."""
        self.catalog.clear()
        for tier, root in zip(self.ROOT_PRIORITY, self.roots):
            if not root.is_dir():
                continue
            for skill_md in sorted(root.rglob("SKILL.md")):
                self._index(skill_md, tier, root)
        log.info("cataloged %d skills", len(self.catalog))  # -> stderr

    def _index(self, skill_md: Path, tier: str, root: Path) -> None:
        real = skill_md.resolve()
        if not real.is_relative_to(root):
            log.warning("symlink escape rejected: %s", skill_md)
            return
        try:
            raw_meta, _body = frontmatter.parse(real.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("bad frontmatter in %s: %s", real, exc)
            return
        try:
            meta = SkillMeta(**raw_meta)
        except Exception as exc:
            log.warning("invalid frontmatter in %s: %s", real, exc)
            return
        if meta.name in self.catalog:
            existing = self.catalog[meta.name]
            log.warning(
                "skill '%s' shadowed: keeping %s (%s), ignoring %s (%s)",
                meta.name, existing.path, existing.tier, real, tier,
            )
            return
        self.catalog[meta.name] = SkillCatalogEntry(meta=meta, path=real, tier=tier)

    def get_catalog_prompt(self) -> str:
        if not self.catalog:
            return ""
        lines = ["Available skills (call load_skill(name) to activate one):"]
        for entry in sorted(self.catalog.values(), key=lambda e: e.meta.name):
            lines.append(f"- {entry.meta.name}: {entry.meta.description}")
        return "\n".join(lines)

    def get_tool_schema(self) -> dict[str, Any]:
        return {
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

    def load_skill(self, name: str) -> dict[str, Any]:
        self._activations += 1
        if self._activations > self.max_chain:
            return {"status": "error", "message": "skill activation limit reached"}

        entry = self.catalog.get(name)
        if entry is None:
            return {
                "status": "error",
                "message": f"unknown skill: {name}",
                "available": sorted(self.catalog),
            }

        if name in self.loaded:
            return {"status": "already_loaded", "name": name}

        # Re-validate path confinement (guards against FS changes since discover())
        if not any(entry.path.is_relative_to(r) for r in self.roots):
            return {"status": "error", "message": f"path confinement violation: {name}"}

        _meta, body = frontmatter.parse(entry.path.read_text(encoding="utf-8"))
        self.loaded[name] = body

        # Auto-load prerequisites in topological order
        if entry.meta.requires:
            for req_name in self._topo_sort(entry.meta.requires, visited={name}):
                if req_name not in self.loaded:
                    self.load_skill(req_name)

        return {"status": "loaded", "name": name, "body": body}

    def dispatch(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """Entry point called by the harness when the model emits a tool call."""
        if tool_call.get("name") != "load_skill":
            return {"status": "error", "message": f"unknown tool: {tool_call.get('name')}"}
        args = tool_call.get("arguments") or tool_call.get("parameters") or {}
        if isinstance(args, str):
            args = json.loads(args)
        return self.load_skill(args.get("name", ""))

    def _topo_sort(self, names: list[str], visited: set[str]) -> list[str]:
        """Return names in topological order, cycle-guarded by visited set."""
        result = []
        for name in names:
            if name in visited:
                log.error("cycle detected in skill requires: %s -> %s", visited, name)
                continue
            visited.add(name)
            entry = self.catalog.get(name)
            if entry and entry.meta.requires:
                result.extend(self._topo_sort(entry.meta.requires, visited))
            result.append(name)
        return result
```

### SkillRegistry.register() with asyncio subprocess spawn and MCP initialize handshake

```python
# services/orchestrator/skills/registry.py
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

import jsonschema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# CRITICAL: ALL logging must go to stderr.
# If this module itself runs as an MCP server, stdout is its JSON-RPC channel.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("skill_registry")


class SkillUnavailable(Exception):
    pass


class SkillDraining(Exception):
    pass


@dataclass
class SkillManifest:
    name: str             # namespace prefix, e.g. "ast.repo-map"
    command: str          # "node" | "python" | "/path/to/binary"
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
    state: str = "INIT"    # INIT | READY | DRAINING | DEAD
    inflight: int = 0
    restarts: int = 0


class SkillRegistry:
    """Manages long-lived MCP skill subprocesses.

    One subprocess per skill. Subprocess stays alive across calls.
    All tool calls are namespaced as {skill_name}.{tool_name}.
    """

    def __init__(self, call_timeout: float = 30.0) -> None:
        self._skills: dict[str, SkillProcess] = {}
        self._tool_index: dict[str, str] = {}   # "ns.tool" -> skill name
        self._call_timeout = call_timeout
        self._lock = asyncio.Lock()

    async def register(self, m: SkillManifest) -> None:
        sp = SkillProcess(manifest=m)
        await self._spawn(sp)
        self._skills[m.name] = sp
        log.info("registered skill %s: %d tools", m.name, len(sp.tools))  # -> stderr

    async def _spawn(self, sp: SkillProcess) -> None:
        m = sp.manifest
        params = StdioServerParameters(command=m.command, args=m.args, env=m.env)
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()           # MCP handshake — one per subprocess lifetime
        listed = await session.list_tools()  # cache tool schemas
        sp.session = session
        sp.stack = stack
        sp.tools = {}
        sp.state = "READY"
        for t in listed.tools:
            qualified = f"{m.name}.{t.name}"
            sp.tools[t.name] = t.inputSchema         # live schema from tools/list response
            self._tool_index[qualified] = m.name      # namespace routing table
```

### SkillRegistry.call_tool() with per-call timeout via anyio.fail_after

The MCP Python SDK is built on anyio, so use `anyio.fail_after` for timeout rather than `asyncio.wait_for` when the call stack may cross anyio/asyncio boundaries:

```python
    async def call_tool(self, qualified_name: str, arguments: dict) -> object:
        ns, _, tool = qualified_name.partition(".")
        sp = self._skills.get(ns)
        if sp is None or sp.state == "DEAD":
            raise SkillUnavailable(f"skill unavailable: {qualified_name}")
        if sp.state == "DRAINING":
            raise SkillDraining(f"skill draining, retry shortly: {qualified_name}")

        schema = sp.tools.get(tool)
        if schema is None:
            raise SkillUnavailable(f"no tool {tool!r} in skill {ns!r}")

        # Validate BEFORE subprocess round-trip. Bad calls never reach the process.
        try:
            jsonschema.validate(instance=arguments, schema=schema)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"invalid arguments for {qualified_name}: {exc.message}") from exc

        sp.inflight += 1
        try:
            import anyio
            with anyio.fail_after(self._call_timeout):
                result = await sp.session.call_tool(tool, arguments)
            return result
        except Exception as exc:
            log.error("skill call %s failed: %r", qualified_name, exc)  # -> stderr only
            asyncio.create_task(self._maybe_restart(sp))
            raise
        finally:
            sp.inflight -= 1
```

### The Supervision Loop (crash detection via returncode / state check)

```python
    async def _maybe_restart(self, sp: SkillProcess) -> None:
        async with self._lock:
            if sp.state == "DEAD":
                return
            sp.state = "DEAD"
            dead_keys = [k for k, v in self._tool_index.items() if v == sp.manifest.name]
            for k in dead_keys:
                self._tool_index.pop(k, None)
            if sp.stack:
                try:
                    await sp.stack.aclose()
                except Exception:
                    pass
            backoff = min(2 ** sp.restarts, 30)
            sp.restarts += 1
            log.warning(
                "restarting skill %s in %ds (attempt %d)",
                sp.manifest.name, backoff, sp.restarts,
            )
            await asyncio.sleep(backoff)
            await self._spawn(sp)   # sets sp.state = "READY"

    async def supervise(self, interval: float = 5.0) -> None:
        """Background health loop. Run as asyncio.create_task at harness startup."""
        while True:
            await asyncio.sleep(interval)
            for sp in list(self._skills.values()):
                if sp.state == "READY" and not self._process_alive(sp):
                    log.warning("skill %s died unexpectedly", sp.manifest.name)
                    asyncio.create_task(self._maybe_restart(sp))

    @staticmethod
    def _process_alive(sp: SkillProcess) -> bool:
        """Check if the subprocess underlying the MCP session is still running.

        The exact attribute path into the MCP SDK's internal transport is
        SDK-version-dependent. Verify against mcp ^1.x source.
        Fallback: treat any session as alive if state is READY.
        """
        try:
            proc = sp.session._read_stream._transport._proc
            return proc.returncode is None
        except AttributeError:
            return sp.session is not None and sp.state == "READY"

    async def reload(self, name: str) -> None:
        """Hot-reload: drain in-flight calls, swap to new process, shut down old."""
        sp = self._skills[name]
        old_stack = sp.stack
        sp.state = "DRAINING"
        while sp.inflight > 0:
            await asyncio.sleep(0.05)
        await self._spawn(sp)          # new process is now READY
        if old_stack:
            await old_stack.aclose()   # shut down old process after swap is complete
```

### TypeScript Skill Server Template (complete src/index.ts)

This is the minimum complete template for a TypeScript MCP server skill. Copy this for every new TypeScript skill and fill in the tool implementation.

```typescript
// services/skills/ast-repo-map/src/index.ts
// CRITICAL: NEVER use console.log() in a skill subprocess.
// stdout is exclusively for JSON-RPC framing.
// Use console.error() for ALL logging.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { zodToJsonSchema } from "zod-to-json-schema";

// ---------------------------------------------------------------------------
// Schema definition — Zod emits self-contained JSON Schema (no external $ref)
// ---------------------------------------------------------------------------

const GetRepoMapSchema = z.object({
  chat_files: z.array(z.string()).describe(
    "Files currently being edited; used to boost their neighbors in PageRank"
  ),
  max_tokens: z.number().int().default(2000).describe(
    "Hard token budget for the output. Never exceeded."
  ),
});

// ---------------------------------------------------------------------------
// Server setup
// ---------------------------------------------------------------------------

const server = new Server(
  { name: "ast-repo-map", version: "0.2.0" },
  { capabilities: { tools: {} } }
);

// ---------------------------------------------------------------------------
// tools/list handler
// ---------------------------------------------------------------------------

server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: "get_repo_map",
        description:
          "Return a PageRank-ranked JSONL symbol map of the repository within a token budget.",
        // zodToJsonSchema produces a self-contained schema — no external $ref
        inputSchema: zodToJsonSchema(GetRepoMapSchema, { target: "jsonSchema7" }),
      },
    ],
  };
});

// ---------------------------------------------------------------------------
// tools/call handler
// ---------------------------------------------------------------------------

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  if (name === "get_repo_map") {
    // Parse and validate with Zod (throws on bad input — MCP SDK catches and returns isError:true)
    const params = GetRepoMapSchema.parse(args);

    // --- actual implementation goes here ---
    // Replace this stub with tree-sitter parsing + PageRank logic.
    console.error("[ast-repo-map] get_repo_map called, chat_files:", params.chat_files);

    const stubResult = JSON.stringify({
      name: "example_function",
      kind: "function",
      signature: "def example_function():",
      parent: null,
      loc: "src/example.py:10",
    });

    return {
      content: [{ type: "text", text: stubResult }],
      isError: false,
    };
  }

  // Unknown tool
  return {
    content: [{ type: "text", text: `Unknown tool: ${name}` }],
    isError: true,
  };
});

// ---------------------------------------------------------------------------
// Start the server
// ---------------------------------------------------------------------------

async function main() {
  // All startup logging must go to stderr
  console.error("[ast-repo-map] starting MCP server on stdio");
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[ast-repo-map] ready");
}

main().catch((err) => {
  console.error("[ast-repo-map] fatal error:", err);
  process.exit(1);
});
```

`package.json` for the skill:

```json
{
  "name": "ast-repo-map",
  "version": "0.2.0",
  "type": "module",
  "main": "dist/index.js",
  "scripts": {
    "build": "tsc",
    "start": "node dist/index.js"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.0.0",
    "zod": "^4.0.0",
    "zod-to-json-schema": "^3.24.5"
  },
  "devDependencies": {
    "typescript": "^5.0.0",
    "@types/node": "^22.0.0"
  }
}
```

`tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "outDir": "dist",
    "strict": true,
    "esModuleInterop": true
  },
  "include": ["src"]
}
```

### Stderr-Only Logging in Every Language

This is the single most important operational constraint. Enforce it before writing any other skill code.

**TypeScript / Node.js:**
```typescript
// CORRECT — writes to stderr
console.error("[skill-name] message:", value);

// WRONG — corrupts the JSON-RPC stream on stdout
// console.log("[skill-name] message:", value);   // NEVER
```

**Python MCP server subprocess:**
```python
import logging
import sys

# Required at module top — before any imports that might print on load
logging.basicConfig(
    stream=sys.stderr,       # MUST be stderr
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("skill-name")

# CORRECT
log.info("processing request: %s", request_id)

# WRONG — corrupts the JSON-RPC stream
# print("processing request:", request_id)   # NEVER
```

**Rust MCP server subprocess:**
```rust
use tracing_subscriber::fmt;

fn main() {
    // Direct ALL tracing output to stderr — required before any other initialization
    fmt()
        .with_writer(std::io::stderr)
        .init();

    // CORRECT
    tracing::info!("skill starting");

    // WRONG — corrupts the JSON-RPC stream
    // println!("skill starting");   // NEVER
    // dbg!(...);                    // NEVER — writes to stderr via dbg! but also stdout in some contexts
}
```

Check your dependencies. If any imported library writes a startup banner to stdout on import, the MCP session will fail before `initialize` is even received. Test with:

```bash
node dist/index.js < /dev/null 2>/dev/null | head -c 100
# Should produce zero bytes of output. Any bytes = stdout pollution.
```

---

## 7. Integration Verification

### 7.1 Create a Minimal SKILL.md

Create `services/skills/hello-world/SKILL.md`:

```markdown
---
name: hello-world
description: A minimal test skill. Use when verifying the skill framework is working.
version: "0.1.0"
license: MIT
---

# Hello World Skill

This skill is a framework integration test. It has no executable component.
If you can read this, the skill body loaded correctly.
```

### 7.2 Verify SkillRunner Discovers It

```python
from pathlib import Path
from services.orchestrator.skills.runner import SkillRunner

runner = SkillRunner(roots=[
    Path("services/skills"),    # bundled tier
])
runner.discover()

assert "hello-world" in runner.catalog, "skill not discovered"
entry = runner.catalog["hello-world"]
assert entry.meta.description.startswith("A minimal test skill")
assert "body" not in entry.__dict__   # body not loaded yet

print("catalog_prompt:", runner.get_catalog_prompt())
# Expected: "Available skills ...\n- hello-world: A minimal test skill..."
```

### 7.3 Verify lazy body loading via load_skill

```python
result = runner.load_skill("hello-world")
assert result["status"] == "loaded"
assert "Hello World Skill" in result["body"]

# Second call returns already_loaded — body is not re-appended to context
result2 = runner.load_skill("hello-world")
assert result2["status"] == "already_loaded"
```

### 7.4 Verify SkillRegistry Can Call a Tool

Build the ast-repo-map skill first (`cd services/skills/ast-repo-map && npm run build`), then:

```python
import asyncio
from services.orchestrator.skills.registry import SkillRegistry, SkillManifest

async def test_registry():
    registry = SkillRegistry(call_timeout=10.0)
    asyncio.create_task(registry.supervise(interval=5.0))

    await registry.register(SkillManifest(
        name="ast.repo-map",
        command="node",
        args=["services/skills/ast-repo-map/dist/index.js"],
    ))

    assert "ast.repo-map" in registry._skills
    sp = registry._skills["ast.repo-map"]
    assert sp.state == "READY"
    assert "get_repo_map" in sp.tools

    result = await registry.call_tool(
        "ast.repo-map.get_repo_map",
        {"chat_files": ["src/example.py"], "max_tokens": 500},
    )
    assert result.isError is False
    assert len(result.content) > 0

asyncio.run(test_registry())
```

### 7.5 Verify the Supervision Loop Restarts a Crashed Skill

```python
async def test_supervision():
    registry = SkillRegistry(call_timeout=5.0)
    asyncio.create_task(registry.supervise(interval=1.0))   # 1s check interval for test speed

    await registry.register(SkillManifest(
        name="ast.repo-map",
        command="node",
        args=["services/skills/ast-repo-map/dist/index.js"],
    ))

    sp = registry._skills["ast.repo-map"]
    initial_restarts = sp.restarts

    # Force a crash: kill the underlying process
    try:
        proc = sp.session._read_stream._transport._proc
        proc.kill()
    except AttributeError:
        # Fallback: call a tool that will fail and trigger restart
        pass

    # Wait for supervision loop to detect and restart
    await asyncio.sleep(3.0)   # 1s poll + 1s backoff (2^0) + margin
    assert sp.restarts > initial_restarts, "supervision loop did not restart the skill"
    assert sp.state == "READY", f"skill did not recover: {sp.state}"

asyncio.run(test_supervision())
```

---

## 8. Done Criteria

The skills framework is working when ALL of the following are true:

- [ ] `SkillRunner.discover()` scans three tiers in priority order and builds a catalog of `SkillMeta` objects with no body text in memory
- [ ] Name collision between tiers: project tier wins, warning logged to stderr
- [ ] Malformed frontmatter (missing `name` or `description`, invalid YAML): skill is skipped, warning logged, discovery continues
- [ ] Symlink pointing outside a root: rejected with a warning, no file opened outside roots
- [ ] `frontmatter.parse()` is used everywhere SKILL.md is read; `yaml.load` / `yaml.FullLoader` does not appear anywhere in the codebase
- [ ] `get_catalog_prompt()` returns a compact text block with one line per skill (name + description only)
- [ ] `get_tool_schema()` returns a valid `load_skill` tool definition with `enum` constrained to known skill names
- [ ] `load_skill("valid-name")` returns `{"status": "loaded", "body": "<markdown body>"}` on first call
- [ ] `load_skill("valid-name")` returns `{"status": "already_loaded"}` on subsequent calls without re-reading the file
- [ ] `load_skill("unknown-name")` returns `{"status": "error", ...}` with the list of valid names
- [ ] `load_skill` activation counter prevents more than `max_chain` activations per session
- [ ] `requires` cycle detection: a skill that requires itself (directly or transitively) produces an error, not infinite recursion
- [ ] `SkillRegistry.register()` spawns a subprocess, runs `initialize`, runs `tools/list`, caches schemas, sets state `READY`
- [ ] All tools are indexed as `{skill_name}.{tool_name}` — no unqualified tool names in `_tool_index`
- [ ] `call_tool()` runs `jsonschema.validate()` against the cached schema before dispatching to the subprocess
- [ ] `call_tool()` with invalid arguments raises `ValueError` without sending any JSON-RPC to the subprocess
- [ ] `call_tool()` wraps the subprocess round-trip in a per-call timeout (anyio or asyncio)
- [ ] `call_tool()` on a `DEAD` skill raises `SkillUnavailable` immediately (no hang)
- [ ] Supervision loop detects a killed subprocess within `interval` seconds and triggers restart
- [ ] Restart uses exponential backoff: delays are 1, 2, 4, 8, 16, 30, 30, ... seconds
- [ ] Hot-reload (`reload(name)`) drains in-flight calls before swapping process
- [ ] TypeScript skill subprocess: `node dist/index.js < /dev/null 2>/dev/null | wc -c` outputs `0` (zero stdout bytes at startup)
- [ ] Python skill subprocess: same test passes; no bare `print()` calls exist in any skill subprocess file
- [ ] The ast-repo-map TypeScript skill builds (`npm run build` succeeds) and responds to `tools/list` and `tools/call`
- [ ] The harness injects the catalog prompt into the system prompt and the `load_skill` tool schema into `apply_chat_template` on every turn

---

## 9. Common Mistakes

### 1. Stdout Pollution — the #1 subprocess killer (CRITICAL)

The stdio transport reserves stdout exclusively for JSON-RPC 2.0 framing. Any write to stdout that is not a JSON-RPC message — a `print()`, a `console.log()`, a `println!()`, a library startup banner, an ANSI escape sequence — interleaves with framing bytes and breaks the host's JSON parser.

The error messages are misleading: `"Unexpected token"`, `"JSON Parse error: Unexpected identifier"`, `"Method not found: notifications/initialized"`. None of them say "stdout pollution."

Enforce this before writing any skill logic:
- TypeScript: `console.log` is banned. `console.error` only.
- Python: `logging.basicConfig(stream=sys.stderr)` at the top of every skill server file. No bare `print()`.
- Rust: `tracing_subscriber::fmt().with_writer(std::io::stderr).init()`. No `println!()` or `dbg!()`.

Also check your dependencies. A Python package that prints a banner on import corrupts the stream before `initialize` is received.

Test: `node dist/index.js < /dev/null 2>/dev/null | wc -c` must output `0`.

### 2. Eager Loading Skill Bodies at Startup

Loading all skill bodies into the system prompt at startup defeats the entire purpose of progressive disclosure. With Gemma 4's context window, five skills at 400 lines each will consume context needed for actual task code. Parse frontmatter only at `discover()` time. Bodies enter context only on explicit `load_skill` calls. If you find yourself calling `load_skill` in a loop at startup, stop.

### 3. Cycle in Skill Chaining

A skill body that instructs the model to `load_skill("skill-a")`, which loads `skill-b`, which loads `skill-a`, produces unbounded context growth. Two guards are required and both must be present:
- **Deduplication**: `self.loaded` tracks activated names. Re-requesting a loaded skill returns `already_loaded` without re-appending the body.
- **Chain limit**: `self.max_chain` (default 8) caps total `load_skill` calls per session. The 9th call returns an error.

If only one guard is present the other failure mode is still exploitable.

### 4. Forgetting the initialize Handshake Before tools/list

The MCP protocol requires the full three-step initialize handshake before any other method is called:
1. Client sends `initialize` request
2. Server responds with its capabilities
3. Client sends `notifications/initialized` notification

Calling `tools/list` or `tools/call` before this sequence results in `"Method not found"` errors from the subprocess. The MCP Python SDK's `session.initialize()` handles all three steps. Do not skip it or call it out of order.

### 5. Hand-Authoring JSON Schema Instead of Using the Cached Schema

Never hand-write the `inputSchema` for a tool in a manifest file. Use the schema the skill subprocess returns from `tools/list`. That schema is generated from Zod (TypeScript), Pydantic (Python), or schemars (Rust) and is the single source of truth. A separately maintained schema will silently diverge from the actual parameter handling as the skill evolves, causing jsonschema validation to pass for inputs the skill rejects, or reject inputs the skill accepts.

Cache `t.inputSchema` from `listed.tools` in `_spawn()`. Never read it from a config file.

### 6. External `$ref` in inputSchema

MCP tool `inputSchema` must be self-contained. Some JSON Schema generators emit `$ref` pointing to sibling `$defs` keys; that is fine. But generators that emit `$ref` pointing to external files or URLs produce schemas the host cannot resolve. Use `zodToJsonSchema(schema, { target: "jsonSchema7" })` in TypeScript which inlines all definitions. In Python, use `model.model_json_schema()` (Pydantic v2 inlines by default). In Rust, use `schemars` with `inline_subschemas = true`.

### 7. Using yaml.load() on SKILL.md Frontmatter

Never call `yaml.load()` or `yaml.FullLoader` on any file that is user or community content. The unsafe loader can instantiate arbitrary Python objects via YAML tags such as `!!python/object/apply:os.system`. `python-frontmatter` uses `yaml.safe_load` by default — do not override it. If you ever need to parse YAML directly, use `yaml.safe_load(text)`, never `yaml.load(text, Loader=yaml.FullLoader)`.

### 8. No Per-Call Timeout

A skill subprocess that crashes mid-call leaves `session.call_tool(...)` awaiting forever with no timeout and no supervision. Every `call_tool` invocation must be wrapped in a timeout. Use `anyio.fail_after(self._call_timeout)` (preferred, since the MCP SDK is anyio-based) or `asyncio.wait_for(..., timeout=...)`. The supervision loop catches dead processes between calls; the timeout handles hangs during calls. Both are required.

### 9. Spawn-Per-Call

Spawning a new subprocess for every tool call is 100-1000x slower than reusing a persistent subprocess. Node.js V8 startup alone is ~50–200ms. Python interpreter + library imports can be 500ms or more. Rust binaries are fast but still fork overhead. The SkillRegistry spawns once at `register()` time and reuses the session for all calls. If you see subprocess spawning in the `call_tool` hot path, it is a bug.
