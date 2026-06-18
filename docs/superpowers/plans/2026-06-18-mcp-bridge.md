# MCP Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Labmate MCP Bridge — a TypeScript stdio MCP server exposing fs/git/exec tools plus a Python `MCPClientManager` that owns the subprocess lifecycle, multiplexes concurrent tool calls through one persistent `ClientSession`, and survives crashes via reconnection + circuit breaker.

**Architecture:** The Python orchestrator (Brain) submits tool calls into an `asyncio.Queue`; a single owning `asyncio.Task` inside `MCPClientManager` drains the queue and dispatches each call over a persistent `ClientSession` connected to a long-lived Node.js MCP server over stdio JSON-RPC 2.0. The TypeScript server routes each request to a domain tool handler, which never throws and always bounds output to `CHARACTER_LIMIT`. stdout carries JSON-RPC only; all logs go to stderr.

**Tech Stack:** TypeScript (McpServer, Zod, pino, vitest), Python (mcp SDK, anyio, asyncio, pytest-asyncio)

---

## Critical Rules (enforced in every task)

- **stdout is sacred (TS):** NEVER `console.log()` / `process.stdout.write()`. pino writes to `pino.destination(2)` (fd 2 = stderr). Non-JSON bytes on stdout silently corrupt JSON-RPC.
- **anyio cancel scope (Py):** `stdio_client()` and `ClientSession()` are entered AND exited ONLY inside `MCPClientManager._run()`, which runs in one dedicated owning task. Never store an exit stack on `self` and close it elsewhere.
- **Tool handlers never throw (TS):** every handler body wrapped in `try/catch`; errors returned as `{ content: [{ type: 'text', text }], isError: true }`.
- **Output truncation (TS):** `CHARACTER_LIMIT = 25_000`; every tool reading file/git/command output runs results through `truncate()`.
- **No git commit steps** — there is no git repository in this working directory.

## File Structure

```
services/mcp-bridge/
├── package.json              # "type": "module"; SDK pinned to 1.12.1
├── tsconfig.json             # strict, ES2022, Node16
├── vitest.config.ts          # test root + globals
├── src/
│   ├── index.ts              # Bootstrap: McpServer + StdioServerTransport + graceful shutdown
│   ├── registry.ts           # registerAllTools(server)
│   ├── constants.ts          # CHARACTER_LIMIT = 25_000
│   ├── types.ts              # z.infer-derived types
│   ├── tools/
│   │   ├── fs.ts             # registerFsTools
│   │   ├── git.ts            # registerGitTools
│   │   └── exec.ts           # registerExecTools
│   ├── schemas/
│   │   ├── fs.ts             # Zod schemas for fs tools
│   │   ├── git.ts            # Zod schemas for git tools
│   │   └── exec.ts           # Zod schemas for exec tools
│   ├── services/
│   │   ├── logger.ts         # pino → stderr fd 2 ONLY
│   │   └── ipc.ts            # Child-process MCP client to skill servers
│   └── utils/
│       └── truncate.ts       # truncate() with has_more/next_offset
├── mcp_client_manager.py     # Python MCPClientManager
└── requirements.txt          # mcp>=1.27,<2; anyio>=4.9; pydantic>=2

tests/services/mcp-bridge/
├── test_mcp_client_manager.py
└── src/
    ├── truncate.test.ts
    ├── fs.test.ts
    ├── git.test.ts
    └── server.test.ts
```

## Test commands

- TypeScript: `npx vitest run tests/services/mcp-bridge/src/<file>.test.ts` (run from `/Users/zachstallbohm/Work/gemma/services/mcp-bridge`)
- Python: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py -m mocked -v`

---

## Task 1: Scaffold the TypeScript package

**Files:**
- Create: `services/mcp-bridge/package.json`
- Create: `services/mcp-bridge/tsconfig.json`
- Create: `services/mcp-bridge/vitest.config.ts`

- [ ] **Step 1: Write `package.json`**

```json
{
  "name": "labmate-mcp-bridge",
  "version": "0.1.0",
  "type": "module",
  "dependencies": {
    "@modelcontextprotocol/sdk": "1.12.1",
    "zod": "^3.22",
    "pino": "^9"
  },
  "devDependencies": {
    "typescript": "^5.4",
    "tsx": "^4.19",
    "@types/node": "^22",
    "vitest": "^2"
  },
  "scripts": {
    "build": "tsc",
    "start": "node dist/index.js",
    "dev": "tsx src/index.ts",
    "test": "vitest run"
  }
}
```

- [ ] **Step 2: Write `tsconfig.json`**

```json
{
  "compilerOptions": {
    "strict": true,
    "target": "ES2022",
    "module": "Node16",
    "moduleResolution": "Node16",
    "outDir": "dist",
    "rootDir": "src",
    "declaration": true,
    "esModuleInterop": true,
    "skipLibCheck": true
  },
  "include": ["src"]
}
```

- [ ] **Step 3: Write `vitest.config.ts`**

```typescript
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    include: ['../../tests/services/mcp-bridge/src/**/*.test.ts'],
  },
});
```

- [ ] **Step 4: Install dependencies**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npm install`
Expected: `node_modules/` created, no errors. `@modelcontextprotocol/sdk@1.12.1` installed.

---

## Task 2: `constants.ts` — CHARACTER_LIMIT

**Files:**
- Create: `services/mcp-bridge/src/constants.ts`

- [ ] **Step 1: Write the constant**

```typescript
// src/constants.ts
export const CHARACTER_LIMIT = 25_000;
```

There is no separate test for a single constant; it is exercised by `truncate.test.ts` in Task 3.

---

## Task 3: `truncate()` utility (TDD)

**Files:**
- Create: `services/mcp-bridge/src/utils/truncate.ts`
- Test: `tests/services/mcp-bridge/src/truncate.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// tests/services/mcp-bridge/src/truncate.test.ts
import { describe, it, expect } from 'vitest';
import { truncate } from '../../../../services/mcp-bridge/src/utils/truncate.js';
import { CHARACTER_LIMIT } from '../../../../services/mcp-bridge/src/constants.js';

describe('truncate', () => {
  it('returns full text and has_more=false when under the limit', () => {
    const r = truncate('hello', 0, CHARACTER_LIMIT);
    expect(r.text).toBe('hello');
    expect(r.has_more).toBe(false);
    expect(r.next_offset).toBeNull();
    expect(r.total).toBe(5);
  });

  it('truncates at exactly CHARACTER_LIMIT and appends a notice', () => {
    const big = 'a'.repeat(CHARACTER_LIMIT + 100);
    const r = truncate(big, 0, CHARACTER_LIMIT);
    expect(r.has_more).toBe(true);
    expect(r.next_offset).toBe(CHARACTER_LIMIT);
    expect(r.total).toBe(CHARACTER_LIMIT + 100);
    expect(r.text.startsWith('a'.repeat(CHARACTER_LIMIT))).toBe(true);
    expect(r.text).toContain('[TRUNCATED:');
    expect(r.text).toContain(`offset=${CHARACTER_LIMIT}`);
  });

  it('paginates from a non-zero offset', () => {
    const big = 'a'.repeat(10) + 'b'.repeat(10);
    const r = truncate(big, 10, 10);
    expect(r.text).toBe('b'.repeat(10));
    expect(r.has_more).toBe(false);
    expect(r.next_offset).toBeNull();
    expect(r.total).toBe(20);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx vitest run tests/services/mcp-bridge/src/truncate.test.ts`
Expected: FAIL — cannot resolve `../utils/truncate.js`.

- [ ] **Step 3: Write the implementation**

```typescript
// src/utils/truncate.ts
import { CHARACTER_LIMIT } from '../constants.js';

export interface TruncateResult {
  text: string;
  has_more: boolean;
  next_offset: number | null;
  total: number;
}

export function truncate(
  text: string,
  offset: number = 0,
  limit: number = CHARACTER_LIMIT,
): TruncateResult {
  const slice = text.slice(offset, offset + limit);
  const has_more = offset + limit < text.length;
  return {
    text:
      slice +
      (has_more
        ? `\n\n[TRUNCATED: showing chars ${offset}–${offset + slice.length} of ${text.length} total. ` +
          `Call again with offset=${offset + limit} to continue.]`
        : ''),
    has_more,
    next_offset: has_more ? offset + limit : null,
    total: text.length,
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx vitest run tests/services/mcp-bridge/src/truncate.test.ts`
Expected: PASS (3 tests).

---

## Task 4: `logger.ts` — pino to stderr (fd 2)

**Files:**
- Create: `services/mcp-bridge/src/services/logger.ts`

- [ ] **Step 1: Write the logger**

```typescript
// src/services/logger.ts
// CRITICAL: destination fd 2 = stderr. stdout is reserved for JSON-RPC ONLY.
import pino from 'pino';

export const log = pino(
  { level: process.env.LOG_LEVEL ?? 'info' },
  pino.destination(2), // fd 2 = stderr
);
```

This module is verified indirectly: Task 11's `server.test.ts` asserts that nothing is ever written to stdout. There is no dedicated unit test (a stderr-only logger has no observable stdout behavior to assert in isolation).

---

## Task 5: fs schemas (Zod)

**Files:**
- Create: `services/mcp-bridge/src/schemas/fs.ts`

- [ ] **Step 1: Write the fs schemas**

```typescript
// src/schemas/fs.ts
import { z } from 'zod';

export const FsReadInput = z
  .object({
    path: z.string().describe('Absolute path of the file to read.'),
    offset: z
      .number()
      .int()
      .min(0)
      .default(0)
      .describe('Character offset to start reading (for pagination).'),
    limit: z
      .number()
      .int()
      .min(1)
      .default(25_000)
      .describe('Max characters to return per call.'),
  })
  .strict();
export type FsReadInput = z.infer<typeof FsReadInput>;

export const FsWriteInput = z
  .object({
    path: z.string().describe('Absolute path of the file to write.'),
    content: z.string().describe('UTF-8 content to write.'),
    create_dirs: z
      .boolean()
      .default(true)
      .describe('Create parent directories if they do not exist.'),
  })
  .strict();
export type FsWriteInput = z.infer<typeof FsWriteInput>;

export const FsListDirInput = z
  .object({
    path: z.string().describe('Absolute path of the directory to list.'),
    recursive: z
      .boolean()
      .default(false)
      .describe('Recurse into subdirectories.'),
    max_entries: z
      .number()
      .int()
      .min(1)
      .default(500)
      .describe('Maximum number of entries to return.'),
  })
  .strict();
export type FsListDirInput = z.infer<typeof FsListDirInput>;

export const FsDeleteInput = z
  .object({
    path: z.string().describe('Absolute path of the file to delete.'),
  })
  .strict();
export type FsDeleteInput = z.infer<typeof FsDeleteInput>;
```

Schemas are validated through the fs tool tests in Task 8.

---

## Task 6: git schemas (Zod)

**Files:**
- Create: `services/mcp-bridge/src/schemas/git.ts`

- [ ] **Step 1: Write the git schemas**

```typescript
// src/schemas/git.ts
import { z } from 'zod';

export const GitStatusInput = z
  .object({
    repo_path: z.string().describe('Absolute path of the git repository.'),
  })
  .strict();
export type GitStatusInput = z.infer<typeof GitStatusInput>;

export const GitDiffInput = z
  .object({
    repo_path: z.string().describe('Absolute path of the git repository.'),
    staged: z
      .boolean()
      .default(false)
      .describe('Show staged changes (git diff --staged) instead of unstaged.'),
  })
  .strict();
export type GitDiffInput = z.infer<typeof GitDiffInput>;

export const GitLogInput = z
  .object({
    repo_path: z.string().describe('Absolute path of the git repository.'),
    n: z
      .number()
      .int()
      .min(1)
      .default(20)
      .describe('Number of commits to show.'),
  })
  .strict();
export type GitLogInput = z.infer<typeof GitLogInput>;

export const GitCommitInput = z
  .object({
    repo_path: z.string().describe('Absolute path of the git repository.'),
    message: z.string().describe('Commit message.'),
  })
  .strict();
export type GitCommitInput = z.infer<typeof GitCommitInput>;

export const GitApplyPatchInput = z
  .object({
    repo_path: z.string().describe('Absolute path of the git repository.'),
    patch: z.string().describe('Unified diff patch to apply.'),
  })
  .strict();
export type GitApplyPatchInput = z.infer<typeof GitApplyPatchInput>;
```

Schemas are validated through the git tool tests in Task 9.

---

## Task 7: exec schemas (Zod)

**Files:**
- Create: `services/mcp-bridge/src/schemas/exec.ts`

- [ ] **Step 1: Write the exec schemas**

```typescript
// src/schemas/exec.ts
import { z } from 'zod';

export const ExecRunCommandInput = z
  .object({
    command: z.string().describe('Shell command to run.'),
    cwd: z.string().describe('Absolute working directory for the command.'),
    timeout_ms: z
      .number()
      .int()
      .min(1)
      .default(30_000)
      .describe('Timeout in milliseconds before the command is killed.'),
  })
  .strict();
export type ExecRunCommandInput = z.infer<typeof ExecRunCommandInput>;

export const ExecSkillInput = z
  .object({
    skill_name: z.string().describe('Name of the skill subprocess to dispatch to.'),
    tool_name: z.string().describe('Tool to invoke inside the skill server.'),
    arguments: z
      .record(z.unknown())
      .default({})
      .describe('Arguments object passed to the skill tool.'),
  })
  .strict();
export type ExecSkillInput = z.infer<typeof ExecSkillInput>;
```

Schemas are validated through the exec/server tests.

---

## Task 8: fs tools (TDD)

**Files:**
- Create: `services/mcp-bridge/src/tools/fs.ts`
- Test: `tests/services/mcp-bridge/src/fs.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// tests/services/mcp-bridge/src/fs.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('node:fs/promises', () => ({
  readFile: vi.fn(),
  writeFile: vi.fn(),
  mkdir: vi.fn(),
  readdir: vi.fn(),
  unlink: vi.fn(),
  stat: vi.fn(),
}));

import * as fsp from 'node:fs/promises';
import { registerFsTools } from '../../../../services/mcp-bridge/src/tools/fs.js';

interface Registered {
  handler: (args: any) => Promise<any>;
}

function fakeServer() {
  const tools: Record<string, Registered> = {};
  const server = {
    registerTool(name: string, _meta: unknown, handler: (a: any) => Promise<any>) {
      tools[name] = { handler };
    },
  };
  return { server: server as any, tools };
}

describe('fs tools', () => {
  beforeEach(() => vi.clearAllMocks());

  it('fs_read_file returns file contents', async () => {
    (fsp.readFile as any).mockResolvedValue('file body');
    const { server, tools } = fakeServer();
    registerFsTools(server);
    const res = await tools['fs_read_file'].handler({ path: '/x.txt', offset: 0, limit: 25000 });
    expect(res.isError).toBeUndefined();
    expect(res.content[0].text).toBe('file body');
    expect(res.structuredContent.has_more).toBe(false);
  });

  it('fs_read_file returns isError when the file is missing', async () => {
    (fsp.readFile as any).mockRejectedValue(new Error('ENOENT'));
    const { server, tools } = fakeServer();
    registerFsTools(server);
    const res = await tools['fs_read_file'].handler({ path: '/nope', offset: 0, limit: 25000 });
    expect(res.isError).toBe(true);
    expect(res.content[0].text).toContain('ENOENT');
  });

  it('fs_write_file creates parent dirs then writes', async () => {
    (fsp.mkdir as any).mockResolvedValue(undefined);
    (fsp.writeFile as any).mockResolvedValue(undefined);
    const { server, tools } = fakeServer();
    registerFsTools(server);
    const res = await tools['fs_write_file'].handler({
      path: '/a/b/c.txt',
      content: 'hi',
      create_dirs: true,
    });
    expect(res.isError).toBeUndefined();
    expect(fsp.mkdir).toHaveBeenCalledWith('/a/b', { recursive: true });
    expect(fsp.writeFile).toHaveBeenCalledWith('/a/b/c.txt', 'hi', 'utf8');
  });

  it('fs_delete_file unlinks the path', async () => {
    (fsp.unlink as any).mockResolvedValue(undefined);
    const { server, tools } = fakeServer();
    registerFsTools(server);
    const res = await tools['fs_delete_file'].handler({ path: '/a.txt' });
    expect(res.isError).toBeUndefined();
    expect(fsp.unlink).toHaveBeenCalledWith('/a.txt');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx vitest run tests/services/mcp-bridge/src/fs.test.ts`
Expected: FAIL — cannot resolve `../tools/fs.js`.

- [ ] **Step 3: Write the implementation**

```typescript
// src/tools/fs.ts
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { readFile, writeFile, mkdir, readdir, unlink, stat } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import {
  FsReadInput,
  FsWriteInput,
  FsListDirInput,
  FsDeleteInput,
} from '../schemas/fs.js';
import { truncate } from '../utils/truncate.js';
import { log } from '../services/logger.js';

function errResult(label: string, err: unknown) {
  const msg = err instanceof Error ? err.message : String(err);
  log.error({ err }, `${label} failed`); // stderr
  return {
    content: [{ type: 'text' as const, text: `${label}: ${msg}` }],
    isError: true as const,
  };
}

export function registerFsTools(server: McpServer) {
  server.registerTool(
    'fs_read_file',
    {
      title: 'Read file',
      description: 'Read a UTF-8 text file with character-offset pagination.',
      inputSchema: FsReadInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async (args) => {
      try {
        const content = await readFile(args.path, 'utf8');
        const { text, has_more, next_offset, total } = truncate(
          content,
          args.offset,
          args.limit,
        );
        return {
          content: [{ type: 'text', text }],
          structuredContent: { has_more, next_offset, total },
        };
      } catch (err) {
        return errResult(`Error reading ${args.path}`, err);
      }
    },
  );

  server.registerTool(
    'fs_write_file',
    {
      title: 'Write file',
      description: 'Write a UTF-8 file, optionally creating parent directories.',
      inputSchema: FsWriteInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: false },
    },
    async (args) => {
      try {
        if (args.create_dirs) {
          await mkdir(dirname(args.path), { recursive: true });
        }
        await writeFile(args.path, args.content, 'utf8');
        return {
          content: [{ type: 'text', text: `Wrote ${args.content.length} chars to ${args.path}` }],
        };
      } catch (err) {
        return errResult(`Error writing ${args.path}`, err);
      }
    },
  );

  server.registerTool(
    'fs_list_dir',
    {
      title: 'List directory',
      description: 'List directory entries, optionally recursively, bounded by max_entries.',
      inputSchema: FsListDirInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async (args) => {
      try {
        const out: string[] = [];
        const walk = async (dir: string): Promise<void> => {
          const entries = await readdir(dir, { withFileTypes: true });
          for (const e of entries) {
            if (out.length >= args.max_entries) return;
            const full = join(dir, e.name);
            out.push(e.isDirectory() ? `${full}/` : full);
            if (args.recursive && e.isDirectory()) await walk(full);
          }
        };
        await walk(args.path);
        const { text, has_more, next_offset, total } = truncate(out.join('\n'));
        return {
          content: [{ type: 'text', text }],
          structuredContent: { count: out.length, has_more, next_offset, total },
        };
      } catch (err) {
        return errResult(`Error listing ${args.path}`, err);
      }
    },
  );

  server.registerTool(
    'fs_delete_file',
    {
      title: 'Delete file',
      description: 'Delete a single file (not a directory).',
      inputSchema: FsDeleteInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: false },
    },
    async (args) => {
      try {
        const info = await stat(args.path);
        if (info.isDirectory()) {
          return errResult(
            `Error deleting ${args.path}`,
            new Error('path is a directory; fs_delete_file only removes files'),
          );
        }
        await unlink(args.path);
        return { content: [{ type: 'text', text: `Deleted ${args.path}` }] };
      } catch (err) {
        return errResult(`Error deleting ${args.path}`, err);
      }
    },
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx vitest run tests/services/mcp-bridge/src/fs.test.ts`
Expected: PASS (4 tests).

---

## Task 9: git tools (TDD)

**Files:**
- Create: `services/mcp-bridge/src/tools/git.ts`
- Test: `tests/services/mcp-bridge/src/git.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// tests/services/mcp-bridge/src/git.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest';

const execFileMock = vi.fn();
vi.mock('node:child_process', () => ({ execFile: execFileMock }));
vi.mock('node:util', async (orig) => {
  const actual = await (orig() as Promise<any>);
  return {
    ...actual,
    // promisify(execFile) -> our async stub driven by execFileMock's queued result
    promisify: () => (file: string, args: string[], opts: any) =>
      execFileMock(file, args, opts),
  };
});

import { registerGitTools } from '../../../../services/mcp-bridge/src/tools/git.js';

function fakeServer() {
  const tools: Record<string, { handler: (a: any) => Promise<any> }> = {};
  const server = {
    registerTool(name: string, _meta: unknown, handler: (a: any) => Promise<any>) {
      tools[name] = { handler };
    },
  };
  return { server: server as any, tools };
}

describe('git tools', () => {
  beforeEach(() => vi.clearAllMocks());

  it('git_status returns porcelain output', async () => {
    execFileMock.mockResolvedValue({ stdout: ' M src/a.ts\n', stderr: '' });
    const { server, tools } = fakeServer();
    registerGitTools(server);
    const res = await tools['git_status'].handler({ repo_path: '/repo' });
    expect(res.isError).toBeUndefined();
    expect(res.content[0].text).toContain(' M src/a.ts');
    expect(execFileMock).toHaveBeenCalledWith(
      'git',
      ['-C', '/repo', 'status', '--porcelain'],
      expect.anything(),
    );
  });

  it('git_commit runs commit -am with the message', async () => {
    execFileMock.mockResolvedValue({ stdout: '1 file changed', stderr: '' });
    const { server, tools } = fakeServer();
    registerGitTools(server);
    const res = await tools['git_commit'].handler({ repo_path: '/repo', message: 'msg' });
    expect(res.isError).toBeUndefined();
    expect(execFileMock).toHaveBeenCalledWith(
      'git',
      ['-C', '/repo', 'commit', '-am', 'msg'],
      expect.anything(),
    );
  });

  it('git_status returns isError when git fails', async () => {
    execFileMock.mockRejectedValue(new Error('not a git repository'));
    const { server, tools } = fakeServer();
    registerGitTools(server);
    const res = await tools['git_status'].handler({ repo_path: '/nope' });
    expect(res.isError).toBe(true);
    expect(res.content[0].text).toContain('not a git repository');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx vitest run tests/services/mcp-bridge/src/git.test.ts`
Expected: FAIL — cannot resolve `../tools/git.js`.

- [ ] **Step 3: Write the implementation**

```typescript
// src/tools/git.ts
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import {
  GitStatusInput,
  GitDiffInput,
  GitLogInput,
  GitCommitInput,
  GitApplyPatchInput,
} from '../schemas/git.js';
import { truncate } from '../utils/truncate.js';
import { log } from '../services/logger.js';

const run = promisify(execFile);

function errResult(label: string, err: unknown) {
  const e = err as { stderr?: string; message?: string };
  const msg = e.stderr?.trim() || e.message || String(err);
  log.error({ err }, `${label} failed`); // stderr
  return {
    content: [{ type: 'text' as const, text: `${label}: ${msg}` }],
    isError: true as const,
  };
}

async function git(repo: string, args: string[]) {
  const { stdout } = await run('git', ['-C', repo, ...args], {
    maxBuffer: 10 * 1024 * 1024,
  });
  return stdout;
}

export function registerGitTools(server: McpServer) {
  server.registerTool(
    'git_status',
    {
      title: 'Git status',
      description: 'Show working-tree status in porcelain format.',
      inputSchema: GitStatusInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async (args) => {
      try {
        const out = await git(args.repo_path, ['status', '--porcelain']);
        const { text } = truncate(out);
        return { content: [{ type: 'text', text: text || '(clean)' }] };
      } catch (err) {
        return errResult(`git_status ${args.repo_path}`, err);
      }
    },
  );

  server.registerTool(
    'git_diff',
    {
      title: 'Git diff',
      description: 'Show unstaged or staged diff.',
      inputSchema: GitDiffInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async (args) => {
      try {
        const a = args.staged ? ['diff', '--staged'] : ['diff'];
        const out = await git(args.repo_path, a);
        const { text } = truncate(out);
        return { content: [{ type: 'text', text: text || '(no changes)' }] };
      } catch (err) {
        return errResult(`git_diff ${args.repo_path}`, err);
      }
    },
  );

  server.registerTool(
    'git_log',
    {
      title: 'Git log',
      description: 'Show the most recent commits in one-line format.',
      inputSchema: GitLogInput.shape,
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    async (args) => {
      try {
        const out = await git(args.repo_path, ['log', '--oneline', '-n', String(args.n)]);
        const { text } = truncate(out);
        return { content: [{ type: 'text', text: text || '(no commits)' }] };
      } catch (err) {
        return errResult(`git_log ${args.repo_path}`, err);
      }
    },
  );

  server.registerTool(
    'git_commit',
    {
      title: 'Git commit',
      description: 'Commit all tracked changes with a message (git commit -am).',
      inputSchema: GitCommitInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: false },
    },
    async (args) => {
      try {
        const out = await git(args.repo_path, ['commit', '-am', args.message]);
        const { text } = truncate(out);
        return { content: [{ type: 'text', text }] };
      } catch (err) {
        return errResult(`git_commit ${args.repo_path}`, err);
      }
    },
  );

  server.registerTool(
    'git_apply_patch',
    {
      title: 'Git apply patch',
      description: 'Apply a unified diff patch to the repository working tree.',
      inputSchema: GitApplyPatchInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: false },
    },
    async (args) => {
      try {
        const child = execFile(
          'git',
          ['-C', args.repo_path, 'apply', '-'],
          { maxBuffer: 10 * 1024 * 1024 },
        );
        const done = new Promise<string>((resolve, reject) => {
          let stdout = '';
          let stderr = '';
          child.stdout?.on('data', (d) => (stdout += d));
          child.stderr?.on('data', (d) => (stderr += d));
          child.on('error', reject);
          child.on('close', (code) =>
            code === 0 ? resolve(stdout) : reject(new Error(stderr || `exit ${code}`)),
          );
        });
        child.stdin?.write(args.patch);
        child.stdin?.end();
        await done;
        return { content: [{ type: 'text', text: 'Patch applied.' }] };
      } catch (err) {
        return errResult(`git_apply_patch ${args.repo_path}`, err);
      }
    },
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx vitest run tests/services/mcp-bridge/src/git.test.ts`
Expected: PASS (3 tests).

---

## Task 10: exec tools + ipc service

**Files:**
- Create: `services/mcp-bridge/src/services/ipc.ts`
- Create: `services/mcp-bridge/src/tools/exec.ts`

- [ ] **Step 1: Write the ipc service**

```typescript
// src/services/ipc.ts
// Child-process MCP client to skill servers. Spawns a per-call skill subprocess,
// performs the MCP initialize handshake, calls one tool, then tears down.
// Used by exec_skill. All logging goes to stderr via the logger.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { log } from './logger.js';

export interface SkillCallResult {
  text: string;
  isError: boolean;
}

const SKILLS_ROOT = process.env.SKILLS_ROOT ?? '/app/services/skills';
const SKILL_TIMEOUT_MS = Number(process.env.SKILL_TIMEOUT_MS ?? '60000');

export async function callSkill(
  skillName: string,
  toolName: string,
  args: Record<string, unknown>,
): Promise<SkillCallResult> {
  const transport = new StdioClientTransport({
    command: 'node',
    args: [`${SKILLS_ROOT}/${skillName}/dist/index.js`],
  });
  const client = new Client({ name: 'labmate-bridge', version: '0.1.0' });

  const timer = setTimeout(() => {
    log.error({ skillName, toolName }, 'skill call timed out');
    void transport.close();
  }, SKILL_TIMEOUT_MS);

  try {
    await client.connect(transport);
    const res = (await client.callTool({ name: toolName, arguments: args })) as {
      content?: Array<{ type: string; text?: string }>;
      isError?: boolean;
    };
    const text = (res.content ?? [])
      .filter((c) => c.type === 'text')
      .map((c) => c.text ?? '')
      .join('\n');
    return { text, isError: Boolean(res.isError) };
  } finally {
    clearTimeout(timer);
    await client.close().catch(() => undefined);
    await transport.close().catch(() => undefined);
  }
}
```

- [ ] **Step 2: Write the exec tools**

```typescript
// src/tools/exec.ts
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { execFile } from 'node:child_process';
import { ExecRunCommandInput, ExecSkillInput } from '../schemas/exec.js';
import { truncate } from '../utils/truncate.js';
import { callSkill } from '../services/ipc.js';
import { log } from '../services/logger.js';

function errResult(label: string, err: unknown) {
  const msg = err instanceof Error ? err.message : String(err);
  log.error({ err }, `${label} failed`); // stderr
  return {
    content: [{ type: 'text' as const, text: `${label}: ${msg}` }],
    isError: true as const,
  };
}

export function registerExecTools(server: McpServer) {
  server.registerTool(
    'exec_run_command',
    {
      title: 'Run command',
      description: 'Run a shell command in a working directory with a timeout; output is truncated.',
      inputSchema: ExecRunCommandInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: true },
    },
    async (args) => {
      try {
        const out = await new Promise<string>((resolve, reject) => {
          execFile(
            'bash',
            ['-lc', args.command],
            { cwd: args.cwd, timeout: args.timeout_ms, maxBuffer: 10 * 1024 * 1024 },
            (err, stdout, stderr) => {
              const combined = `${stdout}${stderr}`;
              if (err) {
                reject(new Error(`${err.message}\n${combined}`));
              } else {
                resolve(combined);
              }
            },
          );
        });
        const { text, has_more, next_offset, total } = truncate(out);
        return {
          content: [{ type: 'text', text }],
          structuredContent: { has_more, next_offset, total },
        };
      } catch (err) {
        return errResult(`exec_run_command`, err);
      }
    },
  );

  server.registerTool(
    'exec_skill',
    {
      title: 'Run skill tool',
      description: 'Dispatch a tool call to a skill subprocess over MCP and return its result.',
      inputSchema: ExecSkillInput.shape,
      annotations: { readOnlyHint: false, openWorldHint: true },
    },
    async (args) => {
      try {
        const { text, isError } = await callSkill(
          args.skill_name,
          args.tool_name,
          args.arguments,
        );
        const { text: bounded } = truncate(text);
        return { content: [{ type: 'text', text: bounded }], isError };
      } catch (err) {
        return errResult(`exec_skill ${args.skill_name}/${args.tool_name}`, err);
      }
    },
  );
}
```

The exec tools are exercised end-to-end by the server integration test in Task 11.

---

## Task 11: registry, bootstrap, and server integration test (TDD)

**Files:**
- Create: `services/mcp-bridge/src/registry.ts`
- Create: `services/mcp-bridge/src/types.ts`
- Create: `services/mcp-bridge/src/index.ts`
- Test: `tests/services/mcp-bridge/src/server.test.ts`

- [ ] **Step 1: Write the registry**

```typescript
// src/registry.ts
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { registerFsTools } from './tools/fs.js';
import { registerGitTools } from './tools/git.js';
import { registerExecTools } from './tools/exec.js';

export function registerAllTools(server: McpServer) {
  registerFsTools(server);
  registerGitTools(server);
  registerExecTools(server);
}
```

- [ ] **Step 2: Write the derived types**

```typescript
// src/types.ts
export type {
  FsReadInput,
  FsWriteInput,
  FsListDirInput,
  FsDeleteInput,
} from './schemas/fs.js';
export type {
  GitStatusInput,
  GitDiffInput,
  GitLogInput,
  GitCommitInput,
  GitApplyPatchInput,
} from './schemas/git.js';
export type { ExecRunCommandInput, ExecSkillInput } from './schemas/exec.js';
export type { TruncateResult } from './utils/truncate.js';
```

- [ ] **Step 3: Write the failing integration test**

```typescript
// tests/services/mcp-bridge/src/server.test.ts
// Uses the SDK in-memory linked transport pair: no subprocess, no stdout pollution risk.
import { describe, it, expect } from 'vitest';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { registerAllTools } from '../../../../services/mcp-bridge/src/registry.js';

async function connectedPair() {
  const server = new McpServer({ name: 'labmate', version: '0.1.0' });
  registerAllTools(server);
  const client = new Client({ name: 'test', version: '0.1.0' });
  const [clientT, serverT] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(serverT), client.connect(clientT)]);
  return { server, client };
}

describe('server integration', () => {
  it('lists every registered tool with a description and inputSchema', async () => {
    const { client } = await connectedPair();
    const { tools } = await client.listTools();
    const names = tools.map((t) => t.name).sort();
    expect(names).toEqual(
      [
        'exec_run_command',
        'exec_skill',
        'fs_delete_file',
        'fs_list_dir',
        'fs_read_file',
        'fs_write_file',
        'git_apply_patch',
        'git_commit',
        'git_diff',
        'git_log',
        'git_status',
      ].sort(),
    );
    for (const t of tools) {
      expect(t.description).toBeTruthy();
      expect(t.inputSchema).toBeTruthy();
      expect(JSON.stringify(t.inputSchema)).not.toContain('$ref');
    }
  });

  it('a failing tool call returns isError, not a protocol error', async () => {
    const { client } = await connectedPair();
    const res = (await client.callTool({
      name: 'git_status',
      arguments: { repo_path: '/definitely/not/a/repo/xyz' },
    })) as { isError?: boolean; content: Array<{ text: string }> };
    expect(res.isError).toBe(true);
    expect(res.content[0].text.length).toBeGreaterThan(0);
  });

  it('does not write to process.stdout during a tool call', async () => {
    const writes: string[] = [];
    const orig = process.stdout.write.bind(process.stdout);
    (process.stdout.write as any) = (chunk: any, ...rest: any[]) => {
      writes.push(String(chunk));
      return orig(chunk, ...rest);
    };
    try {
      const { client } = await connectedPair();
      await client.callTool({
        name: 'fs_read_file',
        arguments: { path: '/no/such/file', offset: 0, limit: 25000 },
      });
    } finally {
      (process.stdout.write as any) = orig;
    }
    expect(writes.join('')).toBe('');
  });
});
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx vitest run tests/services/mcp-bridge/src/server.test.ts`
Expected: FAIL — cannot resolve `../registry.js` (or earlier tool modules) until all prior tasks complete.

- [ ] **Step 5: Write the bootstrap entry point**

```typescript
// src/index.ts
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { registerAllTools } from './registry.js';
import { log } from './services/logger.js';

async function main() {
  const server = new McpServer({ name: 'labmate', version: '0.1.0' });
  registerAllTools(server);
  const transport = new StdioServerTransport();

  let shuttingDown = false;
  const shutdown = async (sig: string) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.info({ sig }, 'shutting down'); // stderr
    try {
      await server.close();
      await transport.close();
    } finally {
      process.exit(0);
    }
  };

  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
  process.on('uncaughtException', (e) => {
    log.fatal(e, 'uncaught');
    void shutdown('uncaughtException');
  });

  // From here on stdout carries JSON-RPC ONLY.
  await server.connect(transport);
  log.info('labmate MCP server ready on stdio'); // stderr
}

main().catch((e) => {
  log.fatal(e, 'fatal startup');
  process.exit(1);
});
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx vitest run tests/services/mcp-bridge/src/server.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 7: Type-check the whole TS package**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 8: Run all TS tests**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx vitest run`
Expected: PASS (truncate 3 + fs 4 + git 3 + server 3 = 13 tests).

---

## Task 12: Python test config + requirements

**Files:**
- Create: `services/mcp-bridge/requirements.txt`
- Modify/Create: `/Users/zachstallbohm/Work/gemma/pyproject.toml` (add pytest config)

- [ ] **Step 1: Write requirements.txt**

```text
mcp>=1.27,<2
anyio>=4.9
pydantic>=2
pytest>=8
pytest-asyncio>=0.23
```

- [ ] **Step 2: Ensure pytest config exists**

If `/Users/zachstallbohm/Work/gemma/pyproject.toml` already has a `[tool.pytest.ini_options]` section, verify it contains the keys below and add any missing ones. Otherwise create the file with exactly this content:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
    "mocked: tests with no GPU, network, or subprocess; always run in CI",
    "live: tests that need a running inference server or subprocess",
]
```

- [ ] **Step 3: Install Python deps**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pip install -r services/mcp-bridge/requirements.txt`
Expected: `mcp`, `anyio`, `pydantic`, `pytest`, `pytest-asyncio` installed.

---

## Task 13: `MCPClientManager` — submit, multiplex, result (TDD)

**Files:**
- Create: `services/mcp-bridge/mcp_client_manager.py`
- Test: `tests/services/mcp-bridge/test_mcp_client_manager.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/mcp-bridge/test_mcp_client_manager.py
import asyncio
import importlib.util
import time
from pathlib import Path

import pytest

# Load the module by path (hyphenated package dir is not importable normally).
_MOD_PATH = (
    Path(__file__).resolve().parents[2]
    / "services" / "mcp-bridge" / "mcp_client_manager.py"
)
_spec = importlib.util.spec_from_file_location("mcp_client_manager", _MOD_PATH)
mcm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcm)

MCPClientManager = mcm.MCPClientManager
CircuitOpenError = mcm.CircuitOpenError


class _FakeParams:
    """Stand-in for StdioServerParameters; never actually connected in mocked tests."""


@pytest.mark.mocked
async def test_submit_enqueues_and_returns_result():
    mgr = MCPClientManager(_FakeParams())

    async def fake_serve(session):
        # Drain inbox, immediately resolve each future with a canned result.
        while True:
            req = await mgr._inbox.get()
            req.future.set_result({"ok": True, "name": req.name})

    mgr._serve = fake_serve  # type: ignore[assignment]
    # Mark ready and start a bare serve loop (no real session needed).
    mgr._ready.set()
    serve_task = asyncio.create_task(fake_serve(None))
    try:
        result = await mgr.call_tool("fs_read_file", {"path": "/x"})
        assert result == {"ok": True, "name": "fs_read_file"}
    finally:
        serve_task.cancel()
        await asyncio.gather(serve_task, return_exceptions=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py -m mocked -v`
Expected: FAIL — `mcp_client_manager.py` does not exist (spec load error).

- [ ] **Step 3: Write the implementation**

```python
# services/mcp-bridge/mcp_client_manager.py
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client


class CircuitOpenError(Exception):
    """Raised when the breaker has tripped after repeated server crashes."""


@dataclass
class _Req:
    name: str
    args: dict[str, Any]
    future: asyncio.Future
    timeout: float = 30.0


class MCPClientManager:
    """
    Single owning task for the MCP session lifecycle.

    CRITICAL INVARIANT: stdio_client() and ClientSession() context managers
    are entered AND exited inside _run(), which runs in one dedicated
    asyncio.Task. They are never entered or exited from a caller's task.
    This satisfies anyio's cancel-scope rule.
    """

    def __init__(
        self,
        params: StdioServerParameters,
        *,
        max_failures: int = 5,
        window: float = 60.0,
        call_timeout: float = 30.0,
    ) -> None:
        self._params = params
        self._inbox: asyncio.Queue[_Req] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._failures: deque[float] = deque()
        self._max_failures = max_failures
        self._window = window
        self._call_timeout = call_timeout
        self.tools: list = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the owning lifecycle task. Call once before any tool calls."""
        self._task = asyncio.create_task(self._run(), name="mcp-lifecycle")

    async def wait_ready(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        fut = asyncio.get_running_loop().create_future()
        await self._inbox.put(_Req(name, args, fut, timeout or self._call_timeout))
        return await fut

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _breaker_open(self) -> bool:
        now = time.monotonic()
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        return len(self._failures) >= self._max_failures

    def _drain_with(self, exc: Exception) -> None:
        while not self._inbox.empty():
            try:
                req = self._inbox.get_nowait()
                if not req.future.done():
                    req.future.set_exception(exc)
            except asyncio.QueueEmpty:
                break

    async def _run(self) -> None:
        """
        The single owning task. Both stdio_client() and ClientSession() are
        entered here and will exit here — in the SAME task.
        """
        backoff = 0.5
        while True:
            if self._breaker_open():
                err = CircuitOpenError(
                    f"MCP server crashed {self._max_failures}+ times "
                    f"in {self._window}s; circuit open"
                )
                self._drain_with(err)
                await asyncio.sleep(self._window)
                self._failures.clear()
                continue

            try:
                async with stdio_client(self._params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        # Always re-list after every (re)connect — never stale cache.
                        result = await session.list_tools()
                        self.tools = result.tools
                        self._ready.set()
                        backoff = 0.5
                        await self._serve(session)
            except asyncio.CancelledError:
                return
            except Exception:
                self._failures.append(time.monotonic())
                self._ready.clear()
                jitter = random.uniform(0, backoff)
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, 30.0)

    async def _serve(self, session: ClientSession) -> None:
        """Multiplex tool calls from the inbox onto the session."""
        while True:
            req = await self._inbox.get()
            try:
                with anyio.fail_after(req.timeout):
                    result = await session.call_tool(req.name, req.args)
                if not req.future.done():
                    req.future.set_result(result)
            except TimeoutError as exc:
                if not req.future.done():
                    req.future.set_exception(exc)
            except Exception as exc:
                if not req.future.done():
                    req.future.set_exception(exc)
                raise  # bubble connection-level errors to _run for reconnect
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py -m mocked -v`
Expected: PASS (1 test).

---

## Task 14: Circuit breaker behavior (TDD)

**Files:**
- Modify: `tests/services/mcp-bridge/test_mcp_client_manager.py` (append tests)

- [ ] **Step 1: Append the failing tests**

```python
@pytest.mark.mocked
def test_circuit_breaker_opens_after_max_failures():
    mgr = MCPClientManager(_FakeParams(), max_failures=3, window=60.0)
    now = time.monotonic()
    for _ in range(3):
        mgr._failures.append(now)
    assert mgr._breaker_open() is True


@pytest.mark.mocked
def test_circuit_resets_after_cooldown(monkeypatch):
    mgr = MCPClientManager(_FakeParams(), max_failures=3, window=60.0)
    base = 1000.0
    # All 3 failures recorded at base time.
    for _ in range(3):
        mgr._failures.append(base)
    # Jump the clock past the window so old failures expire.
    monkeypatch.setattr(mcm.time, "monotonic", lambda: base + 61.0)
    assert mgr._breaker_open() is False
    assert len(mgr._failures) == 0


@pytest.mark.mocked
async def test_drain_with_fails_all_pending():
    mgr = MCPClientManager(_FakeParams())
    f1 = asyncio.get_running_loop().create_future()
    f2 = asyncio.get_running_loop().create_future()
    await mgr._inbox.put(_Req("a", {}, f1))
    await mgr._inbox.put(_Req("b", {}, f2))
    mgr._drain_with(CircuitOpenError("open"))
    with pytest.raises(CircuitOpenError):
        f1.result()
    with pytest.raises(CircuitOpenError):
        f2.result()
```

- [ ] **Step 2: Run tests to verify the new ones pass**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py -m mocked -v`
Expected: PASS — the 3 new tests pass against the existing implementation (they exercise `_breaker_open` / `_drain_with` written in Task 13).

---

## Task 15: Per-call timeout (TDD)

**Files:**
- Modify: `tests/services/mcp-bridge/test_mcp_client_manager.py` (append a test)

- [ ] **Step 1: Append the failing test**

```python
@pytest.mark.mocked
async def test_per_call_timeout_raises_TimeoutError():
    mgr = MCPClientManager(_FakeParams(), call_timeout=0.05)

    class _SlowSession:
        async def call_tool(self, name, args):
            await asyncio.sleep(5)  # never completes within the timeout

    serve = asyncio.create_task(mgr._serve(_SlowSession()))
    try:
        with pytest.raises(TimeoutError):
            await mgr.call_tool("slow_tool", {}, timeout=0.05)
    finally:
        serve.cancel()
        await asyncio.gather(serve, return_exceptions=True)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py::test_per_call_timeout_raises_TimeoutError -m mocked -v`
Expected: PASS — `anyio.fail_after(0.05)` fires, `_serve` sets `TimeoutError` on the future.

---

## Task 16: Tool-list refresh on reconnect (TDD)

**Files:**
- Modify: `tests/services/mcp-bridge/test_mcp_client_manager.py` (append a test)

- [ ] **Step 1: Append the failing test**

```python
@pytest.mark.mocked
async def test_tool_list_refreshed_after_initialize():
    """
    _run() must call list_tools() after every initialize(). We drive one
    connect cycle with fake context managers, then cancel to exit cleanly.
    """
    import contextlib

    mgr = MCPClientManager(_FakeParams())
    calls = {"initialize": 0, "list_tools": 0}

    class _ToolsResult:
        tools = ["fs_read_file", "git_status"]

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            calls["initialize"] += 1

        async def list_tools(self):
            calls["list_tools"] += 1
            return _ToolsResult()

        async def call_tool(self, name, args):  # unused
            return None

    @contextlib.asynccontextmanager
    async def _fake_stdio(_params):
        yield ("read", "write")

    monkeypatch_done = []
    mcm.stdio_client = _fake_stdio  # type: ignore[assignment]
    mcm.ClientSession = _FakeSession  # type: ignore[assignment]
    monkeypatch_done.append(True)

    task = asyncio.create_task(mgr._run())
    try:
        await mgr.wait_ready(timeout=2.0)
        assert calls["initialize"] == 1
        assert calls["list_tools"] == 1
        assert mgr.tools == ["fs_read_file", "git_status"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
```

> Note: this test rebinds `mcm.stdio_client` and `mcm.ClientSession` at module scope. Because `_run()` references them as module globals, the fakes take effect. The test runs last in the file is not required, but if other tests need the real symbols, restore them in a fixture; here no later test connects, so no restore is needed.

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py::test_tool_list_refreshed_after_initialize -m mocked -v`
Expected: PASS — `wait_ready` returns, `initialize` and `list_tools` each called once, `mgr.tools` populated.

---

## Task 17: Shutdown cancels the owning task (TDD)

**Files:**
- Modify: `tests/services/mcp-bridge/test_mcp_client_manager.py` (append a test)

- [ ] **Step 1: Append the failing test**

```python
@pytest.mark.mocked
async def test_shutdown_cancels_owning_task():
    mgr = MCPClientManager(_FakeParams())

    started = asyncio.Event()

    async def fake_run():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    mgr._run = fake_run  # type: ignore[assignment]
    await mgr.start()
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert mgr._task is not None and not mgr._task.done()

    await mgr.shutdown()
    assert mgr._task.cancelled()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py::test_shutdown_cancels_owning_task -m mocked -v`
Expected: PASS — `shutdown()` cancels the owning task and it reports cancelled.

---

## Task 18: Cancel-scope ownership assertion (TDD)

**Files:**
- Modify: `tests/services/mcp-bridge/test_mcp_client_manager.py` (append a test)

- [ ] **Step 1: Append the failing test**

```python
@pytest.mark.mocked
async def test_cancel_scope_stays_in_owning_task():
    """
    Enter and exit of session context managers must occur in the SAME task.
    We record the task that enters and the task that exits and assert equality.
    """
    import contextlib

    mgr = MCPClientManager(_FakeParams())
    enter_task = {}
    exit_task = {}

    class _SessionTrack:
        async def __aenter__(self):
            enter_task["t"] = asyncio.current_task()
            return self

        async def __aexit__(self, *exc):
            exit_task["t"] = asyncio.current_task()
            return False

        async def initialize(self):
            pass

        async def list_tools(self):
            class _R:
                tools = []
            return _R()

        async def call_tool(self, name, args):
            return None

    @contextlib.asynccontextmanager
    async def _fake_stdio(_params):
        yield ("r", "w")

    mcm.stdio_client = _fake_stdio  # type: ignore[assignment]
    mcm.ClientSession = lambda r, w: _SessionTrack()  # type: ignore[assignment]

    task = asyncio.create_task(mgr._run())
    try:
        await mgr.wait_ready(timeout=2.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert enter_task["t"] is task
    assert exit_task["t"] is task
    assert enter_task["t"] is exit_task["t"]
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py::test_cancel_scope_stays_in_owning_task -m mocked -v`
Expected: PASS — the owning task enters and exits the session; both recorded tasks are identical.

- [ ] **Step 3: Run the full Python suite**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py -m mocked -v`
Expected: PASS — all mocked tests (submit, breaker open, breaker reset, drain, timeout, tool-list refresh, shutdown, cancel-scope ownership).

---

## Task 19: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full TypeScript suite + type check**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx tsc --noEmit && npx vitest run`
Expected: type check clean; all 13 TS tests pass.

- [ ] **Step 2: Full Python suite**

Run: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/mcp-bridge/test_mcp_client_manager.py -m mocked -v`
Expected: all mocked tests pass.

- [ ] **Step 3: Manual stdout-hygiene smoke check (optional)**

Run: `cd /Users/zachstallbohm/Work/gemma/services/mcp-bridge && npx tsc && (echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'; sleep 1) | node dist/index.js 2>/tmp/bridge.stderr | head -c 200`
Expected: stdout shows a JSON-RPC response object (starts with `{"result"` or `{"jsonrpc"`); all log lines land in `/tmp/bridge.stderr`, never on stdout.

---

## Self-Review Notes

- **Spec coverage:** TS server bootstrap (Task 11), logger/stderr (Task 4), registry/domain modules (Tasks 5-11), fs/git/exec tools (Tasks 8-10), truncate + CHARACTER_LIMIT (Tasks 2-3), graceful shutdown SIGINT/SIGTERM/uncaughtException (Task 11 index.ts), Zod `.strict().describe()` (Tasks 5-7), isError pattern (every handler), `MCPClientManager` full class with single owning task / multiplexer / circuit breaker / per-call timeout / tool-list refresh (Tasks 13-18), dependencies pinned (`@modelcontextprotocol/sdk` 1.12.1, `mcp>=1.27,<2`).
- **Checklist:** no placeholders; every handler try/catch → `isError: true`; every file task has a test step or is exercised by an integration test; Python tests `@pytest.mark.mocked` (no real subprocess/network); TS tests use `vi.mock`/`InMemoryTransport` (no real fs/git/stdout); `asyncio_mode = "auto"` in pyproject.toml (Task 12); no git commit steps; `CHARACTER_LIMIT = 25_000` enforced in fs/git/exec read paths; pino → `pino.destination(2)`; `_run()` is the only place stdio_client/ClientSession are entered/exited.
