#!/usr/bin/env bash
# install.sh — Reproducible, idempotent setup of the Labmate LOCAL stack on a
# RunPod-style pod that CANNOT run containers (no NET_ADMIN; namespaces blocked)
# and whose NVIDIA driver caps at CUDA 12.8.
#
# Re-runnable: every step is guarded, so re-running skips work already done.
#
# What it installs:
#   1. System packages   — Node.js 22 (NodeSource), MongoDB 8, Redis
#   2. Node deps         — services/mcp-bridge (npm)
#   3. Python deps       — services/memory + services/mcp-bridge requirements
#   4. Inference engine  — llama.cpp (CUDA build) + Gemma 4 GGUF download
#
# WHY llama.cpp AND NOT vLLM (read this before "fixing" it):
#   This pod's driver is CUDA 12.8 (570.x). Every vLLM build that has the
#   `gemma4` tool/reasoning parser is compiled for CUDA 13 (needs libcudart.so.13
#   / driver >= 580) and crashes here with "NVIDIA driver too old (found 12080)".
#   llama.cpp compiles against the local CUDA 12.8 toolkit and serves the same
#   OpenAI-compatible API. If this pod is ever recreated with a >=580 driver,
#   vLLM becomes viable again (preferred for batching) — see INSTALL.md.
#
# Usage:
#   infrastructure/local/install.sh            # everything
#   infrastructure/local/install.sh --no-model # skip the 18GB GGUF download
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ─── Tunables (override via env) ──────────────────────────────────────────────
export HF_HOME="${HF_HOME:-/workspace/.hf-cache}"
LLAMA_DIR="${LLAMA_DIR:-/workspace/llama.cpp}"
MODEL_DIR="${MODEL_DIR:-/workspace/models/gemma-4-gguf}"
GGUF_REPO="${GGUF_REPO:-unsloth/gemma-4-31B-it-GGUF}"
GGUF_FILE="${GGUF_FILE:-gemma-4-31B-it-UD-Q4_K_XL.gguf}"

SKIP_MODEL=false
[[ "${1:-}" == "--no-model" ]] && SKIP_MODEL=true

log()  { echo "[install] $*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ─── 0. Detect GPU / CUDA ─────────────────────────────────────────────────────
if have nvidia-smi; then
  DRIVER_CUDA="$(nvidia-smi | sed -n 's/.*CUDA Version: \([0-9.]*\).*/\1/p' | head -1)"
  GPU_CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')"
  log "GPU detected: compute_cap=${GPU_CC:-?} driver_cuda=${DRIVER_CUDA:-?}"
else
  GPU_CC=""; DRIVER_CUDA=""
  log "WARNING: nvidia-smi not found — inference build will be CPU-only."
fi

# ─── 1. System packages ───────────────────────────────────────────────────────
log "system packages ..."
export DEBIAN_FRONTEND=noninteractive

if ! have node; then
  log "installing Node.js 22 (NodeSource) ..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
  apt-get install -y nodejs >/dev/null
fi
log "node $(node --version)"

if ! have mongod; then
  log "installing MongoDB 8.0 ..."
  curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-8.0.gpg 2>/dev/null
  echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" \
    > /etc/apt/sources.list.d/mongodb-org-8.0.list
  apt-get update -qq
  apt-get install -y mongodb-org-server mongodb-mongosh >/dev/null
fi
log "mongod $(mongod --version | head -1 | awk '{print $3}')"

if ! have redis-server; then
  log "installing Redis ..."
  apt-get install -y redis-server >/dev/null
fi
log "redis $(redis-server --version | grep -oE 'v=[0-9.]+')"

# ─── 2. Node deps (mcp-bridge) ────────────────────────────────────────────────
if [[ -f "${REPO_ROOT}/services/mcp-bridge/package.json" ]]; then
  log "npm install (mcp-bridge) ..."
  ( cd "${REPO_ROOT}/services/mcp-bridge" && npm install --no-audit --no-fund >/dev/null 2>&1 )
fi

# ─── 3. Python deps ───────────────────────────────────────────────────────────
# System Python is PEP-668 externally-managed → --break-system-packages.
log "python deps (memory + mcp-bridge) ..."
PIP="pip install --break-system-packages -q"
$PIP -r "${REPO_ROOT}/services/memory/requirements.txt"
$PIP -r "${REPO_ROOT}/services/mcp-bridge/requirements.txt"
# NOTE: We deliberately do NOT install vllm here (CUDA-13 incompatibility above).

# ─── 4. Inference engine: llama.cpp + GGUF ────────────────────────────────────
LLAMA_SERVER="${LLAMA_DIR}/build/bin/llama-server"
if [[ -x "$LLAMA_SERVER" ]]; then
  log "llama.cpp already built: $LLAMA_SERVER"
else
  have cmake || { apt-get install -y cmake build-essential >/dev/null; }
  log "building llama.cpp with CUDA (arch sm_${GPU_CC:-86}) — this takes ~10-15 min ..."
  rm -rf "$LLAMA_DIR"
  git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_DIR" >/dev/null 2>&1
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" \
    -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="${GPU_CC:-86}" -DLLAMA_CURL=OFF \
    >"${REPO_ROOT}/.data/logs/llama-build.log" 2>&1 || { log "cmake configure FAILED — see .data/logs/llama-build.log"; exit 1; }
  cmake --build "$LLAMA_DIR/build" --config Release -j"$(nproc)" --target llama-server \
    >>"${REPO_ROOT}/.data/logs/llama-build.log" 2>&1 || { log "build FAILED — see .data/logs/llama-build.log"; exit 1; }
  log "llama-server built."
fi

if $SKIP_MODEL; then
  log "skipping model download (--no-model)."
else
  if [[ -f "${MODEL_DIR}/${GGUF_FILE}" ]]; then
    log "GGUF already present: ${MODEL_DIR}/${GGUF_FILE}"
  else
    have hf || $PIP "huggingface-hub>=0.34"
    log "downloading ${GGUF_REPO}/${GGUF_FILE} (~18.8GB) ..."
    mkdir -p "$MODEL_DIR"
    hf download "$GGUF_REPO" "$GGUF_FILE" --local-dir "$MODEL_DIR"
  fi
fi

log "DONE. Next:"
log "  infrastructure/local/start.sh         # Mongo(rs0) + Redis + Chroma"
log "  infrastructure/local/serve-model.sh   # Gemma 4 via llama.cpp on :8000"
log "  source infrastructure/local/local.env # export connection URLs"
