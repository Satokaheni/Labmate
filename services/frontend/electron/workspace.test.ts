import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  resolveRoots,
  resolveToolPath,
  parseWorkspaceState,
  emptyWorkspaceState,
  WorkspaceStore,
  type WorkspaceState,
} from './workspace';

describe('resolveRoots', () => {
  const state: WorkspaceState = {
    defaultWorkspace: '/default',
    perSession: { s1: ['/repo-a', '/repo-b'] },
  };

  it('returns the per-session roots when set', () => {
    expect(resolveRoots(state, 's1')).toEqual(['/repo-a', '/repo-b']);
  });

  it('falls back to [default] when a chat has no roots', () => {
    expect(resolveRoots(state, 's2')).toEqual(['/default']);
  });

  it('returns [] when neither roots nor default exist (no silent home fallback)', () => {
    expect(resolveRoots(emptyWorkspaceState(), 's1')).toEqual([]);
  });
});

describe('resolveToolPath', () => {
  it('resolves a relative path against the primary root', () => {
    expect(resolveToolPath('src/x.ts', ['/repo-a', '/repo-b'])).toBe('/repo-a/src/x.ts');
  });

  it('accepts an absolute path inside any root', () => {
    expect(resolveToolPath('/repo-b/pkg/y.ts', ['/repo-a', '/repo-b'])).toBe('/repo-b/pkg/y.ts');
  });

  it('rejects an absolute path outside all roots', () => {
    expect(() => resolveToolPath('/etc/passwd', ['/repo-a'])).toThrow(/outside all workspace roots/);
  });

  it('rejects a relative path escaping the primary root', () => {
    expect(() => resolveToolPath('../secret', ['/repo-a'])).toThrow(/outside the primary/);
  });

  it('throws when there are no roots', () => {
    expect(() => resolveToolPath('x.ts', [])).toThrow(/no workspace set/);
  });
});

describe('parseWorkspaceState', () => {
  it('returns empty state for non-objects', () => {
    expect(parseWorkspaceState(null)).toEqual(emptyWorkspaceState());
    expect(parseWorkspaceState('nope')).toEqual(emptyWorkspaceState());
  });

  it('keeps only string entries in each session root list', () => {
    const parsed = parseWorkspaceState({ defaultWorkspace: '/d', perSession: { a: ['/x', 5, '/y'] } });
    expect(parsed).toEqual({ defaultWorkspace: '/d', perSession: { a: ['/x', '/y'] } });
  });

  it('coerces a non-string default to null', () => {
    expect(parseWorkspaceState({ defaultWorkspace: 42 }).defaultWorkspace).toBeNull();
  });
});

describe('WorkspaceStore', () => {
  let dir: string;
  let file: string;

  beforeEach(async () => {
    dir = await fs.mkdtemp(path.join(os.tmpdir(), 'lm-ws-'));
    file = path.join(dir, 'workspaces.json');
  });
  afterEach(async () => {
    await fs.rm(dir, { recursive: true, force: true });
  });

  it('seeds the default from env only when unset, and persists it', () => {
    const seeded = new WorkspaceStore(file, '/env/path');
    expect(seeded.getDefault()).toBe('/env/path');
    expect(seeded.hasDefault()).toBe(true);
    // Existing default is not overwritten by a later seed.
    expect(new WorkspaceStore(file, '/other').getDefault()).toBe('/env/path');
  });

  it('addRoot materializes the default seed then appends, de-duplicated', () => {
    const store = new WorkspaceStore(file, '/default');
    expect(store.roots('s1')).toEqual(['/default']); // seeded
    expect(store.addRoot('s1', '/repo-b')).toEqual(['/default', '/repo-b']);
    expect(store.addRoot('s1', '/repo-b')).toEqual(['/default', '/repo-b']); // no dupe

    const reloaded = new WorkspaceStore(file);
    expect(reloaded.roots('s1')).toEqual(['/default', '/repo-b']);
  });

  it('removeRoot drops a root and persists', () => {
    const store = new WorkspaceStore(file, '/default');
    store.addRoot('s1', '/repo-b');
    expect(store.removeRoot('s1', '/default')).toEqual(['/repo-b']);
    expect(new WorkspaceStore(file).roots('s1')).toEqual(['/repo-b']);
  });

  it('removing the only (seeded default) root sticks and does not reappear on add', () => {
    // Repro of the live bug: remove the default, then add a new dir — the removed
    // default must NOT come back.
    const store = new WorkspaceStore(file, '/default');
    expect(store.roots('s1')).toEqual(['/default']); // seeded
    expect(store.removeRoot('s1', '/default')).toEqual([]); // removed
    expect(store.roots('s1')).toEqual([]); // STAYS empty — no fallback to default
    // adding a new root must not re-materialize the removed default
    expect(store.addRoot('s1', '/labmate-skills')).toEqual(['/labmate-skills']);
    expect(store.roots('s1')).toEqual(['/labmate-skills']);
  });

  it('an explicitly emptied session persists as [] across reload (no default fallback)', () => {
    const store = new WorkspaceStore(file, '/default');
    store.removeRoot('s1', '/default');
    const reloaded = new WorkspaceStore(file, '/default');
    expect(reloaded.roots('s1')).toEqual([]); // not ['/default']
  });

  it('reports no default and empty roots on a fresh store', () => {
    const store = new WorkspaceStore(path.join(dir, 'nope.json'));
    expect(store.hasDefault()).toBe(false);
    expect(store.roots('s1')).toEqual([]);
  });
});
