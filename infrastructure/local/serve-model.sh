#!/usr/bin/env bash
# serve-model.sh — Serve the Gemma 4 brain on the HOST via llama.cpp (never in Docker).
#
# We use llama.cpp (not vLLM) because this pod's driver caps at CUDA 12.8 and every
# gemma4-capable vLLM build is compiled for CUDA 13. See install.sh / INSTALL.md.
# llama-server exposes the same OpenAI-compatible API on :8000.
#
# Prereqs: run infrastructure/local/install.sh once (builds llama.cpp, downloads GGUF).
#
# Usage:
#   ./serve-model.sh            # start llama-server (idempotent), wait until ready
#   ./serve-model.sh --no-wait  # start and return immediately
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

LLAMA_SERVER="${LLAMA_SERVER:-/workspace/llama.cpp/build/bin/llama-server}"
MODEL="${MODEL:-/workspace/models/gemma-4-gguf/gemma-4-31B-it-UD-Q4_K_XL.gguf}"
ALIAS="${ALIAS:-gemma-4}"
PORT="${PORT:-8000}"
CTX="${CTX:-16384}"
NGL="${NGL:-999}"          # offload all layers to GPU (A6000 48GB fits the 18.8GB model)
PARALLEL="${PARALLEL:-4}"  # concurrent request slots (FIX 10: was 2; KV cache for 4 slots @ ctx 16384 fits 48GB GPU w/ 18GB model)

LOGS="${REPO_ROOT}/.data/logs"; PIDS="${REPO_ROOT}/.data/pids"
mkdir -p "$LOGS" "$PIDS"

info() { echo "[serve-model] $*"; }
fail() { echo "[serve-model] FAIL: $*" >&2; exit 1; }

ready() { curl -fsS "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q '"status":"ok"'; }

if ready; then info "llama-server already serving on :${PORT}"; exit 0; fi
[[ -x "$LLAMA_SERVER" ]] || fail "llama-server not built at $LLAMA_SERVER — run install.sh"
[[ -f "$MODEL" ]]        || fail "GGUF not found at $MODEL — run install.sh"

info "launching llama-server: ${MODEL##*/} on :${PORT} (ctx=${CTX}, ngl=${NGL}, --jinja) ..."
nohup "$LLAMA_SERVER" \
  -m "$MODEL" \
  --alias "$ALIAS" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --ctx-size "$CTX" \
  --n-gpu-layers "$NGL" \
  --parallel "$PARALLEL" \
  --jinja \
  -fa on \
  --reasoning-format deepseek \
  --reasoning-budget-message $'\n</think>\n' \
  >"$LOGS/llama-server.log" 2>&1 &
echo $! > "$PIDS/llama-server.pid"
info "llama-server pid $(cat "$PIDS/llama-server.pid") — logs: $LOGS/llama-server.log"

if [[ "${1:-}" == "--no-wait" ]]; then exit 0; fi

info "waiting for model to load into VRAM ..."
for i in $(seq 1 120); do   # up to ~10 min
  if ready; then info "READY — '${ALIAS}' serving on :${PORT} (OpenAI API at /v1)"; exit 0; fi
  if ! kill -0 "$(cat "$PIDS/llama-server.pid")" 2>/dev/null; then
    echo "[serve-model] FAIL: llama-server exited — last log lines:" >&2
    tail -25 "$LOGS/llama-server.log" >&2
    exit 1
  fi
  sleep 5
done
fail "model not ready after timeout — see $LOGS/llama-server.log"
