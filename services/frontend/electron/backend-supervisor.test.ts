import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EventEmitter } from 'node:events';

// Fake child process: an EventEmitter with stdout/stderr streams + a kill spy.
class FakeChild extends EventEmitter {
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  kill = vi.fn();
  killed = false;
}
// vi.mock(...) factories are hoisted above all other top-level code, so any
// state they close over must be declared via vi.hoisted() (a plain top-level
// const/let would not yet be initialized when the factory runs).
const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));
let fakeChild: FakeChild;
spawnMock.mockImplementation(() => { fakeChild = new FakeChild(); return fakeChild; });
vi.mock('node:child_process', () => ({ default: { spawn: spawnMock }, spawn: spawnMock }));

import { BackendSupervisor } from './backend-supervisor';

const OPTS = { gemmaBase: 'https://x/v1', localPort: 8799, repoRoot: '/repo' };

beforeEach(() => { spawnMock.mockClear(); });

describe('BackendSupervisor.start', () => {
  it('resolves ready when healthz returns ok', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) })));
    const sup = new BackendSupervisor();
    const statuses: string[] = [];
    sup.onStatus((s) => statuses.push(s.phase));
    await sup.start(OPTS);
    expect(spawnMock).toHaveBeenCalledOnce();
    // GEMMA_BASE + LOCAL_PORT injected into the child env
    const env = spawnMock.mock.calls[0][2].env;
    expect(env.GEMMA_BASE).toBe('https://x/v1');
    expect(env.LOCAL_PORT).toBe('8799');
    expect(statuses).toContain('ready');
  });

  it('rejects with a log tail when the child exits before healthz', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('conn refused'); }));
    const sup = new BackendSupervisor();
    const p = sup.start({ ...OPTS });
    fakeChild.stderr.emit('data', Buffer.from('Traceback: boom\n'));
    fakeChild.emit('exit', 1);
    await expect(p).rejects.toThrow(/boom/);
  });

  it('stop() SIGTERMs then SIGKILLs the child', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) })));
    const sup = new BackendSupervisor();
    await sup.start(OPTS);
    const stopP = sup.stop();
    expect(fakeChild.kill).toHaveBeenCalledWith('SIGTERM');
    fakeChild.emit('exit', 0); // child obeys before the grace timer
    await stopP;
  });
});

describe('BackendSupervisor.probeModel', () => {
  it('sends a curl-style User-Agent (RunPod 403s the default)', async () => {
    const f = vi.fn(async () => ({ ok: true }));
    vi.stubGlobal('fetch', f);
    const ok = await BackendSupervisor.probeModel('https://pod/v1');
    expect(ok).toBe(true);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe('https://pod/health');            // /v1 stripped
    expect(init.headers['User-Agent']).toMatch(/curl/); // avoids the proxy 403
  });
});
