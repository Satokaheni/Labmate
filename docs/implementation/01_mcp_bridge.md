# Implementation Plan — TypeScript MCP Bridge (`lm-mcp-bridge`)

**Labmate v0.1 — Implementation Plan**
**Spec reference:** `research/llm-harness-research/specs/spec_mcp_bridge.md`
**Contract reference:** `docs/implementation/00_contracts.md` (Contract B)
**Status:** Ready for implementation

---

## 1. What This Service Does

The MCP Bridge is a long-lived Node.js/TypeScript process that translates JSON-RPC 2.0 messages received over `stdin` from the Python orchestrator into tool dispatch calls, and writes JSON-RPC 2.0 responses back over `stdout`. It uses the `@modelcontextprotocol/sdk` `McpServer` class to handle capability negotiation, request routing, and schema advertisement automatically. Tool handlers are organized by domain (`fs`, `git`, `exec`) and registered at startup; the bridge never spawns or tears down tool handlers at runtime. For tools that require a child process (skill servers), the bridge spawns that child process, connects to it over its own stdio using the MCP client API, and relays the call through — the bridge is the server to the orchestrator and simultaneously a client to each skill child. The bridge does NOT contain business logic, does NOT talk to MongoDB/Chroma/Redis directly, does NOT implement the Python orchestrator's session management, and does NOT write anything to `stdout` except valid JSON-RPC messages.

---

## 2. Dependencies

### Must exist before building

- Node.js `>=20` and `npm` available in the build environment.
- `@modelcontextprotocol/sdk` (exact version pinned — see Step 2). The `McpServer` and `StdioServerTransport` APIs used here are stable only within a pinned version.

### Must exist before integration testing

- The Python orchestrator (`lm-orchestrator`) must be runnable to serve as the JSON-RPC client.
- At least one skill child process must be implemented to test the relay path (the `exec` domain tools are a good first target). For unit tests of the bridge itself, skill child processes can be stubs.
- `00_contracts.md` Contract B defines the exact wire format. Any change to message shapes must be reflected there first.

### Runtime connections

- Upstream: Python orchestrator connects to the bridge's `stdin`/`stdout` (the bridge is a subprocess of the orchestrator).
- Downstream: The bridge spawns skill child processes and connects to them over their `stdin`/`stdout` using `StdioClientTransport` from the MCP SDK.
- No TCP ports are opened by this service.

---

## 3. File Structure

```
services/mcp-bridge/
├── package.json              — ESM module, pinned deps, build/start/dev scripts
├── tsconfig.json             — strict: true, target ES2022, module Node16
├── src/
│   ├── index.ts              — entry point: bootstrap McpServer, wire transport, SIGINT/SIGTERM/uncaughtException
│   ├── registry.ts           — registerAllTools(server): calls every domain registrar in one place
│   ├── constants.ts          — CHARACTER_LIMIT = 25_000 and tool-name prefix constants
│   ├── types.ts              — shared TypeScript types derived from Zod schemas via z.infer<>
│   ├── tools/
│   │   ├── fs.ts             — registerFsTools(server): fs_read_file, fs_write_file, fs_list_dir, fs_delete_file
│   │   ├── git.ts            — registerGitTools(server): git_status, git_diff, git_commit, git_log
│   │   └── exec.ts           — registerExecTools(server): exec_run_command — relays to skill child process via IPC
│   ├── schemas/
│   │   ├── fs.ts             — Zod schemas for all fs tool inputs (FsReadInput, FsWriteInput, etc.)
│   │   ├── git.ts            — Zod schemas for all git tool inputs
│   │   └── exec.ts           — Zod schemas for exec tool inputs
│   ├── services/
│   │   ├── logger.ts         — pino instance writing to fd 2 (stderr); never stdout
│   │   └── ipc.ts            — spawnSkillClient(command, args): spawns a skill child process, returns a connected MCP ClientSession
│   └── utils/
│       └── truncate.ts       — truncate(text, offset, limit): returns { text, has_more, next_offset, total }
```

---

## 4. Interface Contracts

All message shapes are defined authoritatively in `00_contracts.md` Contract B. The examples below show the exact bytes the bridge reads from stdin and writes to stdout.

### 4.1 Initialize handshake

The Python orchestrator (MCP client) sends `initialize`. The bridge (MCP server) responds. The SDK handles this automatically once `server.connect(transport)` is called — no manual handler needed.

```
→ stdin:
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"labmate-orchestrator","version":"1.0.0"}}}

← stdout:
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"labmate-mcp-bridge","version":"1.0.0"}}}

→ stdin:
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
```

The bridge never writes anything before `server.connect(transport)` completes. The `log.info('server ready')` line writes to **stderr**, not stdout.

### 4.2 Incoming tool call (orchestrator → bridge) and response (bridge → orchestrator)

```
→ stdin:
{"jsonrpc":"2.0","id":"call-uuid-001","method":"tools/call","params":{"name":"fs_read_file","arguments":{"path":"/workspace/main.py","offset":0,"limit":25000}}}

← stdout:
{"jsonrpc":"2.0","id":"call-uuid-001","result":{"content":[{"type":"text","text":"import sys\n..."}],"isError":false}}
```

### 4.3 Bridge relaying that call to a skill child process (bridge → skill)

For tools in `exec.ts` that delegate to a skill child process, the bridge uses its own MCP client (`ipc.ts`) to forward the call over the skill's `stdin`/`stdout`:

```
bridge spawns: node /skills/exec-runner/dist/index.js
bridge → skill stdin:
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"lm-mcp-bridge","version":"0.1.0"}}}

skill → bridge stdout:
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"exec-runner","version":"0.1.0"}}}

bridge → skill stdin:
{"jsonrpc":"2.0","id":"relay-001","method":"tools/call","params":{"name":"exec_run_command","arguments":{"command":"pytest","args":["tests/","--tb=short"]}}}

skill → bridge stdout:
{"jsonrpc":"2.0","id":"relay-001","result":{"content":[{"type":"text","text":"...test output..."}],"isError":false}}
```

The bridge then copies the skill's result into a `CallToolResult` and writes it back to the orchestrator on its own stdout. The relay is transparent to the orchestrator.

### 4.4 Tool error case

Errors are returned as `isError: true` in the `result` field — never as a JSON-RPC `error` object. The orchestrator can read and act on `isError: true`; a JSON-RPC protocol error would surface as an exception in the Python SDK that the orchestrator cannot reason about.

```
← stdout:
{"jsonrpc":"2.0","id":"call-uuid-002","result":{"content":[{"type":"text","text":"Error reading /workspace/missing.py: ENOENT: no such file or directory"}],"isError":true}}
```

A JSON-RPC protocol error looks like this and must NOT be used for tool failures:

```json
{"jsonrpc":"2.0","id":"call-uuid-002","error":{"code":-32603,"message":"Internal error"}}
```

### 4.5 Pagination for large results

When the content to return exceeds `CHARACTER_LIMIT` (25,000 characters), return the first page with pagination metadata in a second `text` content block. The orchestrator can call again with `offset` to retrieve subsequent pages.

```
← stdout (first page of a large file):
{
  "jsonrpc": "2.0",
  "id": "call-uuid-003",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "<first 25000 chars of content>\n\n[TRUNCATED: showing chars 0–25000 of 87432 total. Call again with offset=25000 to continue.]"
      },
      {
        "type": "text",
        "text": "{\"has_more\": true, \"next_offset\": 25000, \"total\": 87432}"
      }
    ],
    "isError": false
  }
}
```

The second text block carries machine-readable pagination metadata. The truncation notice in the first block carries human-readable metadata for the LLM.

---

## 5. Implementation Steps

Each step is a self-contained coding task. Steps 1–4 establish the skeleton; steps 5–10 fill in the tools; steps 11–13 cover integration.

**Step 1: Initialize the npm project.**

Create `services/mcp-bridge/package.json`:

```json
{
  "name": "lm-mcp-bridge",
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "build": "tsc",
    "start": "node dist/index.js",
    "dev":   "tsx watch src/index.ts"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "1.12.1",
    "pino": "^9.0.0",
    "zod": "^3.22.0"
  },
  "devDependencies": {
    "@types/node": "^22.0.0",
    "tsx": "^4.19.0",
    "typescript": "^5.4.0"
  }
}
```

Pin `@modelcontextprotocol/sdk` to an exact version. Check `npm info @modelcontextprotocol/sdk version` for the latest `1.x.y` and use that. Do not use `^1.x.y` — the SDK moves fast and floating versions pull breaking changes.

**Step 2: Create `tsconfig.json`.**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "Node16",
    "moduleResolution": "Node16",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "declaration": true,
    "declarationMap": true,
    "sourceMap": true
  },
  "include": ["src/**/*"],
  "exclude": ["node_modules", "dist"]
}
```

**Step 3: Install dependencies.**

```bash
cd services/mcp-bridge && npm install
```

Verify the lock file is committed. Never run `npm install` without committing `package-lock.json`.

**Step 4: Create `src/constants.ts` and `src/services/logger.ts`.**

These must exist before any other file imports them.

`src/constants.ts`:
```typescript
export const CHARACTER_LIMIT = 25_000;

export const TOOL_PREFIX = {
  FS:   'fs_',
  GIT:  'git_',
  EXEC: 'exec_',
} as const;
```

`src/services/logger.ts`:
```typescript
import pino from 'pino';

// CRITICAL: destination fd 2 = stderr.
// stdout is reserved exclusively for JSON-RPC messages.
// Any log line on stdout corrupts the protocol.
export const log = pino(
  { level: process.env.LOG_LEVEL ?? 'info' },
  pino.destination(2),  // fd 2 = stderr
);
```

**Step 5: Create `src/utils/truncate.ts`.**

```typescript
import { CHARACTER_LIMIT } from '../constants.js';

export interface TruncateResult {
  text:        string;
  has_more:    boolean;
  next_offset: number | null;
  total:       number;
}

export function truncate(
  text:   string,
  offset: number = 0,
  limit:  number = CHARACTER_LIMIT,
): TruncateResult {
  const slice    = text.slice(offset, offset + limit);
  const has_more = offset + limit < text.length;
  return {
    text: slice + (has_more
      ? `\n\n[TRUNCATED: showing chars ${offset}–${offset + slice.length} of ${text.length} total. ` +
        `Call again with offset=${offset + limit} to continue.]`
      : ''),
    has_more,
    next_offset: has_more ? offset + limit : null,
    total: text.length,
  };
}
```

**Step 6: Create Zod schemas in `src/schemas/`.**

Create `src/schemas/fs.ts`, `src/schemas/git.ts`, `src/schemas/exec.ts`. Every schema must use `.strict()` (rejects unknown keys) and every field must carry a `.describe()` string (populates the JSON Schema description that the LLM reads).

Example `src/schemas/fs.ts`:
```typescript
import { z } from 'zod';

export const FsReadInput = z.object({
  path:   z.string().describe('Absolute path of the file to read.'),
  offset: z.number().int().min(0).default(0)
            .describe('Character offset to start reading (for pagination).'),
  limit:  z.number().int().min(1).default(25_000)
            .describe('Maximum characters to return. Default is CHARACTER_LIMIT.'),
}).strict();

export const FsWriteInput = z.object({
  path:    z.string().describe('Absolute path of the file to write.'),
  content: z.string().describe('UTF-8 content to write to the file.'),
  append:  z.boolean().default(false).describe('If true, append instead of overwrite.'),
}).strict();

export const FsListDirInput = z.object({
  path:      z.string().describe('Absolute path of the directory to list.'),
  recursive: z.boolean().default(false).describe('If true, list recursively.'),
}).strict();

export const FsDeleteInput = z.object({
  path: z.string().describe('Absolute path of the file to delete.'),
}).strict();

export type FsReadInput   = z.infer<typeof FsReadInput>;
export type FsWriteInput  = z.infer<typeof FsWriteInput>;
export type FsListDirInput = z.infer<typeof FsListDirInput>;
export type FsDeleteInput = z.infer<typeof FsDeleteInput>;
```

**Step 7: Create `src/services/ipc.ts`.**

This module handles spawning a skill child process and connecting to it as an MCP client. It is used by `exec.ts` tools that relay calls to skill processes.

```typescript
import { Client }               from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { log }                  from './logger.js';

export interface SkillClient {
  callTool(name: string, args: Record<string, unknown>): Promise<unknown>;
  close(): Promise<void>;
}

export async function spawnSkillClient(
  command: string,
  args:    string[] = [],
): Promise<SkillClient> {
  const transport = new StdioClientTransport({ command, args });
  const client    = new Client(
    { name: 'lm-mcp-bridge', version: '0.1.0' },
    { capabilities: {} },
  );

  await client.connect(transport);
  log.info({ command, args }, 'skill client connected');

  return {
    async callTool(name, toolArgs) {
      return client.callTool({ name, arguments: toolArgs });
    },
    async close() {
      await client.close();
    },
  };
}
```

**Step 8: Create tool domain modules in `src/tools/`.**

Create `src/tools/fs.ts`, `src/tools/git.ts`, `src/tools/exec.ts`. Each exports a single `registerXxxTools(server: McpServer): void` function. Tool handlers must never throw — wrap every handler body in `try/catch` and return `isError: true` on any error.

See section 6 for the full code pattern.

**Step 9: Create `src/registry.ts`.**

```typescript
import { McpServer }          from '@modelcontextprotocol/sdk/server/mcp.js';
import { registerFsTools }    from './tools/fs.js';
import { registerGitTools }   from './tools/git.js';
import { registerExecTools }  from './tools/exec.js';

export function registerAllTools(server: McpServer): void {
  registerFsTools(server);
  registerGitTools(server);
  registerExecTools(server);
}
```

**Step 10: Create `src/index.ts` (entry point).**

See section 6 for the full bootstrap pattern. This file must not contain any tool definitions — it only wires together `McpServer`, `StdioServerTransport`, `registerAllTools`, and signal handlers.

**Step 11: Build and smoke-test.**

```bash
cd services/mcp-bridge
npm run build            # produces dist/index.js
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}' | node dist/index.js
```

Verify that the response on stdout is a valid JSON-RPC `initialize` result and that no non-JSON bytes appear on stdout. The `server ready` log line must appear on stderr only.

**Step 12: Write tool unit tests.**

For each tool handler, write a test that:
- Calls the handler directly (not through the MCP server) with valid args and asserts the result shape.
- Calls it with invalid args and asserts `isError: true` in the result.
- Calls it with a large output source and asserts truncation at `CHARACTER_LIMIT`.

Use Node's built-in `node:test` runner or vitest. Do not use jest (ESM compatibility issues with `"type": "module"`).

**Step 13: Integration test with a live orchestrator.**

See section 7.

---

## 6. Key Code Patterns

### 6.1 Registering a tool on McpServer

The `server.registerTool` signature takes: a name string, a config object (with `title`, `description`, `inputSchema`, and optionally `annotations`), and an async handler function. Pass `Schema.shape` for `inputSchema` — not the schema object directly.

```typescript
// src/tools/fs.ts
import { McpServer }  from '@modelcontextprotocol/sdk/server/mcp.js';
import { readFile }   from 'node:fs/promises';
import { FsReadInput } from '../schemas/fs.js';
import { truncate }   from '../utils/truncate.js';
import { log }        from '../services/logger.js';

export function registerFsTools(server: McpServer): void {
  server.registerTool(
    'fs_read_file',
    {
      title:       'Read file',
      description: 'Read a UTF-8 text file with character-offset pagination.',
      inputSchema: FsReadInput.shape,   // .shape, not the schema itself
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async (args) => {
      // args is already validated by Zod before this handler runs
      try {
        const content = await readFile(args.path, 'utf8');   // async: never block the event loop
        const { text, has_more, next_offset, total } = truncate(
          content, args.offset, args.limit,
        );
        return {
          content: [
            { type: 'text', text },
            { type: 'text', text: JSON.stringify({ has_more, next_offset, total }) },
          ],
          isError: false,
        };
      } catch (err) {
        log.error({ err, path: args.path }, 'fs_read_file failed');   // stderr only
        const msg = err instanceof Error ? err.message : String(err);
        return {
          content: [{ type: 'text', text: `Error reading ${args.path}: ${msg}` }],
          isError: true,
        };
      }
    },
  );
}
```

### 6.2 Handling tool errors (the isError pattern)

Tool handlers must never throw. An uncaught exception becomes a JSON-RPC protocol error that the Python SDK surfaces as a hard exception — the orchestrator cannot inspect the error text, reason about it, or retry intelligently.

```typescript
// Reusable wrapper for handlers that call out to skills or external processes
async function safeCall<T>(
  label: string,
  fn: () => Promise<T>,
): Promise<T | { content: [{ type: 'text'; text: string }]; isError: true }> {
  try {
    return await fn();
  } catch (err) {
    log.error({ err }, `${label} failed`);   // stderr
    const msg = err instanceof Error ? err.message : String(err);
    return {
      content: [{ type: 'text', text: `${label}: ${msg}` }],
      isError: true,
    };
  }
}

// Usage inside a handler:
return safeCall('exec_run_command', async () => {
  const skillClient = await spawnSkillClient('node', ['/skills/exec-runner/dist/index.js']);
  const result = await skillClient.callTool('exec_run_command', args);
  await skillClient.close();
  return result as ReturnType<typeof server.callTool>;
});
```

### 6.3 Spawning a skill child process and piping JSON-RPC to/from it

The bridge uses `StdioClientTransport` from the MCP SDK to spawn a skill subprocess. The SDK handles the JSON-RPC framing over `stdin`/`stdout` of the child process automatically.

```typescript
// src/tools/exec.ts
import { McpServer }          from '@modelcontextprotocol/sdk/server/mcp.js';
import { spawnSkillClient }   from '../services/ipc.js';
import { ExecRunInput }       from '../schemas/exec.js';
import { log }                from '../services/logger.js';

const EXEC_RUNNER = process.env.EXEC_RUNNER_BIN ?? 'node';
const EXEC_RUNNER_ARGS = (process.env.EXEC_RUNNER_ARGS ?? '/skills/exec-runner/dist/index.js').split(' ');

export function registerExecTools(server: McpServer): void {
  server.registerTool(
    'exec_run_command',
    {
      title:       'Run command',
      description: 'Execute a shell command in the workspace sandbox.',
      inputSchema: ExecRunInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: false },
    },
    async (args) => {
      try {
        const skill = await spawnSkillClient(EXEC_RUNNER, EXEC_RUNNER_ARGS);
        try {
          const result = await skill.callTool('exec_run_command', args as Record<string, unknown>);
          return result as { content: { type: string; text: string }[]; isError: boolean };
        } finally {
          await skill.close().catch((e) => log.warn({ e }, 'skill close failed'));
        }
      } catch (err) {
        log.error({ err }, 'exec_run_command failed');
        const msg = err instanceof Error ? err.message : String(err);
        return {
          content: [{ type: 'text', text: `exec_run_command: ${msg}` }],
          isError: true,
        };
      }
    },
  );
}
```

Note: `spawnSkillClient` above creates a new child process per call. For high-frequency tools, consider caching the `SkillClient` instance as a module-level singleton and reconnecting on error instead of spawning per call.

### 6.4 Writing to stderr only — never stdout

```typescript
// CORRECT: log to stderr via pino with fd 2
import { log } from './services/logger.js';
log.info({ path }, 'reading file');
log.error({ err }, 'handler failed');

// CORRECT: debug writes
process.stderr.write('debug: something happened\n');

// WRONG — corrupts the JSON-RPC stream:
console.log('something');          // goes to stdout
process.stdout.write('debug\n');   // corrupts the stream
console.error('ok');               // WRONG: console.error goes to stderr but avoid it —
                                   // use log.error instead for structured output
```

`console.error` writes to stderr and is safe but unstructured. Prefer `log.error` from pino for all log output so log lines are machine-parseable JSON on stderr.

### 6.5 Graceful shutdown pattern

```typescript
// src/index.ts
import { McpServer }            from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { registerAllTools }     from './registry.js';
import { log }                  from './services/logger.js';

async function main(): Promise<void> {
  const server    = new McpServer({ name: 'labmate-mcp-bridge', version: '0.1.0' });
  const transport = new StdioServerTransport();

  registerAllTools(server);

  let shuttingDown = false;

  const shutdown = async (sig: string): Promise<void> => {
    // Guard: re-entrant signals are a no-op
    if (shuttingDown) return;
    shuttingDown = true;
    log.info({ sig }, 'shutting down');   // stderr
    try {
      await server.close();       // stop accepting new requests
      await transport.close();    // close stdio channel
    } finally {
      process.exit(0);
    }
  };

  // SIGINT:  Ctrl-C in terminal
  // SIGTERM: container orchestrators (k8s, RunPod, Docker)
  process.on('SIGINT',  () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));

  // Last-resort flush: log and shut down on uncaught throws
  process.on('uncaughtException', (err) => {
    log.fatal({ err }, 'uncaught exception');
    void shutdown('uncaughtException');
  });
  process.on('unhandledRejection', (reason) => {
    log.fatal({ reason }, 'unhandled rejection');
    void shutdown('unhandledRejection');
  });

  // This begins reading stdin / writing stdout.
  // JSON-RPC ONLY from this point forward on stdout.
  await server.connect(transport);
  log.info('labmate MCP bridge ready on stdio');   // stderr
}

main().catch((err) => {
  // pino may not be initialized yet — use process.stderr directly
  process.stderr.write(`fatal startup error: ${String(err)}\n`);
  process.exit(1);
});
```

---

## 7. Integration Verification

### 7.1 Run the bridge manually and confirm stderr/stdout separation

```bash
cd services/mcp-bridge
npm run build

# Redirect stdout to a file, stderr to terminal
node dist/index.js >bridge_stdout.log 2>&1 &
BRIDGE_PID=$!

# Send an initialize message
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}' \
  | node dist/index.js

kill $BRIDGE_PID 2>/dev/null
```

Expected: the response on stdout starts with `{"jsonrpc":"2.0","id":1,"result":`. The log line `labmate MCP bridge ready on stdio` must appear in the stderr stream only.

### 7.2 Send a test tool call and inspect the response

```bash
# Send initialize + initialized notification + a tools/list call, one message per line
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | node dist/index.js 2>/dev/null \
  | python3 -m json.tool   # pretty-print each JSON-RPC line
```

Expected: two JSON-RPC responses, one for id 1 (initialize result) and one for id 2 (tools/list result containing all registered tools). No parse errors.

### 7.3 Verify a tool call reaches a skill and returns a result

```bash
# Requires the exec-runner skill to be built and available
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"exec_run_command","arguments":{"command":"echo","args":["hello from skill"]}}}' \
  | EXEC_RUNNER_ARGS='/skills/exec-runner/dist/index.js' node dist/index.js 2>/dev/null
```

Expected: response for id 3 with `"content":[{"type":"text","text":"hello from skill\n"}],"isError":false`.

### 7.4 Verify stdout hygiene under error conditions

```bash
# Send a tool call that will fail (file does not exist)
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"fs_read_file","arguments":{"path":"/this/does/not/exist.py"}}}' \
  | node dist/index.js 2>/dev/null \
  | grep '"isError":true'   # must match
```

Expected: the error is in `result.isError: true` — not in a JSON-RPC `error` field. The `grep` finds the line. No non-JSON bytes appear.

### 7.5 Verify truncation

```bash
# Create a large file
python3 -c "print('x' * 100_000)" > /tmp/large_file.txt

printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"fs_read_file","arguments":{"path":"/tmp/large_file.txt"}}}' \
  | node dist/index.js 2>/dev/null \
  | python3 -c "
import sys, json
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get('id') == 5:
        result = msg['result']
        text = result['content'][0]['text']
        meta = json.loads(result['content'][1]['text'])
        assert len(text) <= 26000, f'too long: {len(text)}'
        assert meta['has_more'] == True
        assert meta['next_offset'] == 25000
        print('PASS: truncation correct')
"
```

---

## 8. Done Criteria

The service is done when all of the following are true:

- [ ] `npm run build` completes without TypeScript errors.
- [ ] `node dist/index.js` responds to an `initialize` message with a valid JSON-RPC result on stdout.
- [ ] `tools/list` returns at minimum `fs_read_file`, `fs_write_file`, `fs_list_dir`, `fs_delete_file`, `git_status`, `git_diff`, `git_commit`, `git_log`, and `exec_run_command`.
- [ ] Each tool's `inputSchema` in the `tools/list` response is valid JSON Schema, has a `description` field on every property, and the top-level schema has `"additionalProperties": false` (from `.strict()`).
- [ ] A `tools/call` for `fs_read_file` with a valid path returns the file content with `isError: false`.
- [ ] A `tools/call` for `fs_read_file` with a nonexistent path returns `isError: true` and an error message — not a JSON-RPC `error` object.
- [ ] A `tools/call` for `fs_read_file` on a file larger than 25,000 characters returns exactly 25,000 characters (plus the truncation notice), `has_more: true`, and a valid `next_offset` in the second content block.
- [ ] Calling `fs_read_file` again with `offset: 25000` returns the next page.
- [ ] `process.kill(pid, 'SIGTERM')` causes the process to call `server.close()` and `transport.close()` and exit with code 0, observable via `echo $?`.
- [ ] Piping stdout through `python3 -m json.tool` produces no parse errors even while the bridge emits `log.info` calls — confirming all log output goes to stderr.
- [ ] `console.log` does not appear anywhere in `src/` (verified by `grep -r 'console\.log' src/` returning no results).
- [ ] TypeScript strict mode is on: `tsc --noEmit` passes with zero errors.
- [ ] The Python orchestrator can call `MCPClientManager.call_tool('fs_read_file', {'path': '/workspace/main.py'})` and receive the file content without an exception.

---

## 9. Common Mistakes

### Mistake 1: Writing to stdout (stdout pollution)

**What happens:** A developer adds `console.log('tool called')` or a library (e.g. `dotenv`) prints a banner to stdout. The Python MCP SDK parser encounters non-JSON bytes interleaved with JSON-RPC messages and throws `json.JSONDecodeError`. The session crashes. There is no obvious stack trace in the bridge itself — the error surfaces in the Python orchestrator as a broken connection. This is the hardest bug to diagnose in the entire system.

**Fix:** All log output must go through the `log` instance in `src/services/logger.ts`, which writes to `pino.destination(2)` (fd 2 = stderr). Run `grep -r 'console\.log' src/` in CI and fail the build if any results are found. Audit every `npm` dependency for startup stdout writes (redirect stdout to a file on startup in the integration test and diff against empty).

### Mistake 2: Throwing from a tool handler

**What happens:** A tool handler throws an unhandled exception. The MCP SDK converts this into a JSON-RPC `error` object: `{"jsonrpc":"2.0","id":"...","error":{"code":-32603,"message":"Internal error"}}`. The Python SDK receives this and raises an exception in the caller coroutine. The orchestrator gets a hard exception, not an inspectable error message. It cannot adapt, retry, or report the cause to the user.

**Fix:** Wrap every tool handler body in `try/catch`. On any error, log to stderr and return `{ content: [{ type: 'text', text: errorMessage }], isError: true }`. The LLM receives the error text as a normal tool result and can reason about it. Use the `safeCall` wrapper (section 6.2) for handlers that delegate to skill processes.

### Mistake 3: Passing the Zod schema object instead of `.shape` to `inputSchema`

**What happens:** `server.registerTool('fs_read_file', { inputSchema: FsReadInput, ... }, handler)` — the SDK receives a `ZodObject` instance, not a plain shape object. The SDK may silently accept it or throw a confusing type error at registration time. `tools/list` may return malformed or empty JSON Schema for that tool.

**Fix:** Always pass `FsReadInput.shape` (a plain `{ [key: string]: ZodType }` record), not `FsReadInput` (the `ZodObject` instance). The SDK's `McpServer.registerTool` expects the `.shape` property.

### Mistake 4: Using `module: "CommonJS"` or `"moduleResolution": "Node10"` in tsconfig

**What happens:** With `"type": "module"` in `package.json`, Node.js treats all `.js` files as ESM. If `tsconfig.json` is set to `CommonJS` or uses the old `"Node10"` resolution, compiled output uses `require()` which fails at runtime with `require is not defined in ES module scope`. Alternatively, using `moduleResolution: "Node10"` with ESM causes incorrect resolution of the `.js` extension imports that the MCP SDK uses.

**Fix:** Set `"module": "Node16"` and `"moduleResolution": "Node16"` in `tsconfig.json`. Ensure all local imports in `src/` use the `.js` extension (e.g. `import { log } from './services/logger.js'`) even though the source files are `.ts`. The TypeScript compiler maps `.js` imports to their `.ts` source at compile time.

### Mistake 5: Returning `isError: true` result instead of throwing when the entire MCP session should fail

**What happens:** A developer wraps the `server.connect(transport)` call in `try/catch` and returns `isError: true` when the transport fails to initialize. This is the wrong place to use `isError: true`. Connection failures during startup are fatal — the process should log to stderr and call `process.exit(1)`. Using `isError: true` for protocol-level or startup failures masks the failure from the process supervisor.

**Fix:** `isError: true` is for individual tool handler failures only. Any error during startup (failing to read config, failing to connect the transport) should be logged to stderr and cause `process.exit(1)`. The outer `main().catch(...)` block (section 6.5) handles this correctly.
