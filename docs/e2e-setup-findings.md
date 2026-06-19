# E2E Setup Findings — 2026-06-19

First full-stack e2e bring-up on a fresh RunPod pod (RTX 6000 Ada, 49 GB, CUDA 12.4).
This logs **every problem hit during setup + e2e** and the fix applied. Seven blocking
issues — **all fixed** — plus a few non-blocking notes at the end.

End state: **the full Redis round-trip AND the one-shot CLI path both pass** — task
travels CLI/Redis → orchestrator → LangGraph (plan→execute→check→reflect) → Gemma 4 →
result → Redis, returning `{"ok": true}`. 184 core unit tests pass.

---

## Fixed

### 1. `install.sh` — only installed 2 of 6 core service requirements  *(FIXED)*
It ran `pip install -r` for **memory** and **mcp-bridge** only. Missing:
`orchestrator`, `skill_runner`, `skill_worker`, `cli`.

Symptom: `start.sh` crashed the skill-worker immediately:
```
ModuleNotFoundError: No module named 'frontmatter'
```
(`python-frontmatter`, a `skill_runner` dep). `orchestrator` deps (litellm, langgraph,
redis, etc.) were also absent.

Fix: loop over all core services:
```bash
for svc in memory mcp-bridge orchestrator skill_runner skill_worker cli; do
  req="${REPO_ROOT}/services/${svc}/requirements.txt"
  [[ -f "$req" ]] && $PIP -r "$req"
done
```
(Skill-specific deps under `services/skills/*` are still installed lazily per skill —
intentional, to avoid pulling every skill's heavy deps for a core bring-up.)

### 2. `install.sh` — wrote build log to a non-existent dir  *(FIXED)*
The llama.cpp build step redirects to `${REPO_ROOT}/.data/logs/llama-build.log` but
nothing creates `.data/logs` first. On a truly fresh checkout the redirect fails before
the build even starts. (Masked in this run because the dir already existed.)

Fix: `mkdir -p "${REPO_ROOT}/.data/logs" "${REPO_ROOT}/.data/pids"` near the top.

### 3. `start.sh` — never rebuilt a *stale* MCP bridge  *(FIXED)*
The guard was `if [[ ! -f dist/index.js ]]`. A `dist/` compiled before a new source
file was added (here `src/utils/formatError.ts`) is present-but-incomplete, so it was
never rebuilt. The bridge then crashed at import:
```
Error [ERR_MODULE_NOT_FOUND]: Cannot find module '.../dist/utils/formatError.js'
```
which the orchestrator only surfaces as the soft warning
`MCP bridge did not become ready within 30 s — continuing` (it then runs with **0 skills**).

Fix: rebuild when `dist/index.js` is missing **or** any `src/*.ts` is newer than it,
and `rm -rf dist` before building:
```bash
_mcp_stale() {
  [[ ! -f "$MCP_DIST" ]] && return 0
  [[ -n "$(find "$MCP_BRIDGE_DIR/src" -name '*.ts' -newer "$MCP_DIST" -print -quit)" ]]
}
```

### 4. `coding_orchestrator.py` — litellm calls missing `api_key`  *(FIXED)*
All 4 `litellm.acompletion(...)` calls (in `AsyncOrchestrator` and `CodingOrchestrator`)
omitted `api_key`. Even though the local llama-server ignores the key, the OpenAI SDK
litellm uses **requires one to be present**:
```
openai.OpenAIError: Missing credentials. Please pass an `api_key` ...
```
This is inconsistent with the rest of the repo — **every other** litellm call site
(`memory_consolidator.py`, all `services/skills/*`) passes a dummy key
(`"not-needed"` / `"EMPTY"`).

Fix: added `api_key="not-needed"` to all 4 calls (lines ~160, 180, 268, 282), matching
the codebase convention. (Alternative: export `OPENAI_API_KEY` in `local.env` — but the
per-call dummy matches existing style and doesn't risk leaking to real OpenAI.)

### 5. `coding_orchestrator.py::plan_and_dispatch` — double `ts.done()`  *(FIXED)*
The TaskGroup scheduler marked a completed task done but never removed it from the
`running` dict, so the next `while ts.is_active()` iteration re-saw it and called
`ts.done(tid)` again:
```
ValueError: node 'root_sub0' was already marked done   (graphlib)
```
Fires whenever **≥2 sub-tasks are ready at once** (a single ready node never re-trips,
which is why a trivial task could look fine). For "What is 2+2" the planner emits 4
sub-goals, so it always failed.

Fix: remove from `running` before marking done:
```python
for tid, task in list(running.items()):
    if task.done():
        del running[tid]
        if not task.cancelled():
            ts.done(tid)
```

### 6. blocking `xreadgroup` timeout killed goal consumption — a redis-py 8.x regression  *(FIXED, two ways)*
**The hardest one.** The orchestrator would log `ready` but never read the goal stream:
no consumer registered, `lag` stuck at 1, tasks timed out. Intermittent — it "worked"
exactly when a message was already waiting when the loop first polled.

Root cause: `await self._redis.xreadgroup(..., block=5000)` on an **empty** stream raises
`redis.exceptions.TimeoutError` under **redis-py 8.x**. redis-py does not translate
`block=` into a read timeout — blocking reads rely on `socket_timeout=None` (wait
forever). 5.x honors that; 8.x regressed and raises a read-timeout at the BLOCK window
under a busy event loop. The loop only caught `aioredis.ResponseError`, so this
propagated and silently stopped polling. When a goal *was* already queued, `xreadgroup`
returned data *before* the timeout → looked fine → the intermittency.

**Empirically confirmed** (same orchestrator + busy event loop, only redis-py changed):

| redis-py | empty-stream poll outcome (per 5 s) |
|----------|-------------------------------------|
| 8.0.0    | `TimeoutError` raised — 0 clean returns |
| 5.2.1    | returns `[]` cleanly — never raises     |

Fix applied in **two** complementary places:
1. **Pinned `redis>=5.0,<6`** in all 4 requirements (orchestrator, memory, cli,
   skill_worker) — restores the correct blocking-read behavior and removes the spurious
   timeout/reconnect churn. Also dropped the dead `redis[asyncio]` extra (asyncio is
   built into redis-py since 4.2; the extra just emits a warning).
2. **Kept a defensive catch** in `_loop` — a blocking read should tolerate a timeout and
   re-poll regardless of version. It does **not** fire on 5.x, but guards against any
   future blocking-read timeout (network blip, version drift):
   ```python
   except (aioredis.TimeoutError, TimeoutError):
       continue
   ```
Verified end-to-end on redis-py 5.2.1: clean empty-stream start → push goal → consumed
+ `ok:true` + `XACK`.

### 7. `stop.sh` killed the model server, contradicting the docs  *(FIXED)*
`docs/e2e-testing.md` and `CLAUDE.md` say *"Stopping everything … does not kill the model
server"* (with a separate manual `kill` step). But `stop.sh` had a block that **did** kill
`llama-server`, taking down the ~10-min-to-load model on every support-stack restart.

Fix: `stop.sh` now **leaves the model running by default** and only stops it with an
explicit `--all` / `--model` flag:
```bash
./stop.sh         # support stack + orchestrator; model stays up
./stop.sh --all   # also stop llama-server
```

---

## Also noted (not blocking e2e)

- **Full `pytest tests/` fails at collection.** Skill tests need per-skill deps (not
  installed by design) and several share basenames (`test_server.py`, `models.py`)
  causing `import file mismatch`. Core suites pass: orchestrator (118) + cli/skill_worker/
  skill_runner (66) = **184**. Fix later with `--import-mode=importlib` (or per-dir
  `__init__.py`) and a way to install skill deps in CI.
- **MCP bridge restarts ~every 5 s** in the orchestrator (a fresh `labmate MCP server
  ready on stdio` appears periodically). Not fatal — the client reconnects and tasks
  complete — but worth understanding; likely related to the same blocking-read timeout
  churn recycling the stdio session. Low priority.
- **Test methodology gotcha (for whoever debugs next):** `pkill -f orchestrator.main`
  also matches your *own shell* if the command line contains that string. Kill
  orchestrators by PID (`ps -eo pid,args | grep '[s]ervices.orchestrator.main'`) instead.

---

## Files changed
- `infrastructure/local/install.sh` — all core service deps + `.data/logs` mkdir (#1, #2)
- `infrastructure/local/start.sh` — stale-bridge rebuild detection (#3)
- `infrastructure/local/stop.sh` — leave model up by default, `--all` to stop it (#7)
- `services/orchestrator/coding_orchestrator.py` — `api_key` ×4 (#4), TaskGroup dedup (#5)
- `services/orchestrator/main.py` — `_loop` defensive timeout catch (#6)
- `services/{orchestrator,memory,cli,skill_worker}/requirements.txt` — `redis>=5.0,<6`,
  dropped dead `[asyncio]` extra (#6)
