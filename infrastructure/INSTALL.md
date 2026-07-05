# Labmate — Local Stack Install / Reinstall Guide

Everything needed to bring Labmate up from scratch on a host that **cannot run
containers** and whose **NVIDIA driver caps at CUDA 12.8** (this guide was written
against a RunPod-style pod, but nothing here requires RunPod specifically).

The runtime is two processes: `llama-server` (inference) and `services.local.main`
(gateway + orchestrator, one asyncio loop). All application state — sessions,
turns, auth, LangGraph checkpoints — lives in one SQLite database file. There is
no MongoDB, no Redis, no Chroma, and no Docker anywhere in this stack.

> **TL;DR:** `infrastructure/install.sh` does all of this idempotently.
> Then `serve-model.sh` (the model) and `start.sh` (the harness).

---

## One-command install

```bash
infrastructure/install.sh            # system + python + llama.cpp + 18.8GB GGUF
infrastructure/install.sh --no-model # skip the GGUF download
```

Re-running is safe — every step is guarded and skips work already done.

Then:

```bash
infrastructure/serve-model.sh   # Gemma 4 via llama.cpp on :8000
infrastructure/start.sh         # services.local.main (gateway + orchestrator, SQLite state)
infrastructure/status.sh        # health of both
source infrastructure/local.env # export LOCAL_HOST / LOCAL_PORT / GEMMA_BASE / etc.
```

---

## Quickstart

End-to-end, in order:

1. **Install:** `bash infrastructure/install.sh` — system + Python deps,
   MCP bridge build, frontend `config.ts` provisioning. Idempotent; safe to re-run.
2. **Configure:** edit `infrastructure/local.env` — set `GEMMA_BASE` to your
   model server (`http://localhost:8000/v1` if serving on this same box, or
   `http://<gpu-host>:8000/v1` for a remote GPU box — this is the harness's
   **sole remote dependency**), and set `ADMIN_EMAIL` / `ADMIN_PASSWORD` (these
   auto-seed the admin account on first boot).
3. **Start the model server:** `infrastructure/serve-model.sh` on the GPU
   box — wait until `/health` reports `"ok"` (see `status.sh` or curl it directly).
4. **Start the harness:** `bash infrastructure/start.sh` — launches
   `services.local.main` (gateway + orchestrator, one process) and prints the
   seeded admin login once healthy.
5. **Frontend:** defaults to `ws://localhost:8787/ws` for local use; log in with
   the seeded admin credentials from step 2.
6. **Add more users** (registration is closed — no signup UI): run the headless
   admin CLI, no running server required —
   ```bash
   python -m services.ws_gateway.seed_user --email teammate@example.com --password ... [--role admin]
   ```
   Pass `--reset-password` to rotate an existing user's password instead.

---

## What gets installed (and the gotchas)

### 1. System packages
| Package | Version | How |
|---|---|---|
| Node.js | 22 LTS | NodeSource (apt's default v18 is too old for the TS toolchain) |

MongoDB, Redis, and Chroma are **not installed** — the local-state-sqlite
rearchitecture removed them; all state is a SQLite file managed by
`services/orchestrator/local_store.py::LocalStore`.

### 2. Node deps
`npm install` in `services/mcp-bridge`.

### 3. Python deps
System Python is **PEP-668 externally-managed**, so installs use
`pip install --break-system-packages`. Requirements come from the service
`requirements.txt` files (`services/mcp-bridge/requirements.txt`, orchestrator/
gateway requirements, etc.) — see `install.sh` for the exact list.

### 4. Inference engine — **llama.cpp, NOT vLLM** ⚠️

**This is the big one.** Do not "fix" it back to vLLM on this pod.

- The pod's driver is **CUDA 12.8** (`nvidia-smi` → 570.x). It is host-level and
  cannot be upgraded from inside the pod.
- **Every vLLM build that has the `gemma4` tool/reasoning parser pins
  `torch==2.11.0` and ships only cu129/cu130 wheels.** vLLM's compiled
  `vllm._C` then needs `libcudart.so.13` (CUDA 13, driver ≥ 580) and crashes with:
  ```
  ImportError: libcudart.so.13: cannot open shared object file
  RuntimeError: The NVIDIA driver on your system is too old (found version 12080)
  ```
  Pinning `torch==2.11.0+cu128` makes torch itself work, but does **not** fix
  vLLM's cu13-compiled extension. There is no cu128 vLLM build with gemma4.
- **llama.cpp** compiles against the local CUDA 12.8 toolkit (`nvcc` present) and
  serves the same OpenAI-compatible API. It is the spec's documented single-box
  fallback (`spec_inference.md` §2.2).

**Build:** `cmake -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86` (A6000 = sm_86),
target `llama-server`. Output: `/workspace/llama.cpp/build/bin/llama-server`.

**Model:** GGUF from `unsloth/gemma-4-12b-it-GGUF`, quant **`UD-Q4_K_XL`**
(~7.4 GB, Unsloth *Dynamic* — higher accuracy than plain Q4_0/Q4_K per §8.5),
downloaded to `/workspace/models/gemma-4-12b-gguf/` (kept in sync with `local.env`'s
`MODEL`). The 12B fits its full 262144-token context on a 48 GB card. The download
**verifies the GGUF exists in the repo first** and, if not, prints the quants that *are*
published; override `GGUF_REPO`/`MODEL_DIR`/`GGUF_FILE` to serve a different model or quant.
(A Q6_K A/B on c2 showed no measured gain for ~50% more latency — Q4 stays the default.)

**Serve:** `llama-server -m <gguf> --jinja --n-gpu-layers 999 --ctx-size 16384`
on `127.0.0.1:8000`. `--jinja` uses the GGUF's embedded chat template, which
enables Gemma 4 tool calling. Served model name (`--alias`) = `gemma-4`.

> If the pod is ever recreated with a **≥ 580 driver (CUDA 13)**, vLLM becomes
> viable again and is preferred for continuous batching / multi-user throughput.
> Swap `serve-model.sh` back to a vLLM serve command in that case.

---

## Ports (this pod)

| Service | Port | Notes |
|---|---|---|
| Gemma 4 (llama.cpp) | 8000 | OpenAI API at `/v1`, health at `/health` |
| Local harness (`services.local.main`) | 8787 | gateway HTTP/WebSocket API; `LOCAL_HOST`/`LOCAL_PORT` |

No MongoDB, Redis, or Chroma ports — those services were retired; all
application state is a single SQLite file (see "Auth model" below and
`infrastructure/README.md` § State model).

## Data / logs

Under `<repo>/.data/` (gitignored): `labmate_state.sqlite` (SQLite state —
sessions, turns, `auth_users`, LangGraph checkpoints), `logs/` (`local.log`,
`llama-server.log`, `llama-build.log`), `pids/` (`local.pid`, `llama-server.pid`).
Model weights live outside the repo at `/workspace/models/` and the HF cache at
`/workspace/.hf-cache`.

---

## Auth model

Registration is **closed** — there is no signup UI or public registration endpoint.

- **Bootstrap admin:** on first boot, `services.local.main` auto-seeds a single
  admin account into the SQLite `auth_users` table from the `ADMIN_EMAIL` /
  `ADMIN_PASSWORD` environment variables (see `infrastructure/local.env`).
  **If `ADMIN_PASSWORD` is unset, login is impossible** — set it before first
  boot (the shipped default in `local.env` is a dev-only throwaway; override it
  for anything beyond local dev).
- **Additional users:** two ways to add the 2nd/3rd user or rotate a password:
  - **Headless admin CLI** (`services/ws_gateway/seed_user.py`, Piece 7c) — writes
    directly to the SQLite `auth_users` table, no running gateway required:
    ```bash
    python -m services.ws_gateway.seed_user --email teammate@example.com --password ... [--role admin]
    python -m services.ws_gateway.seed_user --email teammate@example.com --password newpw --reset-password
    ```
    Creating an email that already exists fails (exit 1) unless `--reset-password`
    is passed, in which case the password is updated instead.
  - **HTTP API** — `POST /auth/users` against a running gateway, authenticated
    with the admin's Bearer token (admin-only endpoint — see
    `services/ws_gateway/auth.py`):
    ```bash
    curl -X POST "http://localhost:8787/auth/users" \
      -H "Authorization: Bearer <admin JWT from /auth/login>" \
      -H "Content-Type: application/json" \
      -d '{"email": "teammate@example.com", "password": "..."}'
    ```
