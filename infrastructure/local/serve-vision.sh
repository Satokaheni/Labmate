#!/usr/bin/env bash
# Second llama-server for VISION, pinned to GPU 1 (3070 Ti). Text model on :8000
# (GPU 0) is untouched. Idempotent; waits until :8002/health is ready.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/local.env" 2>/dev/null || true

LLAMA_SERVER="${LLAMA_SERVER:-/workspace/llama.cpp/build/bin/llama-server}"
VISION_MODEL_GGUF="${VISION_MODEL_GGUF:-/workspace/models/gemma-3-vision-gguf/gemma-3-4b-it-Q4_K_M.gguf}"
VISION_MMPROJ="${VISION_MMPROJ:-/workspace/models/gemma-3-vision-gguf/mmproj-gemma-3-4b-it.gguf}"
VISION_PORT="${VISION_PORT:-8002}"
VISION_GPU="${VISION_GPU:-1}"
VISION_NGL="${VISION_NGL:-99}"
VISION_CTX="${VISION_CTX:-8192}"
LOGS="${LOGS:-$HERE/../../.data/logs}"; PIDS="${PIDS:-$HERE/../../.data/pids}"
mkdir -p "$LOGS" "$PIDS"
info(){ echo "[serve-vision] $*" >&2; }
fail(){ echo "[serve-vision] FAIL: $*" >&2; exit 1; }
ready(){ curl -fsS "http://localhost:${VISION_PORT}/health" >/dev/null 2>&1; }

if ready; then info "vision llama-server already serving on :${VISION_PORT}"; exit 0; fi
[[ -x "$LLAMA_SERVER" ]] || fail "llama-server not built at $LLAMA_SERVER"
[[ -f "$VISION_MODEL_GGUF" ]] || fail "vision GGUF not found at $VISION_MODEL_GGUF — run install.sh"
[[ -f "$VISION_MMPROJ" ]] || fail "mmproj not found at $VISION_MMPROJ — run install.sh"

info "launching vision llama-server: ${VISION_MODEL_GGUF##*/} on :${VISION_PORT} (GPU ${VISION_GPU}, ctx=${VISION_CTX}) ..."
CUDA_VISIBLE_DEVICES="${VISION_GPU}" "$LLAMA_SERVER" \
  -m "$VISION_MODEL_GGUF" \
  --mmproj "$VISION_MMPROJ" \
  --port "$VISION_PORT" \
  --ctx-size "$VISION_CTX" \
  --n-gpu-layers "$VISION_NGL" \
  --alias gemma-3-vision \
  >"$LOGS/llama-vision.log" 2>&1 &
echo $! > "$PIDS/llama-vision.pid"
info "vision llama-server pid $(cat "$PIDS/llama-vision.pid") — logs: $LOGS/llama-vision.log"

for _ in $(seq 1 120); do
  if ready; then info "vision ready on :${VISION_PORT}"; exit 0; fi
  if ! kill -0 "$(cat "$PIDS/llama-vision.pid")" 2>/dev/null; then
    echo "[serve-vision] FAIL: vision llama-server exited — last log lines:" >&2
    tail -25 "$LOGS/llama-vision.log" >&2; exit 1
  fi
  sleep 2
done
fail "vision model not ready after timeout — see $LOGS/llama-vision.log"
