#!/usr/bin/env bash
# start.sh — Start the Labmate local harness NATIVELY on the host (no Docker).
#
# Why this exists: this pod cannot run any container engine (no NET_ADMIN, and
# the unshare/clone namespace syscalls are blocked by seccomp). Docker/Podman
# both fail at network/namespace creation. So the harness runs as an ordinary
# host process here. The Docker path lives in ../docker/ for when Labmate runs
# on a privileged host / your own server.
#
# Runtime: a SINGLE co-located process (services.local.main — gateway +
# orchestrator, one asyncio loop) backed by local SQLite state. There is no
# MongoDB, Redis, or Chroma — those were retired when the runtime was unified
# onto SQLite (see docs/superpowers for the local-state-unification design).
#
# Data + logs + pidfiles live under <repo>/.data (gitignored).
#
# Usage:
#   ./start.sh           # start everything that isn't already running (idempotent)
#   ./start.sh --status  # alias for ./status.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DATA="${REPO_ROOT}/.data"
LOGS="${DATA}/logs"
PIDS="${DATA}/pids"
mkdir -p "$LOGS" "$PIDS"

info() { echo "[local] $*"; }
pass() { echo "[local] PASS: $*"; }
fail() { echo "[local] FAIL: $*" >&2; exit 1; }

if [[ "${1:-}" == "--status" ]]; then exec "${SCRIPT_DIR}/status.sh"; fi

# ─── SearXNG (native metasearch for the web-search skill) ─────────────────────
# Internal instance with JSON output enabled. Non-fatal: if SearXNG isn't installed
# or won't start, the rest of the stack still comes up (web-search is just unavailable).
SEARXNG_DIR="${SEARXNG_DIR:-/workspace/searxng}"
SEARXNG_SRC="${SEARXNG_DIR}/searxng-src"
SEARXNG_VENV="${SEARXNG_DIR}/searx-pyenv"
SEARXNG_SETTINGS="${SEARXNG_DIR}/settings.yml"
SEARXNG_PORT="${SEARXNG_PORT:-8080}"
searxng_alive() { curl -fsS "http://127.0.0.1:${SEARXNG_PORT}/search?q=ping&format=json" >/dev/null 2>&1; }
if [[ ! -x "${SEARXNG_VENV}/bin/python" || ! -d "${SEARXNG_SRC}/searx" ]]; then
  info "SearXNG not installed (run install.sh) — skipping; web-search skill will be unavailable"
elif searxng_alive; then
  info "searxng already running on :${SEARXNG_PORT}"
else
  info "starting SearXNG on :${SEARXNG_PORT} ..."
  (
    cd "$SEARXNG_SRC"
    SEARXNG_SETTINGS_PATH="$SEARXNG_SETTINGS" \
      nohup "${SEARXNG_VENV}/bin/python" -m searx.webapp >"$LOGS/searxng.log" 2>&1 &
    echo $! >"$PIDS/searxng.pid"
  )
  for i in $(seq 1 30); do
    searxng_alive && break
    kill -0 "$(cat "$PIDS/searxng.pid" 2>/dev/null)" 2>/dev/null || { info "WARN: SearXNG exited — see $LOGS/searxng.log"; break; }
    sleep 1
  done
fi
if searxng_alive; then
  pass "SearXNG ready :${SEARXNG_PORT}  (web-search skill -> SEARXNG_URL=http://localhost:${SEARXNG_PORT})"
else
  info "WARN: SearXNG not responding on :${SEARXNG_PORT} — web-search skill will be unavailable (see $LOGS/searxng.log)"
fi

# ─── MCP bridge (TypeScript) — build only; the orchestrator spawns it as a child ─
MCP_BRIDGE_DIR="${REPO_ROOT}/services/mcp-bridge"
MCP_DIST="${MCP_BRIDGE_DIR}/dist/index.js"
# Rebuild when dist is missing OR stale. A stale dist — e.g. compiled before a
# new src/ file was added — loads but crashes the bridge at import time
# (ERR_MODULE_NOT_FOUND), which the orchestrator only surfaces as "MCP bridge
# did not become ready". Two staleness signals:
#   (a) any src/*.ts newer than the compiled entrypoint (normal edit-then-run);
#   (b) any src/*.ts with NO matching dist/*.js — catches an incomplete/committed
#       dist on a FRESH CHECKOUT, where every file has the same mtime so (a) can
#       never fire (this is exactly how dist/utils/formatError.js went missing).
_mcp_stale() {
  [[ ! -f "$MCP_DIST" ]] && return 0
  [[ -n "$(find "$MCP_BRIDGE_DIR/src" -name '*.ts' -newer "$MCP_DIST" -print -quit 2>/dev/null)" ]] && return 0
  local ts rel js
  while IFS= read -r ts; do
    rel="${ts#"$MCP_BRIDGE_DIR"/src/}"
    js="$MCP_BRIDGE_DIR/dist/${rel%.ts}.js"
    [[ -f "$js" ]] || return 0
  done < <(find "$MCP_BRIDGE_DIR/src" -name '*.ts' ! -name '*.d.ts' 2>/dev/null)
  return 1
}
if _mcp_stale; then
  info "building MCP bridge (npm ci && npm run build) ..."
  command -v node >/dev/null 2>&1 || fail "node not found — install Node.js"
  (cd "$MCP_BRIDGE_DIR" && rm -rf dist && npm ci --quiet && npm run build --quiet) \
    >"$LOGS/mcp-bridge-build.log" 2>&1 \
    || fail "MCP bridge build failed — see $LOGS/mcp-bridge-build.log"
  pass "MCP bridge built → $MCP_DIST"
else
  info "MCP bridge already built (dist/index.js up to date)"
fi

# Skill dispatch runs in-process inside the local harness (SkillRegistry, Piece 4) —
# no standalone skill-worker process to start.

# ─── Local harness (single process: gateway + orchestrator, one asyncio loop) ──
_local_alive() { [[ -f "$PIDS/local.pid" ]] && kill -0 "$(cat "$PIDS/local.pid")" 2>/dev/null; }
if _local_alive; then
  info "local harness already running (pid $(cat "$PIDS/local.pid"))"
else
  info "starting local harness (services.local.main) ..."
  (
    source "${SCRIPT_DIR}/local.env"
    export PYTHONPATH="${REPO_ROOT}"
    export MCP_BRIDGE_ARGS="${MCP_DIST}"
    nohup python -m services.local.main >"$LOGS/local.log" 2>&1 &
    echo $! >"$PIDS/local.pid"
  )
  for i in $(seq 1 60); do
    _local_alive || { fail "local harness exited — see $LOGS/local.log"; }
    curl -fsS "http://127.0.0.1:${LOCAL_PORT:-8787}/healthz" >/dev/null 2>&1 && break
    sleep 1
  done
  pass "local harness running (pid $(cat "$PIDS/local.pid"))"
fi

echo
pass "Labmate stack is up. Connection settings:"
echo "    SEARXNG_URL=http://localhost:${SEARXNG_PORT:-8080}  (web-search skill)"
echo "    GEMMA_BASE=http://localhost:8000/v1"
echo "    LABMATE_GATEWAY_URL=ws://localhost:8787/ws  (CLI + Electron connect here)"
echo "    -> source infrastructure/local/local.env to export these."
echo "    -> Logs: $LOGS/  PIDs: $PIDS/"
