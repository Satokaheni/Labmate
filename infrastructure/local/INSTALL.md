# Labmate — Local Stack Install / Reinstall Guide

Everything needed to bring Labmate up from scratch on a fresh RunPod-style pod that
**cannot run containers** and whose **NVIDIA driver caps at CUDA 12.8**.

> **TL;DR:** `infrastructure/local/install.sh` does all of this idempotently.
> Then `start.sh` (data services) and `serve-model.sh` (the model).

---

## One-command install

```bash
infrastructure/local/install.sh            # system + python + llama.cpp + 18.8GB GGUF
infrastructure/local/install.sh --no-model # skip the GGUF download
```

Re-running is safe — every step is guarded and skips work already done.

Then:

```bash
infrastructure/local/start.sh         # MongoDB(rs0) + Redis + Chroma
infrastructure/local/serve-model.sh   # Gemma 4 via llama.cpp on :8000
infrastructure/local/status.sh        # health of all four
source infrastructure/local/local.env # export MONGO_URI / CHROMA_URL / REDIS_URL
```

---

## What gets installed (and the gotchas)

### 1. System packages
| Package | Version | How |
|---|---|---|
| Node.js | 22 LTS | NodeSource (apt's default v18 is too old for the TS toolchain) |
| MongoDB | 8.0 | official `mongodb-org-server` + `mongodb-mongosh` repo |
| Redis | 7.x | apt `redis-server` |

### 2. Node deps
`npm install` in `services/mcp-bridge`.

### 3. Python deps
System Python is **PEP-668 externally-managed**, so installs use
`pip install --break-system-packages`. Requirements:
`services/memory/requirements.txt` + `services/mcp-bridge/requirements.txt`.

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
> Swap `serve-model.sh` back to the vLLM command from `CLAUDE.md` Rule #6.

---

## Ports (this pod)

| Service | Port | Notes |
|---|---|---|
| Gemma 4 (llama.cpp) | 8000 | OpenAI API at `/v1`, health at `/health` |
| MongoDB (rs0) | 27017 | single-node replica set — change streams need it |
| Redis | 6379 | |
| Chroma | 8765 | `:8000`=model, `:8001`=RunPod nginx proxy — both taken |

## Data / logs

Under `<repo>/.data/` (gitignored): `mongo/ redis/ chroma/`, `logs/`
(`mongod.log`, `redis.log`, `chroma.log`, `llama-server.log`, `llama-build.log`),
`pids/`. Model weights live outside the repo at `/workspace/models/` and the HF
cache at `/workspace/.hf-cache`.
