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
#   infrastructure/local/install.sh             # everything (core + skills + model)
#   infrastructure/local/install.sh --no-model  # skip the 18GB GGUF download
#   infrastructure/local/install.sh --no-skills # skip per-skill deps (core only)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Build/install steps below redirect logs into .data/logs — make sure it exists
# before the first write (the llama.cpp build redirect fails on a missing dir).
mkdir -p "${REPO_ROOT}/.data/logs" "${REPO_ROOT}/.data/pids"

# ─── Tunables (override via env) ──────────────────────────────────────────────
export HF_HOME="${HF_HOME:-/workspace/.hf-cache}"
LLAMA_DIR="${LLAMA_DIR:-/workspace/llama.cpp}"
MODEL_DIR="${MODEL_DIR:-/workspace/models/gemma-4-gguf}"
GGUF_REPO="${GGUF_REPO:-unsloth/gemma-4-31B-it-GGUF}"
GGUF_FILE="${GGUF_FILE:-gemma-4-31B-it-UD-Q4_K_XL.gguf}"

# SearXNG (native metasearch for the web-search skill).
SEARXNG_DIR="${SEARXNG_DIR:-/workspace/searxng}"
SEARXNG_PORT="${SEARXNG_PORT:-8080}"

SKIP_MODEL=false
SKIP_SKILLS=false
SKIP_SEARXNG=false
for arg in "$@"; do
  case "$arg" in
    --no-model)   SKIP_MODEL=true ;;
    --no-skills)  SKIP_SKILLS=true ;;
    --no-searxng) SKIP_SEARXNG=true ;;
  esac
done

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

# ─── 2. Node deps + build (mcp-bridge) ────────────────────────────────────────
# dist/ is gitignored (build output), so a fresh clone has NO dist/index.js until
# it is compiled here. The orchestrator spawns the bridge from dist/index.js, so
# it must exist before the stack runs. (start.sh also rebuilds a missing/stale
# dist as a safety net, but building at install time surfaces TS errors during
# setup instead of as an opaque "MCP bridge did not become ready" at first start.)
if [[ -f "${REPO_ROOT}/services/mcp-bridge/package.json" ]]; then
  log "npm install + build (mcp-bridge) ..."
  ( cd "${REPO_ROOT}/services/mcp-bridge" \
      && npm install --no-audit --no-fund >/dev/null 2>&1 \
      && npm run build >"${REPO_ROOT}/.data/logs/mcp-bridge-build.log" 2>&1 ) \
    || log "  WARN: mcp-bridge install/build failed — see .data/logs/mcp-bridge-build.log (start.sh will retry)"
fi

# ─── 3. Python deps ───────────────────────────────────────────────────────────
# System Python is PEP-668 externally-managed → --break-system-packages.
# Install requirements for every core service that runs in the local stack —
# not just memory + mcp-bridge. Missing any of these makes start.sh fail at
# runtime (e.g. skill-worker dies with ModuleNotFoundError: 'frontmatter').
log "python deps (core services) ..."
PIP="pip install --break-system-packages -q"
# Auto-discover every top-level service's requirements.txt (services/<name>/requirements.txt)
# rather than hardcoding a list — a hardcoded list silently misses newly-added services
# (e.g. codegraph_embedder), whose absence only surfaces later as a runtime
# ModuleNotFoundError. The glob matches DIRECT children only, so per-skill deps
# (services/skills/<name>/, handled in §3b) and the DEFERRED Discord connector
# (services/connectors/deferred/) are intentionally NOT matched.
for req in "${REPO_ROOT}"/services/*/requirements.txt; do
  [[ -f "$req" ]] || continue
  svc="$(basename "$(dirname "$req")")"
  log "  pip: services/${svc}"
  $PIP -r "$req"
done
# NOTE: We deliberately do NOT install vllm here (CUDA-13 incompatibility above).

# ─── 3b. Skill dependencies (Python requirements + TypeScript build) ───────────
# Each skill in services/skills/<name> is an independent MCP server with its own
# deps. The skill-worker spawns every skill that has a SKILL.md; a skill whose
# deps are missing fails to register and is silently skipped. Install them ALL so
# the full skill set is available for e2e. A single skill's install failing must
# not abort the whole run, so each is guarded with `|| log WARN`.
# Pass --no-skills to skip this section (faster core-only setup).
if [[ "${SKIP_SKILLS:-false}" == "true" ]]; then
  log "skipping skill deps (--no-skills)."
else
  log "skill deps (python + typescript) ..."
  for d in "${REPO_ROOT}"/services/skills/*/; do
    name="$(basename "$d")"
    if [[ -f "$d/requirements.txt" ]]; then
      log "  pip: skills/${name}"
      $PIP -r "$d/requirements.txt" || log "  WARN: pip failed for skills/${name} — skill will be skipped at runtime"
    fi
    if [[ -f "$d/package.json" ]]; then
      log "  npm: skills/${name}"
      ( cd "$d" && npm install --no-audit --no-fund >/dev/null 2>&1 \
          && npm run build --if-present >/dev/null 2>&1 ) \
        || log "  WARN: npm install/build failed for skills/${name} — skill will be skipped at runtime"
    fi
  done
fi

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

# ─── 5. SearXNG (native metasearch for the web-search skill) ──────────────────
# The web-search skill calls a SearXNG instance's JSON API (GET /search?format=json).
# We run SearXNG NATIVELY here (this pod cannot run Docker — see the WHY note above).
# The public-abuse limiter is DISABLED (this is an internal, single-agent instance),
# so SearXNG needs no valkey/redis backend. JSON output is enabled (required by the skill).
# The Docker stack runs SearXNG as the `lm-searxng` container instead — see
# infrastructure/docker/docker-compose.yml + infrastructure/docker/searxng-config/settings.yml.
# Pass --no-searxng to skip this section.
if $SKIP_SEARXNG; then
  log "skipping SearXNG (--no-searxng)."
else
  SEARXNG_SRC="${SEARXNG_DIR}/searxng-src"
  SEARXNG_VENV="${SEARXNG_DIR}/searx-pyenv"
  SEARXNG_SETTINGS="${SEARXNG_DIR}/settings.yml"
  if [[ -x "${SEARXNG_VENV}/bin/python" && -d "${SEARXNG_SRC}/searx" ]]; then
    log "SearXNG already installed: ${SEARXNG_SRC}"
  else
    log "installing SearXNG (native, into ${SEARXNG_DIR}) ..."
    apt-get install -y python3-dev python3-babel python3-venv build-essential \
      libxslt1-dev zlib1g-dev libffi-dev libssl-dev git >/dev/null 2>&1 \
      || log "  WARN: some SearXNG apt deps failed to install"
    mkdir -p "$SEARXNG_DIR"
    [[ -d "$SEARXNG_SRC/.git" ]] || git clone --depth 1 https://github.com/searxng/searxng "$SEARXNG_SRC" >/dev/null 2>&1
    [[ -x "${SEARXNG_VENV}/bin/python" ]] || python3 -m venv "$SEARXNG_VENV"
    "${SEARXNG_VENV}/bin/pip" install -U pip setuptools wheel pyyaml msgspec typing-extensions pybind11 >/dev/null 2>&1
    if ( cd "$SEARXNG_SRC" && "${SEARXNG_VENV}/bin/pip" install --use-pep517 --no-build-isolation -e . \
          >"${REPO_ROOT}/.data/logs/searxng-build.log" 2>&1 ); then
      log "SearXNG installed."
    else
      log "  WARN: SearXNG pip install FAILED — see .data/logs/searxng-build.log (web-search skill will be unavailable)"
    fi
  fi
  # Settings: enable JSON format (required by the skill); disable the limiter (internal use).
  if [[ ! -f "$SEARXNG_SETTINGS" ]]; then
    log "writing SearXNG settings: ${SEARXNG_SETTINGS}"
    SEARXNG_SECRET="$(openssl rand -hex 32 2>/dev/null || echo labmate-dev-secret)"
    cat > "$SEARXNG_SETTINGS" <<YAML
# Labmate local SearXNG settings — internal instance for the web-search skill.
# JSON output is REQUIRED by the skill (GET /search?format=json). The limiter is
# disabled because this instance is private (loopback only), so no valkey/redis is needed.
use_default_settings: true
general:
  debug: false
  instance_name: "labmate-searxng"
search:
  safe_search: 0
  autocomplete: "duckduckgo"
  formats:
    - html
    - json
server:
  bind_address: "127.0.0.1"
  port: ${SEARXNG_PORT}
  secret_key: "${SEARXNG_SECRET}"
  limiter: false
  image_proxy: false
YAML
  else
    log "SearXNG settings already present: ${SEARXNG_SETTINGS}"
  fi
fi

log "DONE. Next:"
log "  infrastructure/local/start.sh         # Mongo(rs0) + Redis + Chroma + SearXNG"
log "  infrastructure/local/serve-model.sh   # Gemma 4 via llama.cpp on :8000"
log "  source infrastructure/local/local.env # export connection URLs (incl. SEARXNG_URL)"
