import path from 'node:path';

export type Decision = 'allow' | 'ask' | 'deny';

/**
 * Allowed test-runner binaries for run_tests tool execution.
 * Phase 1 only: 'ask' UI deferred; anything not on the allowlist is 'deny'.
 */
const ALLOWED_TEST_BINS = new Set([
  'pytest',
  'python',
  'python3',
  'npm',
  'npx',
  'yarn',
  'pnpm',
  'jest',
  'vitest',
  'go',
  'cargo',
  'make',
  'bun',
  'deno',
]);

/**
 * Decide whether run_tests may execute this command.
 * argv[0] is the binary name; we extract the basename and compare against the allowlist.
 * Returns 'deny' if argv is empty or the binary is not allowed.
 * Phase 1: 'ask' deferred — only 'allow' or 'deny' are returned.
 */
export function checkTestCommand(argv: string[]): Decision {
  const bin = String(argv[0] ?? '');
  if (!bin) {
    return 'deny';
  }

  // Extract basename and strip Windows extensions
  const baseName = path.basename(bin).toLowerCase().replace(/\.(exe|cmd|bat)$/, '');
  return ALLOWED_TEST_BINS.has(baseName) ? 'allow' : 'deny';
}
