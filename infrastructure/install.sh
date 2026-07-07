#!/usr/bin/env bash
# install.sh — Reproducible, idempotent setup of the Labmate LOCAL stack on a
# RunPod-style pod that CANNOT run containers (no NET_ADMIN; namespaces blocked)
# and whose NVIDIA driver caps at CUDA 12.8.
#
# Re-runnable: every step is guarded, so re-running skips work already done.
#
# What it installs:
#   1. System packages   — Node.js 22 (NodeSource), ripgrep
#   2. Node deps         — services/mcp-bridge (npm)
#   3. Python deps       — services/memory + services/mcp-bridge requirements
#   4. Inference engine  — llama.cpp (CUDA build) + Gemma 4 GGUF download
#
# Local state is SQLite (co-located with services.local.main) — there is no
# MongoDB, Redis, or Chroma to install/provision here anymore.
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
#   infrastructure/install.sh                 # everything (core + skills + model)
#   infrastructure/install.sh --no-model      # skip the 18GB GGUF download
#   infrastructure/install.sh --no-skills     # skip per-skill deps (core only)
#   infrastructure/install.sh --server-only   # RunPod/GPU box: ONLY the model server
#   infrastructure/install.sh --client-only   # harness/client box: everything EXCEPT the engine
#   infrastructure/install.sh --searxng-docker  # force the official searxng/searxng image
#   infrastructure/install.sh --searxng-native  # force the native (Linux/apt) build
#
# SearXNG runtime defaults by OS (SEARXNG_MODE=auto): macOS -> docker (the native
# apt build does not apply there; Docker is auto-installed via Homebrew colima if
# absent), Linux -> native (unchanged RunPod path). Override with the flags above.
#
# --server-only installs JUST the inference engine (llama.cpp CUDA build + GGUF)
# — nothing else. On the split topology the GPU box runs ONLY llama-server (:8000);
# the harness (Node/mcp-bridge, orchestrator/ws_gateway/cli Python deps, skills,
# SearXNG, tokenizer, frontend) runs on the CLIENT (your Mac), which points at this
# box via GEMMA_BASE. So --server-only skips every client-only section and does only
# GPU-detect (§0) + llama.cpp + GGUF (§4). Combine with --no-model to build the
# engine but bring your own weights.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Build/install steps below redirect logs into .data/logs — make sure it exists
# before the first write (the llama.cpp build redirect fails on a missing dir).
mkdir -p "${REPO_ROOT}/.data/logs" "${REPO_ROOT}/.data/pids"

# ─── Tunables (override via env) ──────────────────────────────────────────────
export HF_HOME="${HF_HOME:-/workspace/.hf-cache}"
LLAMA_DIR="${LLAMA_DIR:-/workspace/llama.cpp}"
# Served model — keep in sync with local.env's MODEL (serve-model.sh serves that path).
# The 12B UD-Q4_K_XL is the default (single-GPU; ~2x faster than the 31B and fits its full
# 262144 ctx on a 48 GB card). A Q6_K A/B on c2 showed no measured gain for ~50% more latency,
# so Q4 stays the default. Override GGUF_REPO/MODEL_DIR/GGUF_FILE via env for a different model
# or quant; the download step verifies the file exists in the repo and lists the quants if not.
MODEL_DIR="${MODEL_DIR:-/workspace/models/gemma-4-12b-gguf}"
GGUF_REPO="${GGUF_REPO:-unsloth/gemma-4-12b-it-GGUF}"   # verify matches your HF source; env-overridable
GGUF_FILE="${GGUF_FILE:-gemma-4-12b-it-UD-Q4_K_XL.gguf}"

# Tokenizer for the ast-repo-map skill's token budgeting. We download only the
# tokenizer FILES of the served model (not the weights), so it costs no VRAM and
# ~31MB on disk. The skill loads it from this dir via REPO_MAP_TOKENIZER (local.env).
# 12B & 31B share the gemma-4 tokenizer, so the (identical) tokenizer files still apply.
# PIN the dir to the path local.env's REPO_MAP_TOKENIZER already points at — independent of
# MODEL_DIR — so swapping the GGUF to the 12B does not move the tokenizer out from under it.
TOKENIZER_REPO="${TOKENIZER_REPO:-google/gemma-4-31B-it}"
TOKENIZER_DIR="${TOKENIZER_DIR:-/workspace/models/gemma-4-gguf/tokenizer}"

# SearXNG (metasearch for the web-search skill). Two runtimes:
#   native — clone + venv + build (Linux/apt only; the original RunPod path).
#   docker — the official searxng/searxng image (cross-platform; the only path
#            that works on macOS, where the native apt build does not apply).
# Mode is resolved below from --searxng-docker/--searxng-native or, in `auto`,
# from the OS: macOS -> docker, Linux -> native (RunPod behavior unchanged).
SEARXNG_DIR="${SEARXNG_DIR:-/workspace/searxng}"
SEARXNG_PORT="${SEARXNG_PORT:-8080}"
SEARXNG_MODE="${SEARXNG_MODE:-auto}"       # auto | docker | native
SEARXNG_IMAGE="${SEARXNG_IMAGE:-searxng/searxng}"

SKIP_MODEL=false
SKIP_SKILLS=false
SKIP_SEARXNG=false
SERVER_ONLY=false
CLIENT_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --no-model)       SKIP_MODEL=true ;;
    --no-skills)      SKIP_SKILLS=true ;;
    --no-searxng)     SKIP_SEARXNG=true ;;
    --server-only)    SERVER_ONLY=true ;;
    --client-only)    CLIENT_ONLY=true ;;
    --searxng-docker) SEARXNG_MODE=docker ;;
    --searxng-native) SEARXNG_MODE=native ;;
  esac
done

# ─── Platform detection ───────────────────────────────────────────────────────
case "$(uname -s)" in
  Darwin) PLATFORM=mac ;;
  Linux)  PLATFORM=linux ;;
  *)      PLATFORM=other ;;
esac

# Resolve SEARXNG_MODE=auto → docker on macOS (no native apt path), native on
# Linux (unchanged RunPod behavior). `other` (e.g. Git-Bash/WSL) → docker.
if [[ "$SEARXNG_MODE" == "auto" ]]; then
  case "$PLATFORM" in
    mac|other) SEARXNG_MODE=docker ;;
    linux)     SEARXNG_MODE=native ;;
  esac
fi

# --server-only ⇒ only the inference engine. Force-skip the client-only sections
# that have their own skip flags; the remaining client-only sections (§1 node/rg,
# §2 mcp-bridge, §3 python deps, §4b tokenizer, §6 frontend) are guarded on
# $SERVER_ONLY inline below.
if $SERVER_ONLY; then
  SKIP_SKILLS=true
  SKIP_SEARXNG=true
fi

# --client-only ⇒ the harness/client side, no inference engine (the model is a
# REMOTE box reached via GEMMA_BASE). Installs node/mcp-bridge, core+skill Python
# deps, tokenizer, SearXNG, frontend — and SKIPS the llama.cpp build + GGUF
# download (§4). It is the inverse of --server-only; the two are mutually
# exclusive. §4 is guarded on $CLIENT_ONLY inline below.
if $SERVER_ONLY && $CLIENT_ONLY; then
  log "ERROR: --server-only and --client-only are mutually exclusive."
  exit 2
fi

log()  { echo "[install] $*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ensure_docker — make a usable Docker daemon available, auto-installing where it
# is SAFE and non-invasive (Q2 policy), else guiding. Returns 0 if `docker info`
# works afterward, 1 otherwise (caller degrades gracefully — web-search off).
#   macOS: if docker is missing, install colima + the docker CLI via Homebrew (a
#          headless VM runtime — no GUI, no license prompt) and `colima start`.
#          A pre-existing Docker Desktop is used as-is; we never install it.
#   Linux: use the official get.docker.com convenience script (root/sudo).
#   other (Git-Bash/WSL): detect only — point at Docker Desktop + WSL2.
ensure_docker() {
  if have docker && docker info >/dev/null 2>&1; then return 0; fi
  # docker CLI present but daemon down → try to bring up colima (mac), else guide.
  if have docker && ! docker info >/dev/null 2>&1; then
    if have colima; then log "docker CLI present, daemon down — 'colima start' ..."; colima start >/dev/null 2>&1 || true; fi
    docker info >/dev/null 2>&1 && return 0
    log "  WARN: docker installed but daemon unreachable — start Docker Desktop (or 'colima start') and re-run."
    return 1
  fi
  # docker missing entirely.
  case "$PLATFORM" in
    mac)
      if have brew; then
        log "docker not found — installing colima + docker CLI via Homebrew (headless, no GUI) ..."
        brew install colima docker >/dev/null 2>&1 || { log "  WARN: 'brew install colima docker' failed — install Docker Desktop manually."; return 1; }
        log "starting colima VM (first start pulls a small Linux image) ..."
        colima start >/dev/null 2>&1 || { log "  WARN: 'colima start' failed — run it manually, then re-run install."; return 1; }
        docker info >/dev/null 2>&1 && { log "docker ready via colima."; return 0; }
        log "  WARN: docker still not reachable after colima start."; return 1
      fi
      log "  Docker not found and Homebrew missing. Install Homebrew (https://brew.sh) then re-run,"
      log "  or install Docker Desktop (https://docker.com/products/docker-desktop) and re-run."
      return 1 ;;
    linux)
      log "docker not found — installing via get.docker.com convenience script ..."
      curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 || { log "  WARN: Docker convenience-script install failed — install Docker manually."; return 1; }
      docker info >/dev/null 2>&1 && return 0
      log "  WARN: docker installed but daemon not running — 'sudo systemctl start docker' then re-run."
      return 1 ;;
    *)
      log "  Docker not found. On Windows, install Docker Desktop + enable WSL2, then run this inside WSL."
      return 1 ;;
  esac
}

# ─── 0. Detect GPU / CUDA ─────────────────────────────────────────────────────
if have nvidia-smi; then
  DRIVER_CUDA="$(nvidia-smi | sed -n 's/.*CUDA Version: \([0-9.]*\).*/\1/p' | head -1)"
  GPU_CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')"
  log "GPU detected: compute_cap=${GPU_CC:-?} driver_cuda=${DRIVER_CUDA:-?}"
else
  GPU_CC=""; DRIVER_CUDA=""
  log "WARNING: nvidia-smi not found — inference build will be CPU-only."
fi

# ─── 1. System packages (CLIENT-ONLY: node runs mcp-bridge, rg backs search_files) ──
export DEBIAN_FRONTEND=noninteractive
if $SERVER_ONLY; then
  log "server-only: skipping node + ripgrep (client-only; the model box needs neither)."
else
  log "system packages ..."
  if ! have node; then
    log "installing Node.js 22 (NodeSource) ..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
    apt-get install -y nodejs >/dev/null
  fi
  log "node $(node --version)"

  if ! have rg; then
    log "installing ripgrep (used by search_files; Python fallback exists) ..."
    if have apt-get; then
      apt-get install -y ripgrep >/dev/null
    elif have brew; then
      brew install ripgrep >/dev/null
    else
      log "  WARN: no apt-get/brew found — install ripgrep manually (https://github.com/BurntSushi/ripgrep#installation)"
    fi
  fi
  have rg && log "ripgrep $(rg --version | head -1)" || log "  WARN: rg still not on PATH — search_files will fall back to its Python implementation"
fi

# ─── 2. Node deps + build (mcp-bridge) ────────────────────────────────────────
# dist/ is gitignored (build output), so a fresh clone has NO dist/index.js until
# it is compiled here. The orchestrator spawns the bridge from dist/index.js, so
# it must exist before the stack runs. (start.sh also rebuilds a missing/stale
# dist as a safety net, but building at install time surfaces TS errors during
# setup instead of as an opaque "MCP bridge did not become ready" at first start.)
if $SERVER_ONLY; then
  log "server-only: skipping mcp-bridge (client-only)."
elif [[ -f "${REPO_ROOT}/services/mcp-bridge/package.json" ]]; then
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
# PIP is also used by later sections (§4 huggingface-hub, §4b tokenizer), so define
# it before the server-only short-circuit.
PIP="pip install --break-system-packages -q"
if $SERVER_ONLY; then
  log "server-only: skipping core-service python deps (client-only)."
else
  log "python deps (core services) ..."
  # Auto-discover every top-level service's requirements.txt (services/<name>/requirements.txt)
  # rather than hardcoding a list — a hardcoded list silently misses newly-added services
  # (e.g. a future service dir), whose absence only surfaces later as a runtime
  # ModuleNotFoundError. The glob matches DIRECT children only, so per-skill deps
  # (services/skills/<name>/, handled in §3b) are intentionally NOT matched.
  for req in "${REPO_ROOT}"/services/*/requirements.txt; do
    [[ -f "$req" ]] || continue
    svc="$(basename "$(dirname "$req")")"
    log "  pip: services/${svc}"
    $PIP -r "$req"
  done
  # NOTE: We deliberately do NOT install vllm here (CUDA-13 incompatibility above).
fi

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
# CLIENT-ONLY: skipped entirely — the model runs on a REMOTE box (GEMMA_BASE), so
# the client needs neither the llama.cpp build nor the GGUF weights.
LLAMA_SERVER="${LLAMA_DIR}/build/bin/llama-server"
if $CLIENT_ONLY; then
  log "client-only: skipping llama.cpp build + GGUF (model is remote via GEMMA_BASE)."
elif [[ -x "$LLAMA_SERVER" ]]; then
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

if $CLIENT_ONLY || $SKIP_MODEL; then
  $CLIENT_ONLY || log "skipping model download (--no-model)."
else
  if [[ -f "${MODEL_DIR}/${GGUF_FILE}" ]]; then
    log "GGUF already present: ${MODEL_DIR}/${GGUF_FILE}"
  else
    have hf || $PIP "huggingface-hub>=0.34"
    # Verify the requested GGUF actually exists in the repo BEFORE the (large) download,
    # so a bad filename fails fast and prints the quants that ARE published (e.g. if you
    # override GGUF_FILE to a quant the repo names differently, you'll see the real name).
    avail="$(python3 -c "from huggingface_hub import list_repo_files; print('\n'.join(f for f in list_repo_files('$GGUF_REPO') if f.endswith('.gguf')))" 2>/dev/null)" || avail=""
    if [[ -z "$avail" ]]; then
      log "WARN: could not list ${GGUF_REPO} (offline or repo missing) — attempting the download anyway."
    elif ! grep -qxF "$GGUF_FILE" <<<"$avail"; then
      log "ERROR: '${GGUF_FILE}' is not in ${GGUF_REPO}. Available GGUF quants:"
      echo "$avail" | sed 's/^/    /' >&2
      log "Re-run with GGUF_FILE=<exact file> from the list above (and set local.env's MODEL to match)."
      exit 1
    fi
    log "downloading ${GGUF_REPO}/${GGUF_FILE} ..."
    mkdir -p "$MODEL_DIR"
    hf download "$GGUF_REPO" "$GGUF_FILE" --local-dir "$MODEL_DIR"
    log "to SERVE this quant: export MODEL=${MODEL_DIR}/${GGUF_FILE} (or edit local.env's MODEL), then serve-model.sh"
  fi
fi

# ─── 4b. Tokenizer (ast-repo-map token budgeting) ─────────────────────────────
# The ast-repo-map skill counts tokens with the SERVED model's tokenizer to
# enforce its output budget (Gemma SentencePiece — never tiktoken). Only the
# tokenizer FILES are fetched (~31MB, CPU-only); the model weights are NOT
# downloaded, so this uses no VRAM. NOT gated by --no-model: it's tiny and the
# skill needs it even when you bring your own weights. The skill loads it from
# REPO_MAP_TOKENIZER (set in local.env) so no network is needed at runtime.
if $SERVER_ONLY; then
  log "server-only: skipping tokenizer (client-side ast-repo-map skill needs it, not the model box)."
elif [[ -f "${TOKENIZER_DIR}/tokenizer.json" ]]; then
  log "tokenizer already present: ${TOKENIZER_DIR}"
else
  have hf || $PIP "huggingface-hub>=0.34"
  log "downloading tokenizer ${TOKENIZER_REPO} (tokenizer files only, ~31MB) ..."
  mkdir -p "$TOKENIZER_DIR"
  hf download "$TOKENIZER_REPO" tokenizer.json tokenizer_config.json --local-dir "$TOKENIZER_DIR" \
    || log "  WARN: tokenizer download failed — ast-repo-map falls back to the HF id, then a char estimate"
fi

# ─── 5. SearXNG (metasearch for the web-search skill) ─────────────────────────
# The web-search skill calls SearXNG's JSON API (GET /search?format=json). It runs
# as an internal, single-agent instance with the abuse limiter DISABLED (no
# valkey/redis needed) and JSON output enabled. Runtime is $SEARXNG_MODE:
#   docker — the official ${SEARXNG_IMAGE} image (macOS default; cross-platform).
#   native — clone + venv + build (Linux/apt only; the original RunPod path).
# start.sh launches whichever mode was installed (recorded in ${SEARXNG_DIR}/.mode).
# Pass --no-searxng to skip this section.
SEARXNG_SETTINGS="${SEARXNG_DIR}/settings.yml"
SEARXNG_MODE_FILE="${SEARXNG_DIR}/.mode"

# write_searxng_settings <bind_address> <port> — emit the shared settings.yml.
# Native binds host loopback (127.0.0.1) on the host SEARXNG_PORT. The docker image
# always listens on 8080 INSIDE the container (start.sh maps host:SEARXNG_PORT ->
# container:8080), so docker passes bind 0.0.0.0 + port 8080 regardless of the host
# port — decoupling the host port from the container port.
write_searxng_settings() {
  local bind="$1" port="$2" secret
  secret="$(openssl rand -hex 32 2>/dev/null || echo labmate-dev-secret)"
  mkdir -p "$SEARXNG_DIR"
  cat > "$SEARXNG_SETTINGS" <<YAML
# Labmate SearXNG settings — internal instance for the web-search skill.
# JSON output is REQUIRED by the skill (GET /search?format=json). The limiter is
# disabled because this instance is private, so no valkey/redis backend is needed.
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
  bind_address: "${bind}"
  port: ${port}
  secret_key: "${secret}"
  limiter: false
  image_proxy: false
YAML
}

if $SKIP_SEARXNG; then
  log "skipping SearXNG (--no-searxng)."
elif [[ "$SEARXNG_MODE" == "docker" ]]; then
  log "SearXNG runtime: docker (${SEARXNG_IMAGE})"
  if ensure_docker; then
    log "pulling ${SEARXNG_IMAGE} ..."
    if docker pull "$SEARXNG_IMAGE" >/dev/null 2>&1; then
      [[ -f "$SEARXNG_SETTINGS" ]] && log "SearXNG settings already present: ${SEARXNG_SETTINGS}" \
        || { log "writing SearXNG settings: ${SEARXNG_SETTINGS}"; write_searxng_settings "0.0.0.0" "8080"; }
      echo docker > "$SEARXNG_MODE_FILE"
      log "SearXNG (docker) ready — start.sh will 'docker run' it on :${SEARXNG_PORT}."
    else
      log "  WARN: 'docker pull ${SEARXNG_IMAGE}' failed — web-search skill will be unavailable."
    fi
  else
    log "  WARN: Docker unavailable — skipping SearXNG (web-search skill will be unavailable; see guidance above)."
  fi
else
  # native (Linux/apt) — the original RunPod path.
  SEARXNG_SRC="${SEARXNG_DIR}/searxng-src"
  SEARXNG_VENV="${SEARXNG_DIR}/searx-pyenv"
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
  [[ -f "$SEARXNG_SETTINGS" ]] && log "SearXNG settings already present: ${SEARXNG_SETTINGS}" \
    || { log "writing SearXNG settings: ${SEARXNG_SETTINGS}"; write_searxng_settings "127.0.0.1" "${SEARXNG_PORT}"; }
  echo native > "$SEARXNG_MODE_FILE"
fi


# ─── 6. Frontend config (optional — only if the frontend is checked out here) ──
# config.ts is gitignored (personal gateway URL); provision it from the committed
# template so a local build/typecheck works without manual setup. If the frontend
# isn't present, this is a no-op — its own predev/prebuild scripts do this too.
if $SERVER_ONLY; then
  : # server-only: no frontend on the model box
elif [[ -f "${FRONTEND_DIR:="${REPO_ROOT}/services/frontend"}/src/config.example.ts" && ! -f "${FRONTEND_DIR}/src/config.ts" ]]; then
  log "frontend: provisioning src/config.ts from config.example.ts (local default: ws://localhost:8787/ws)"
  cp "${FRONTEND_DIR}/src/config.example.ts" "${FRONTEND_DIR}/src/config.ts"
fi

if $SERVER_ONLY; then
  log "DONE (server-only). This box runs ONLY the model server. Next:"
  log "  infrastructure/serve-model.sh   # Gemma 4 via llama.cpp on :8000"
  log "  curl -s http://localhost:8000/health   # → {\"status\":\"ok\"} once loaded"
  log ""
  log "Then, on your CLIENT (Mac) harness, point GEMMA_BASE at THIS box:"
  log "  GEMMA_BASE=http://<this-host>:8000/v1  (the harness's sole remote dependency)."
  log "  (Expose port 8000 / use the RunPod TCP proxy so the Mac can reach it.)"
elif $CLIENT_ONLY; then
  log "DONE (client-only). This box runs the harness; the model is REMOTE. Next:"
  log "  export GEMMA_BASE=\"https://<pod-id>-8000.proxy.runpod.net/v1\"  # your model box"
  log "  #   (or edit infrastructure/local.env — it respects a pre-set GEMMA_BASE)"
  log "  infrastructure/start.sh         # services.local.main (single process) + SearXNG"
  log "  infrastructure/status.sh        # gateway + SearXNG green"
  log ""
  log "  If :8787 is taken on this host, run with LOCAL_PORT=<free> and connect the"
  log "  CLI/frontend at ws://localhost:<free>/ws."
  log ""
  log "Admin: ADMIN_EMAIL/ADMIN_PASSWORD (dev defaults in local.env) auto-seed on"
  log "  first boot. Single-user mode (default) auto-auths the CLI/frontend — no login."
else
  log "DONE. Next:"
  log "  infrastructure/start.sh         # services.local.main (single process) + SearXNG"
  log "  infrastructure/serve-model.sh   # Gemma 4 via llama.cpp on :8000"
  log "  source infrastructure/local.env # export connection URLs (incl. SEARXNG_URL)"
  log ""
  log "Set GEMMA_BASE in local.env to your model server. Local default:"
  log "  http://localhost:8000/v1 (run serve-model.sh on the same box). Remote GPU"
  log "  box: http://<host>:8000/v1 — this is the harness's sole remote dependency."
  log ""
  log "Admin login: set ADMIN_EMAIL / ADMIN_PASSWORD in infrastructure/local.env"
  log "  (dev defaults are already set there). The admin account is auto-seeded on"
  log "  services.local.main's FIRST boot — only when the auth store is empty. If"
  log "  ADMIN_PASSWORD is unset/empty, no admin is seeded and login is impossible."
  log "  Additional users (2nd/3rd account) or password rotation: run"
  log "  python -m services.ws_gateway.seed_user --email a@b.c --password ... [--role admin]"
  log "  (headless CLI, no running server needed; pass --reset-password to rotate)."
  log "  start.sh prints the admin email/password reminder once the harness is healthy."
fi
