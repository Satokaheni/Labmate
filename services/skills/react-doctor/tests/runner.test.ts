import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';

// vi.hoisted ensures spawnMock is initialized before vi.mock hoists the factory to the top.
const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));
vi.mock('node:child_process', () => ({ spawn: spawnMock }));

import { ReactDoctorRunner } from '../src/runner.js';
import { isAuditError } from '../src/types.js';

/** Build a fake ChildProcess that emits the given stdout, stderr, and exit code. */
function fakeChild(opts: { stdout?: string; stderr?: string; code?: number | null; spawnError?: Error }) {
  const child = new EventEmitter() as any;
  child.stdout = new Readable({ read() {} });
  child.stderr = new Readable({ read() {} });

  // Emit data and end events asynchronously so listeners attach first.
  queueMicrotask(() => {
    if (opts.spawnError) {
      child.emit('error', opts.spawnError);
      return;
    }
    if (opts.stdout) {
      child.stdout.push(opts.stdout);
    }
    child.stdout.push(null); // signal EOF

    if (opts.stderr) {
      child.stderr.push(opts.stderr);
    }
    child.stderr.push(null); // signal EOF

    // Emit close after streams are done
    setImmediate(() => {
      child.emit('close', opts.code ?? 0);
    });
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

describe('stdout discipline', () => {
  it('never calls console.log during audit', async () => {
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    spawnMock.mockReturnValue(fakeChild({ stdout: '{"issues":[]}', stderr: 'some cli noise', code: 0 }));

    const runner = new ReactDoctorRunner();
    await runner.audit('/repo');

    expect(logSpy).not.toHaveBeenCalled();
  });
});

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
