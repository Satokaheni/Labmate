# react-doctor MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the react-doctor TypeScript MCP server — a thin wrapper around the react-doctor CLI that exposes deterministic React static analysis as two MCP tools.

**Architecture:** ReactDoctorRunner spawns the react-doctor CLI as a child process, captures stdout (JSON issues), parses into typed Issue objects, and returns JSONL via the MCP server. No LLM inference. REACT_DOCTOR_CMD is configurable via env var. All logging goes to console.error (never console.log). Offline/missing-CLI errors return structured error objects.

**Tech Stack:** Node.js 20+, TypeScript 5+, `@modelcontextprotocol/sdk`, `zod`, `vitest`

---

## Background

react-doctor (https://github.com/millionco/react-doctor) is a deterministic TypeScript CLI that performs static analysis of React codebases across five categories: State & Effects, Performance, Architecture, Security, and Accessibility. It is framework-aware (Next.js, Vite, TanStack, React Native, Expo) and is run via `npx react-doctor@latest` at a project root. Issues carry stable rule IDs (e.g. `react-doctor/no-array-index-as-key`). It has a CI mode that reports only newly introduced issues versus a baseline.

This MCP server wraps the CLI as a child process — no LLM inference is involved. It exists so the Labmate orchestrator can run it as a QA gate after generating or modifying React code.

**Two tools exposed:**
- `react_doctor.audit(project_path, rules?, ci_mode?)` — run react-doctor, return JSONL of issues
- `react_doctor.list_rules()` — list all rule IDs grouped by category

**Critical rules (non-negotiable):**
- stdout is sacred: NEVER `console.log()`. ALWAYS `console.error()` for logging. stdout carries JSON-RPC.
- `REACT_DOCTOR_CMD = process.env.REACT_DOCTOR_CMD ?? "npx react-doctor@latest"` — never hardcode.
- Spawn with `{ stdio: ["pipe", "pipe", "pipe"] }`; parse stdout as JSON; pipe CLI stderr to `console.error`.
- Missing CLI or unexpected exit → return a structured error, never crash.
- TypeScript files: camelCase.ts. Interfaces: PascalCase.

---

## Phase 1 — Project scaffolding

### Task 1.1 — Create directory structure

- [ ] Create the skill directory tree under `services/skills/react-doctor/`:

```bash
mkdir -p services/skills/react-doctor/src
mkdir -p services/skills/react-doctor/dist
mkdir -p tests
```

Resulting layout:

```
services/skills/react-doctor/
  src/
    index.ts
    runner.ts
    types.ts
  SKILL.md
  package.json
  tsconfig.json
  dist/
tests/
  runner.test.ts
```

### Task 1.2 — Write package.json

- [ ] Create `services/skills/react-doctor/package.json`:

```json
{
  "name": "@labmate/skill-react-doctor",
  "version": "0.1.0",
  "description": "MCP server wrapping the react-doctor CLI for deterministic React static analysis",
  "license": "MIT",
  "type": "module",
  "main": "dist/index.js",
  "bin": {
    "skill-react-doctor": "dist/index.js"
  },
  "scripts": {
    "build": "tsc",
    "start": "node dist/index.js",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.0.0",
    "zod": "^3.23.8"
  },
  "devDependencies": {
    "@types/node": "^20.14.0",
    "typescript": "^5.5.0",
    "vitest": "^2.0.0"
  },
  "engines": {
    "node": ">=20"
  }
}
```

### Task 1.3 — Write tsconfig.json

- [ ] Create `services/skills/react-doctor/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "./dist",
    "rootDir": "./src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "declaration": true,
    "sourceMap": true
  },
  "include": ["src/**/*.ts"],
  "exclude": ["node_modules", "dist", "tests"]
}
```

### Task 1.4 — Install dependencies

- [ ] Run install inside the skill directory:

```bash
cd services/skills/react-doctor && npm install
```

---

## Phase 2 — Types

### Task 2.1 — Write types.ts

- [ ] Create `services/skills/react-doctor/src/types.ts`:

```typescript
export type IssueCategory =
  | 'state_effects'
  | 'performance'
  | 'architecture'
  | 'security'
  | 'accessibility';

export type IssueSeverity = 'error' | 'warning' | 'info';

export interface Issue {
  rule_id: string;
  category: IssueCategory;
  severity: IssueSeverity;
  file: string;
  line: number;
  column: number;
  message: string;
}

export interface AuditResult {
  project_path: string;
  issue_count: number;
  issues: Issue[];
  ci_mode: boolean; // true = only new issues vs baseline
  exit_code: number;
}

/** Returned (not thrown) when the CLI is missing or exits unexpectedly. */
export interface AuditError {
  error: true;
  project_path: string;
  message: string;
  exit_code: number | null;
}

export function isAuditError(r: AuditResult | AuditError): r is AuditError {
  return (r as AuditError).error === true;
}
```

---

## Phase 3 — ReactDoctorRunner (TDD)

> Use superpowers:test-driven-development for this phase. Write the failing test first, then the implementation.

### Task 3.1 — Write runner test scaffold with spawn mock

- [ ] Create `tests/runner.test.ts` with the spawn mock harness and the first test:

```typescript
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';

// Mock node:child_process before importing the runner.
const spawnMock = vi.fn();
vi.mock('node:child_process', () => ({ spawn: spawnMock }));

import { ReactDoctorRunner } from '../services/skills/react-doctor/src/runner.js';
import { isAuditError } from '../services/skills/react-doctor/src/types.js';

/** Build a fake ChildProcess that emits the given stdout, stderr, and exit code. */
function fakeChild(opts: { stdout?: string; stderr?: string; code?: number | null; spawnError?: Error }) {
  const child = new EventEmitter() as any;
  child.stdout = Readable.from(opts.stdout ? [opts.stdout] : []);
  child.stderr = Readable.from(opts.stderr ? [opts.stderr] : []);
  // Emit exit/error asynchronously so listeners attach first.
  queueMicrotask(() => {
    if (opts.spawnError) {
      child.emit('error', opts.spawnError);
    } else {
      child.emit('close', opts.code ?? 0);
    }
  });
  return child;
}

beforeEach(() => {
  spawnMock.mockReset();
  delete process.env.REACT_DOCTOR_CMD;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ReactDoctorRunner.audit', () => {
  it('returns an AuditResult with the correct issue count and shape', async () => {
    const payload = {
      issues: [
        {
          rule_id: 'react-doctor/no-array-index-as-key',
          category: 'performance',
          severity: 'warning',
          file: 'src/List.tsx',
          line: 12,
          column: 5,
          message: 'Do not use array index as key',
        },
      ],
    };
    spawnMock.mockReturnValue(fakeChild({ stdout: JSON.stringify(payload), code: 0 }));

    const runner = new ReactDoctorRunner();
    const result = await runner.audit('/repo');

    expect(isAuditError(result)).toBe(false);
    if (isAuditError(result)) return;
    expect(result.issue_count).toBe(1);
    expect(result.issues[0]).toMatchObject({
      rule_id: 'react-doctor/no-array-index-as-key',
      category: 'performance',
      severity: 'warning',
      file: 'src/List.tsx',
      line: 12,
      column: 5,
    });
    expect(result.project_path).toBe('/repo');
    expect(result.exit_code).toBe(0);
  });
});
```

- [ ] Run `cd services/skills/react-doctor && npx vitest run ../../../tests/runner.test.ts` and confirm it fails (no runner yet).

### Task 3.2 — Implement runner spawn + parse skeleton

- [ ] Create `services/skills/react-doctor/src/runner.ts` with the class, env-var default, and `audit()`:

```typescript
import { spawn } from 'node:child_process';
import type { AuditResult, AuditError, Issue, IssueCategory, IssueSeverity } from './types.js';

const VALID_CATEGORIES: IssueCategory[] = [
  'state_effects',
  'performance',
  'architecture',
  'security',
  'accessibility',
];
const VALID_SEVERITIES: IssueSeverity[] = ['error', 'warning', 'info'];

interface RawProcessResult {
  stdout: string;
  stderr: string;
  code: number | null;
  spawnError?: Error;
}

export class ReactDoctorRunner {
  private cmd: string;

  constructor(cmd = process.env.REACT_DOCTOR_CMD ?? 'npx react-doctor@latest') {
    this.cmd = cmd;
  }

  async audit(projectPath: string, rules?: string[], ciMode = false): Promise<AuditResult | AuditError> {
    const args = ['--json', '--cwd', projectPath];
    if (ciMode) args.push('--ci');
    if (rules && rules.length > 0) args.push('--rules', rules.join(','));

    let proc: RawProcessResult;
    try {
      proc = await this.run(args);
    } catch (err) {
      return this.toError(projectPath, err as Error, null);
    }

    if (proc.spawnError) {
      return this.toError(projectPath, proc.spawnError, null);
    }

    // react-doctor exits non-zero when issues are found; that is NOT an error.
    // Only treat unparseable output as an error.
    let issues: Issue[];
    try {
      issues = this.parseOutput(proc.stdout);
    } catch (err) {
      return this.toError(projectPath, err as Error, proc.code);
    }

    return {
      project_path: projectPath,
      issue_count: issues.length,
      issues,
      ci_mode: ciMode,
      exit_code: proc.code ?? 0,
    };
  }

  async listRules(): Promise<Record<string, string[]>> {
    const proc = await this.run(['--list-rules', '--json']);
    if (proc.spawnError) {
      console.error('[react-doctor] list_rules spawn failed:', proc.spawnError.message);
      return {};
    }
    return this.parseRules(proc.stdout);
  }

  private parseOutput(stdout: string): Issue[] {
    const trimmed = stdout.trim();
    if (!trimmed) return [];
    const parsed = JSON.parse(trimmed);
    const rawIssues: unknown[] = Array.isArray(parsed) ? parsed : (parsed.issues ?? []);
    return rawIssues.map((r) => this.normalizeIssue(r as Record<string, unknown>));
  }

  private normalizeIssue(r: Record<string, unknown>): Issue {
    const category = String(r.category ?? '') as IssueCategory;
    const severity = String(r.severity ?? 'info') as IssueSeverity;
    return {
      rule_id: String(r.rule_id ?? r.ruleId ?? ''),
      category: VALID_CATEGORIES.includes(category) ? category : 'architecture',
      severity: VALID_SEVERITIES.includes(severity) ? severity : 'info',
      file: String(r.file ?? r.filePath ?? ''),
      line: Number(r.line ?? 0),
      column: Number(r.column ?? r.col ?? 0),
      message: String(r.message ?? ''),
    };
  }

  private parseRules(stdout: string): Record<string, string[]> {
    const trimmed = stdout.trim();
    if (!trimmed) return {};
    const parsed = JSON.parse(trimmed);
    const out: Record<string, string[]> = {};
    const rules: Array<Record<string, unknown>> = Array.isArray(parsed)
      ? parsed
      : (parsed.rules ?? []);
    for (const rule of rules) {
      const category = String(rule.category ?? 'architecture');
      const id = String(rule.rule_id ?? rule.ruleId ?? rule.id ?? '');
      if (!id) continue;
      (out[category] ??= []).push(id);
    }
    return out;
  }

  private toError(projectPath: string, err: Error, code: number | null): AuditError {
    console.error('[react-doctor] audit failed:', err.message);
    return { error: true, project_path: projectPath, message: err.message, exit_code: code };
  }

  /** Spawn the CLI and collect stdout/stderr. CLI stderr is forwarded to console.error. */
  private run(args: string[]): Promise<RawProcessResult> {
    const [command, ...baseArgs] = this.cmd.split(' ');
    return new Promise((resolve) => {
      const child = spawn(command, [...baseArgs, ...args], {
        stdio: ['pipe', 'pipe', 'pipe'],
      });

      let stdout = '';
      let stderr = '';

      child.stdout.on('data', (d) => {
        stdout += d.toString();
      });
      child.stderr.on('data', (d) => {
        const text = d.toString();
        stderr += text;
        console.error('[react-doctor:cli]', text.trimEnd());
      });

      child.on('error', (err) => {
        resolve({ stdout, stderr, code: null, spawnError: err as Error });
      });
      child.on('close', (code) => {
        resolve({ stdout, stderr, code });
      });
    });
  }
}
```

- [ ] Run the test from Task 3.1 and confirm it passes.

### Task 3.3 — Add test: issue field correctness across categories

- [ ] Append to `tests/runner.test.ts`:

```typescript
describe('ReactDoctorRunner issue normalization', () => {
  it('preserves rule_id, category, severity, file, line for each issue', async () => {
    const payload = {
      issues: [
        { rule_id: 'react-doctor/exhaustive-deps', category: 'state_effects', severity: 'error', file: 'a.tsx', line: 3, column: 1, message: 'm1' },
        { rule_id: 'react-doctor/no-dangerously-set-inner-html', category: 'security', severity: 'error', file: 'b.tsx', line: 9, column: 2, message: 'm2' },
        { rule_id: 'react-doctor/img-alt', category: 'accessibility', severity: 'warning', file: 'c.tsx', line: 1, column: 1, message: 'm3' },
      ],
    };
    spawnMock.mockReturnValue(fakeChild({ stdout: JSON.stringify(payload), code: 1 }));

    const runner = new ReactDoctorRunner();
    const result = await runner.audit('/repo');
    if (isAuditError(result)) throw new Error('unexpected error result');

    expect(result.issue_count).toBe(3);
    expect(result.issues.map((i) => i.category)).toEqual([
      'state_effects',
      'security',
      'accessibility',
    ]);
    expect(result.issues[1].rule_id).toBe('react-doctor/no-dangerously-set-inner-html');
    // Non-zero exit due to issues is still a successful audit.
    expect(result.exit_code).toBe(1);
  });
});
```

- [ ] Run and confirm pass.

### Task 3.4 — Add test: ci_mode passes the --ci flag

- [ ] Append:

```typescript
describe('ReactDoctorRunner.audit ci_mode', () => {
  it('passes the --ci flag and sets ci_mode in the result', async () => {
    spawnMock.mockReturnValue(fakeChild({ stdout: '{"issues":[]}', code: 0 }));

    const runner = new ReactDoctorRunner('npx react-doctor@latest');
    const result = await runner.audit('/repo', undefined, true);
    if (isAuditError(result)) throw new Error('unexpected error result');

    expect(result.ci_mode).toBe(true);
    const [, args] = spawnMock.mock.calls[0];
    expect(args).toContain('--ci');
  });
});
```

- [ ] Run and confirm pass.

### Task 3.5 — Add test: rules filter is forwarded

- [ ] Append:

```typescript
describe('ReactDoctorRunner.audit rules filter', () => {
  it('forwards the rules list as a comma-separated --rules flag', async () => {
    spawnMock.mockReturnValue(fakeChild({ stdout: '{"issues":[]}', code: 0 }));

    const runner = new ReactDoctorRunner();
    await runner.audit('/repo', ['react-doctor/exhaustive-deps', 'react-doctor/img-alt']);

    const [, args] = spawnMock.mock.calls[0];
    const idx = args.indexOf('--rules');
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(args[idx + 1]).toBe('react-doctor/exhaustive-deps,react-doctor/img-alt');
  });
});
```

- [ ] Run and confirm pass.

### Task 3.6 — Add test: structured error on spawn failure (CLI missing)

- [ ] Append:

```typescript
describe('ReactDoctorRunner.audit error handling', () => {
  it('returns a structured AuditError (does not throw) when the CLI cannot be spawned', async () => {
    spawnMock.mockReturnValue(
      fakeChild({ spawnError: Object.assign(new Error('spawn npx ENOENT'), { code: 'ENOENT' }) }),
    );

    const runner = new ReactDoctorRunner('npx react-doctor@latest');
    const result = await runner.audit('/repo');

    expect(isAuditError(result)).toBe(true);
    if (!isAuditError(result)) return;
    expect(result.error).toBe(true);
    expect(result.message).toContain('ENOENT');
    expect(result.project_path).toBe('/repo');
    expect(result.exit_code).toBeNull();
  });

  it('returns a structured AuditError when stdout is not valid JSON', async () => {
    spawnMock.mockReturnValue(fakeChild({ stdout: 'not json at all', code: 2 }));

    const runner = new ReactDoctorRunner();
    const result = await runner.audit('/repo');

    expect(isAuditError(result)).toBe(true);
    if (!isAuditError(result)) return;
    expect(result.exit_code).toBe(2);
  });
});
```

- [ ] Run and confirm pass.

### Task 3.7 — Add test: console.log is never called

- [ ] Append:

```typescript
describe('stdout discipline', () => {
  it('never calls console.log during audit', async () => {
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    spawnMock.mockReturnValue(fakeChild({ stdout: '{"issues":[]}', stderr: 'some cli noise', code: 0 }));

    const runner = new ReactDoctorRunner();
    await runner.audit('/repo');

    expect(logSpy).not.toHaveBeenCalled();
  });
});
```

- [ ] Run and confirm pass. (CLI stderr noise should route to console.error, not console.log.)

### Task 3.8 — Add test: REACT_DOCTOR_CMD env var is used

- [ ] Append:

```typescript
describe('REACT_DOCTOR_CMD configuration', () => {
  it('uses the REACT_DOCTOR_CMD env var when no explicit cmd is passed', async () => {
    process.env.REACT_DOCTOR_CMD = 'pnpm dlx react-doctor';
    spawnMock.mockReturnValue(fakeChild({ stdout: '{"issues":[]}', code: 0 }));

    const runner = new ReactDoctorRunner();
    await runner.audit('/repo');

    const [command, args] = spawnMock.mock.calls[0];
    expect(command).toBe('pnpm');
    expect(args.slice(0, 2)).toEqual(['dlx', 'react-doctor']);
  });

  it('falls back to npx react-doctor@latest when env var is unset', async () => {
    spawnMock.mockReturnValue(fakeChild({ stdout: '{"issues":[]}', code: 0 }));

    const runner = new ReactDoctorRunner();
    await runner.audit('/repo');

    const [command] = spawnMock.mock.calls[0];
    expect(command).toBe('npx');
  });
});
```

- [ ] Run the full test file and confirm all tests pass.

---

## Phase 4 — MCP server entry point

### Task 4.1 — Write index.ts MCP server

- [ ] Create `services/skills/react-doctor/src/index.ts`:

```typescript
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { ReactDoctorRunner } from './runner.js';
import { isAuditError } from './types.js';

const runner = new ReactDoctorRunner();

const server = new McpServer({
  name: 'react-doctor',
  version: '0.1.0',
});

server.registerTool(
  'react_doctor.audit',
  {
    title: 'Audit a React project',
    description:
      'Run react-doctor static analysis on a React project. Returns JSONL of issues, ' +
      'one per line, each with rule_id, category, severity, file, line, column, message.',
    inputSchema: {
      project_path: z.string().describe('Absolute path to the React project root'),
      rules: z
        .array(z.string())
        .optional()
        .describe('Optional list of rule IDs to restrict the audit to'),
      ci_mode: z
        .boolean()
        .optional()
        .default(false)
        .describe('When true, report only newly introduced issues vs the baseline'),
    },
  },
  async ({ project_path, rules, ci_mode }) => {
    const result = await runner.audit(project_path, rules, ci_mode ?? false);

    if (isAuditError(result)) {
      return {
        isError: true,
        content: [{ type: 'text', text: JSON.stringify(result) }],
      };
    }

    // Emit JSONL: one issue per line. Empty result => empty string.
    const jsonl = result.issues.map((i) => JSON.stringify(i)).join('\n');
    const summary = JSON.stringify({
      project_path: result.project_path,
      issue_count: result.issue_count,
      ci_mode: result.ci_mode,
      exit_code: result.exit_code,
    });

    return {
      content: [{ type: 'text', text: `${summary}\n${jsonl}`.trimEnd() }],
    };
  },
);

server.registerTool(
  'react_doctor.list_rules',
  {
    title: 'List react-doctor rules',
    description:
      'List all available react-doctor rule IDs grouped by category ' +
      '(state_effects, performance, architecture, security, accessibility).',
    inputSchema: {},
  },
  async () => {
    const rules = await runner.listRules();
    return { content: [{ type: 'text', text: JSON.stringify(rules, null, 2) }] };
  },
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('[react-doctor] MCP server started on stdio');
}

main().catch((err) => {
  console.error('[react-doctor] fatal:', err);
  process.exit(1);
});
```

### Task 4.2 — Build the server

- [ ] Run the TypeScript build and confirm it compiles with no errors:

```bash
cd services/skills/react-doctor && npm run build
```

- [ ] Confirm `dist/index.js`, `dist/runner.js`, `dist/types.js` exist.

---

## Phase 5 — SKILL.md

### Task 5.1 — Write SKILL.md

- [ ] Create `services/skills/react-doctor/SKILL.md`:

```markdown
---
name: react-doctor
description: >
  Deterministic static analysis of React codebases via react-doctor CLI. Checks
  State & Effects, Performance, Architecture, Security, and Accessibility rules.
  Use as a QA gate after any React code generation to catch common anti-patterns
  before returning code to the user. Returns issues with stable rule IDs.
trigger: "Use after generating or modifying React code to catch anti-patterns"
tools:
  - react_doctor.audit
  - react_doctor.list_rules
version: "0.1.0"
license: MIT
requires: []
---

# react-doctor

Wraps the [react-doctor](https://github.com/millionco/react-doctor) CLI as an MCP
server. Purely deterministic static analysis — no LLM inference.

## Tools

### `react_doctor.audit(project_path, rules?, ci_mode?)`

Run react-doctor on a React project root. Returns a summary line followed by JSONL,
one issue per line. Each issue has:

- `rule_id` — stable ID, e.g. `react-doctor/no-array-index-as-key`
- `category` — one of `state_effects`, `performance`, `architecture`, `security`, `accessibility`
- `severity` — `error`, `warning`, or `info`
- `file`, `line`, `column`
- `message`

Parameters:
- `project_path` (required) — absolute path to the project root
- `rules` (optional) — restrict the audit to specific rule IDs
- `ci_mode` (optional, default false) — report only newly introduced issues vs the baseline

On a missing CLI or unexpected failure, returns a structured error object
(`{ "error": true, ... }`) with `isError: true` — it never crashes.

### `react_doctor.list_rules()`

Returns all available rule IDs grouped by category.

## Configuration

- `REACT_DOCTOR_CMD` — override the CLI invocation. Defaults to `npx react-doctor@latest`.
  Example: `pnpm dlx react-doctor`.

## Usage notes

react-doctor exits non-zero when issues are found; this is treated as a successful
audit (issues are returned), not an error. Only an unspawnable CLI or unparseable
output yields an error result.
```

---

## Phase 6 — Verification

### Task 6.1 — Run the full test suite

- [ ] From the skill directory, run all tests and confirm green:

```bash
cd services/skills/react-doctor && npx vitest run ../../../tests/runner.test.ts
```

### Task 6.2 — Manual stdio smoke test (optional, requires CLI)

- [ ] If react-doctor is available, send a `tools/list` JSON-RPC request over stdin and confirm both tools appear on stdout and that no non-JSON bytes pollute stdout:

```bash
cd services/skills/react-doctor
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | node dist/index.js
```

Confirm: the server startup log appears on **stderr** (not stdout), and stdout contains only the JSON-RPC response listing `react_doctor.audit` and `react_doctor.list_rules`.

### Task 6.3 — Self-review checklist

- [ ] Both tools (`react_doctor.audit`, `react_doctor.list_rules`) are registered in `index.ts`.
- [ ] `grep -rn "console.log" services/skills/react-doctor/src` returns nothing.
- [ ] `REACT_DOCTOR_CMD` env var is the single source for the CLI command (no hardcoded `npx` outside the default).
- [ ] Spawn uses `stdio: ["pipe", "pipe", "pipe"]` and forwards CLI stderr to `console.error`.
- [ ] Missing CLI / non-JSON output returns a structured `AuditError` with `isError: true`, never a throw to the transport.
- [ ] All `.ts` files are camelCase; all interfaces are PascalCase.

---

## Done criteria

- `npm run build` compiles cleanly.
- All vitest tests pass (issue count/shape, category fields, ci_mode flag, rules filter, structured error on spawn failure and bad JSON, console.log-never-called, env-var resolution).
- `SKILL.md` frontmatter lists both tools and the skill loads in the MCP bridge.
