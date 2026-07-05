# Local (no-Docker) Single-Process Stack

This directory runs the entire Labmate harness — gateway, orchestrator, and all
state — as **plain host processes**, no containers, no MongoDB, no Redis, no
Chroma. All session/turn/auth/checkpoint state lives in one SQLite database
file managed by the `LocalStore`.

## Why this exists

The dev pod **cannot** run containers:

- No `NET_ADMIN` capability → Docker/Podman cannot create a bridge
  (`Failed to create bridge docker0 via netlink: operation not permitted`).
- The `unshare`/`clone` namespace syscalls are blocked by seccomp → even
  rootless Podman and `--network=host` fail (every namespace type returns
  `Operation not permitted`).

So no container engine can run here. Rather than provisioning MongoDB/Redis/Chroma
as native host processes (the original design of this directory), the harness
was re-architected (the `local-state-sqlite` piece) to remove those dependencies
entirely: `services.local.main` runs the gateway and orchestrator together on one
asyncio loop, backed by one SQLite file. There is nothing left to provision except
the inference server.

## Layout

```
infrastructure/
  docker/   ← legacy Docker Compose stack (superseded; kept for reference only)
  local/    ← this folder: the live stack — install/start/stop/serve-model
```

## Usage

```bash
infrastructure/install.sh     # ONE-TIME: system + python + llama.cpp + GGUF (idempotent)

infrastructure/serve-model.sh # Gemma 4 via llama.cpp on :8000 (OpenAI API at /v1)
infrastructure/start.sh       # start services.local.main (gateway + orchestrator, idempotent)
infrastructure/status.sh      # health check (model + gateway)
infrastructure/stop.sh        # stop all (SQLite data preserved)

source infrastructure/local.env   # export LOCAL_HOST / LOCAL_PORT / GEMMA_BASE / etc.
```

Full from-scratch / reinstall instructions, the **llama.cpp-vs-vLLM-CUDA-12.8
gotcha**, ports, and the auth model are in [`INSTALL.md`](./INSTALL.md).

## State model

Everything that used to be MongoDB (sessions, messages) + Chroma (vectors) +
Redis (task queue, event cache) is now one SQLite file: `services/orchestrator/local_store.py::LocalStore`,
shared in-process by the gateway and the orchestrator (`services.local.main`
constructs the orchestrator process first, then builds the gateway app against
that same running process — one `LocalStore`, one asyncio loop, no cross-process
transport). The LangGraph checkpointer (`SqliteSaver`) is backed by the same
database file, so it stays consistent with the rest of the state.

## Data

All state lives under `<repo>/.data/` (gitignored):

```
.data/
  labmate_state.sqlite   # SQLite: sessions, turns, auth_users, checkpoints (WAL mode)
  logs/                  # local.log, llama-server.log, llama-build.log
  pids/                  # local.pid, llama-server.pid
```

(`LABMATE_STATE_DIR` / `LABMATE_STATE_DB` override the directory/file path — see
`services/orchestrator/local_mode.py`.)
