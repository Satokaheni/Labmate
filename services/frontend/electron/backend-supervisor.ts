import { spawn, type ChildProcess } from 'node:child_process';
import * as path from 'node:path';

export type SupervisorStatus =
  | { phase: 'starting'; step: string }
  | { phase: 'ready' }
  | { phase: 'boot_failed'; logTail: string };

export interface StartOpts {
  gemmaBase: string | null;
  localPort: number;
  repoRoot: string;
}

const HEALTH_TIMEOUT_MS = 60_000;
const HEALTH_INTERVAL_MS = 500;
const STOP_GRACE_MS = 5_000;
const STOP_FALLBACK_MS = 1_000;

export class BackendSupervisor {
  private child: ChildProcess | null = null;
  private logTail: string[] = [];
  private cbs: ((s: SupervisorStatus) => void)[] = [];
  private childExited = false;

  onStatus(cb: (s: SupervisorStatus) => void): void { this.cbs.push(cb); }
  private emit(s: SupervisorStatus): void { for (const cb of this.cbs) cb(s); }

  private pushLog(chunk: Buffer): void {
    this.logTail.push(chunk.toString());
    if (this.logTail.length > 200) this.logTail.shift();
  }

  async start(opts: StartOpts): Promise<void> {
    if (this.child && !this.childExited) {
      // Already running — never spawn a second backend (would fight for LOCAL_PORT
      // and orphan the first). A retry from model_unreachable re-probes instead.
      this.emit({ phase: 'ready' });
      return;
    }
    this.emit({ phase: 'starting', step: 'launching backend' });
    const script = path.join(opts.repoRoot, 'infrastructure', 'start.sh');
    const child = spawn('bash', [script, '--foreground'], {
      cwd: opts.repoRoot,
      env: {
        ...process.env,
        ...(opts.gemmaBase ? { GEMMA_BASE: opts.gemmaBase } : {}),
        LOCAL_PORT: String(opts.localPort),
      },
    });
    this.child = child;
    this.childExited = false;
    child.stdout?.on('data', (c: Buffer) => this.pushLog(c));
    child.stderr?.on('data', (c: Buffer) => this.pushLog(c));
    child.on('exit', () => { this.childExited = true; });

    // Health-gate: poll /healthz until ok, the child exits, or we time out.
    const deadline = Date.now() + HEALTH_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (this.childExited) {
        const tail = this.logTail.join('').slice(-2000);
        this.emit({ phase: 'boot_failed', logTail: tail });
        throw new Error(`backend exited before ready:\n${tail}`);
      }
      try {
        const r = await fetch(`http://127.0.0.1:${opts.localPort}/healthz`);
        if (r.ok) { this.emit({ phase: 'ready' }); return; }
      } catch {
        /* not up yet */
      }
      this.emit({ phase: 'starting', step: 'waiting for backend health' });
      await new Promise((res) => setTimeout(res, HEALTH_INTERVAL_MS));
    }
    const tail = this.logTail.join('').slice(-2000);
    this.emit({ phase: 'boot_failed', logTail: tail });
    throw new Error(`backend health timeout:\n${tail}`);
  }

  async stop(): Promise<void> {
    const child = this.child;
    // Idempotent: nothing to stop, or the child is already gone (either we
    // killed it ourselves, or it exited on its own — crash / boot failure /
    // reaped). In the latter case `child.killed` stays false forever, so we
    // must also consult `childExited` or `stop()` would hang waiting on an
    // 'exit' event that will never fire again.
    if (!child || child.killed || this.childExited) {
      this.child = null;
      return;
    }
    await new Promise<void>((resolve) => {
      let graceTimer: ReturnType<typeof setTimeout> | undefined;
      let fallbackTimer: ReturnType<typeof setTimeout> | undefined;
      const finish = () => {
        if (graceTimer) clearTimeout(graceTimer);
        if (fallbackTimer) clearTimeout(fallbackTimer);
        resolve();
      };
      child.once('exit', finish);
      child.kill('SIGTERM');
      graceTimer = setTimeout(() => {
        try { child.kill('SIGKILL'); } catch { /* already gone */ }
        // Fallback: even if 'exit' never fires after SIGKILL (unforeseen
        // no-exit case), resolve so stop() can never hang forever.
        fallbackTimer = setTimeout(finish, STOP_FALLBACK_MS);
      }, STOP_GRACE_MS);
    });
    this.child = null;
  }

  // Model always lives on an external box. Probe {base}/health with a curl-style
  // UA — the RunPod proxy 403s the default Node/urllib UA (see the inference_available
  // fix on fix/live-skill-verification-macos-runpod).
  static async probeModel(gemmaBase: string): Promise<boolean> {
    const base = gemmaBase.replace(/\/v1\/?$/, '');
    try {
      const r = await fetch(`${base}/health`, { headers: { 'User-Agent': 'curl/8.0' } });
      return r.ok;
    } catch {
      return false;
    }
  }
}
