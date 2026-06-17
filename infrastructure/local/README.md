# Local (no-Docker) Support Stack

This directory runs Labmate's support services — MongoDB, Redis, Chroma — as
**plain host processes**, no containers.

## Why this exists

The canonical deployment (see [`../docker/`](../docker/)) runs these services as
Docker Compose containers. That requires a host that can create container
networks and namespaces. The current dev pod **cannot**:

- No `NET_ADMIN` capability → Docker/Podman cannot create a bridge
  (`Failed to create bridge docker0 via netlink: operation not permitted`).
- The `unshare`/`clone` namespace syscalls are blocked by seccomp → even
  rootless Podman and `--network=host` fail (every namespace type returns
  `Operation not permitted`).

So no container engine can run here. Until Labmate moves to a privileged host /
your own server, these scripts provide the same three backends natively.

The application code is unaffected: `StorageManager` and the orchestrator read
`MONGO_URI` / `CHROMA_URL` / `REDIS_URL` from the environment, so only the
connection strings change.

## Layout

```
infrastructure/
  docker/   ← Docker Compose stack (the target deployment; use on a real host)
  local/    ← this folder: native host runners for the constrained pod
```

## Usage

```bash
infrastructure/local/install.sh    # ONE-TIME: system + python + llama.cpp + GGUF (idempotent)

infrastructure/local/start.sh      # start mongod + redis + chroma (idempotent)
infrastructure/local/serve-model.sh # Gemma 4 via llama.cpp on :8000 (OpenAI API at /v1)
infrastructure/local/status.sh     # health check (incl. model)
infrastructure/local/stop.sh       # stop all (data preserved)

source infrastructure/local/local.env   # export MONGO_URI / CHROMA_URL / REDIS_URL
```

Full from-scratch / reinstall instructions and the **vLLM-vs-CUDA-12.8 gotcha**
(why the model runs on llama.cpp, not vLLM) are in [`INSTALL.md`](./INSTALL.md).

## What differs from the Docker stack

| | Docker (`../docker/`) | Local (this) |
|-|-----------------------|--------------|
| MongoDB host | `mongodb:27017` | `localhost:27017` |
| Chroma port | `8000` | **`8765`** (`:8000`=host vLLM, `:8001`=RunPod proxy) |
| Redis host | `redis:6379` | `localhost:6379` |
| MongoDB mode | standalone (compose) | **single-node replica set `rs0`** |
| Process mgmt | Docker restart policy | host processes, pidfiles in `.data/pids` |

### MongoDB must be a replica set

The `StorageManager` outbox worker tails a MongoDB **change stream**
(`db.messages.watch()`), which only works on a replica set or sharded cluster —
not a standalone `mongod`. `start.sh` therefore launches `mongod --replSet rs0`
and runs `rs.initiate()` once.

> Note: the Docker compose currently starts a **standalone** `mongo:7` with no
> `--replSet`, so change streams would not work there either. When that stack is
> used on a real host, add `command: ["--replSet","rs0"]` (plus a one-time
> `rs.initiate()`) to the `mongodb` service.

## Data

All state lives under `<repo>/.data/` (gitignored):

```
.data/
  mongo/   redis/   chroma/        # databases
  logs/    mongod.log redis.log chroma.log
  pids/    mongod.pid redis.pid chroma.pid
```
