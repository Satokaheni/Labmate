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
# --ctx-size is the TOTAL KV budget SHARED across --parallel slots: each request gets
# ctx-size / parallel tokens. 262144/2 = 131072 per request (= the model's n_ctx_train).
# With the 4-bit KV cache below (--cache-type-k/v q4_0) the KV footprint is ~1/4 of f16:
# this Gemma 4 31B (10 global layers @ 4 KV heads/head_dim 512 + 50 sliding-window layers
# @ window 1024) is ~3 GiB per 131072-token slot at q4_0, so 2 slots ≈ 6 GiB KV + 18.8 GiB
# weights + buffers ≈ ~27 GiB — fits the 48 GiB A6000 with wide margin (and a 32 GiB card).
# NOTE: PARALLEL must NOT raise per-request context — it DIVIDES it. Going back to 4 slots
# would quarter each request to 65536 (or 4096 at the old 16384 ctx). If you hit CUDA OOM,
# drop PARALLEL to 1 (a single 131072 slot) or halve CTX.
# SWA prefix-cache fix: Gemma 4 is a sliding-window-attention model (n_swa=1024). WITHOUT
# --swa-full, llama.cpp cannot restore a cached prefix across requests (the SWA layers' KV
# rolls past the window) → it force-re-evaluates the WHOLE prompt every call (cache_n stays 1),
# silently defeating the byte-stable-prefix design (the ~7k system+tools prefix is re-processed
# every turn). --swa-full keeps FULL KV for the ~50 SWA layers so cross-request prefix reuse
# works (the prefix is processed ONCE per session). Cost: full-SWA KV measures ~0.22 MiB/token
# on this build, so KV alone is CTX×0.22 MiB. MEASURED on the 48 GiB A6000: ctx 131072 needs
# ~28.8 GiB KV which + 18.8 GiB weights OOMs the card (cudaMalloc failed) — so the default here
# is a 65536 slot (~14.4 GiB KV, ~36 GiB total, boots with ~12 GiB free). Raise CTX only if
# `nvidia-smi` shows headroom; on a 32 GiB card drop to CTX=32768 (~7.2 GiB KV). Still ONE slot
# (PARALLEL=1). Set SWA_FULL=0 to revert to the windowed behavior (no cross-request cache reuse).
SWA_FULL="${SWA_FULL:-1}"
NGL="${NGL:-999}"          # offload all layers to GPU (A6000 48GB fits the 18.8GB model)
if [[ "$SWA_FULL" == "1" ]]; then
  CTX="${CTX:-65536}"      # one slot; full-SWA KV ~0.22 MiB/tok → ~14.4 GiB KV (131072 OOMs 48GB)
  PARALLEL="${PARALLEL:-1}"
  SWA_ARG=(--swa-full)
else
  CTX="${CTX:-262144}"     # windowed SWA = the OLD default (no cross-request cache reuse)
  PARALLEL="${PARALLEL:-2}"
  SWA_ARG=()
fi

LOGS="${REPO_ROOT}/.data/logs"; PIDS="${REPO_ROOT}/.data/pids"
mkdir -p "$LOGS" "$PIDS"

info() { echo "[serve-model] $*"; }
fail() { echo "[serve-model] FAIL: $*" >&2; exit 1; }

ready() { curl -fsS "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q '"status":"ok"'; }

if ready; then info "llama-server already serving on :${PORT}"; exit 0; fi
[[ -x "$LLAMA_SERVER" ]] || fail "llama-server not built at $LLAMA_SERVER — run install.sh"
[[ -f "$MODEL" ]]        || fail "GGUF not found at $MODEL — run install.sh"

info "launching llama-server: ${MODEL##*/} on :${PORT} (ctx=${CTX}, ngl=${NGL}, parallel=${PARALLEL}, swa_full=${SWA_FULL}) ..."
nohup "$LLAMA_SERVER" \
  -m "$MODEL" \
  --alias "$ALIAS" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --ctx-size "$CTX" \
  --n-gpu-layers "$NGL" \
  --parallel "$PARALLEL" \
  "${SWA_ARG[@]}" \
  --jinja \
  -fa 1 \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  --chat-template-kwargs '{"enable_thinking":false}' \
  --temp 1.0 \
  --top-p 0.95 \
  --top-k 64 \
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
