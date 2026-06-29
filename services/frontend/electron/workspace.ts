import fs from 'node:fs';
import path from 'node:path';

/**
 * Per-chat workspace state.
 *
 * `defaultWorkspace` is the global "current directory" — the assumed root a new
 * chat starts from. `perSession` holds each chat's ordered list of roots
 * (roots[0] is the primary; relative paths resolve against it, additional roots
 * are extra read locations). There is intentionally NO silent home-directory
 * fallback — a chat with no roots and no default surfaces an explicit error.
 */
export interface WorkspaceState {
  defaultWorkspace: string | null;
  perSession: Record<string, string[]>;
}

export function emptyWorkspaceState(): WorkspaceState {
  return { defaultWorkspace: null, perSession: {} };
}

/** Ordered roots for a session: explicit per-chat list, else [default], else []. */
export function resolveRoots(state: WorkspaceState, sessionId: string | null): string[] {
  if (sessionId && state.perSession[sessionId]?.length) return state.perSession[sessionId];
  return state.defaultWorkspace ? [state.defaultWorkspace] : [];
}

/**
 * Resolve a tool path argument against a chat's roots.
 *  - absolute path → allowed only if inside one of the roots
 *  - relative path → resolved against the primary root (roots[0])
 * Throws if there are no roots or the path escapes them.
 */
export function resolveToolPath(rel: string, roots: string[]): string {
  if (roots.length === 0) {
    throw new Error(
      'no workspace set for this chat — add a folder with the workspace control at the top',
    );
  }

  if (path.isAbsolute(rel)) {
    const candidate = path.resolve(rel);
    for (const root of roots) {
      const r = path.resolve(root);
      const relToRoot = path.relative(r, candidate);
      if (candidate === r || (!relToRoot.startsWith('..') && !path.isAbsolute(relToRoot))) {
        return candidate;
      }
    }
    throw new Error(`path "${rel}" is outside all workspace roots`);
  }

  const primary = path.resolve(roots[0]);
  const candidate = path.resolve(primary, rel);
  const relToRoot = path.relative(primary, candidate);
  if (candidate !== primary && (relToRoot.startsWith('..') || path.isAbsolute(relToRoot))) {
    throw new Error(`path "${rel}" resolves outside the primary workspace root`);
  }
  return candidate;
}

/** Parse a persisted workspace blob defensively (tolerates partial / corrupt JSON). */
export function parseWorkspaceState(raw: unknown): WorkspaceState {
  if (!raw || typeof raw !== 'object') return emptyWorkspaceState();
  const r = raw as Partial<WorkspaceState>;
  const perSession: Record<string, string[]> = {};
  if (r.perSession && typeof r.perSession === 'object') {
    for (const [k, v] of Object.entries(r.perSession)) {
      if (Array.isArray(v)) {
        const roots = v.filter((p): p is string => typeof p === 'string');
        if (roots.length) perSession[k] = roots;
      }
    }
  }
  return {
    defaultWorkspace: typeof r.defaultWorkspace === 'string' ? r.defaultWorkspace : null,
    perSession,
  };
}

/** File-backed workspace store (persists to the app config dir). */
export class WorkspaceStore {
  private state: WorkspaceState;

  constructor(private readonly filePath: string, seedDefault?: string | null) {
    this.state = WorkspaceStore.load(filePath);
    if (!this.state.defaultWorkspace && seedDefault) {
      this.state.defaultWorkspace = seedDefault;
      this.save();
    }
  }

  static load(filePath: string): WorkspaceState {
    try {
      return parseWorkspaceState(JSON.parse(fs.readFileSync(filePath, 'utf8')));
    } catch {
      return emptyWorkspaceState();
    }
  }

  private save(): void {
    try {
      fs.writeFileSync(this.filePath, JSON.stringify(this.state), 'utf8');
    } catch {
      /* best-effort persistence */
    }
  }

  roots(sessionId: string | null): string[] {
    return resolveRoots(this.state, sessionId);
  }

  hasDefault(): boolean {
    return this.state.defaultWorkspace !== null;
  }

  getDefault(): string | null {
    return this.state.defaultWorkspace;
  }

  setDefault(workspacePath: string): void {
    this.state.defaultWorkspace = workspacePath;
    this.save();
  }

  /** Add a root to a chat (materializing the default seed first), de-duplicated. */
  addRoot(sessionId: string, workspacePath: string): string[] {
    const current = this.roots(sessionId);
    const next = current.includes(workspacePath) ? current : [...current, workspacePath];
    this.state.perSession[sessionId] = next;
    this.save();
    return next;
  }

  removeRoot(sessionId: string, workspacePath: string): string[] {
    const current = this.roots(sessionId);
    const next = current.filter((p) => p !== workspacePath);
    this.state.perSession[sessionId] = next;
    this.save();
    return next;
  }
}
