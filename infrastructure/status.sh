#!/usr/bin/env bash
# status.sh — Report health of the native Labmate local harness.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PIDS="${REPO_ROOT}/.data/pids"

ok()   { echo "  [UP]   $*"; }
down() { echo "  [DOWN] $*"; }

echo "Labmate local harness:"

# Local harness (single process: gateway + orchestrator)
LOCAL_PORT="${LOCAL_PORT:-8787}"
if curl -fsS "http://127.0.0.1:${LOCAL_PORT}/healthz" 2>/dev/null | grep -q "ok"; then
  if [[ -f "$PIDS/local.pid" ]]; then
    ok "local harness :${LOCAL_PORT}  (services.local.main, pid $(cat "$PIDS/local.pid"))"
  else
    ok "local harness :${LOCAL_PORT}  (services.local.main)"
  fi
else
  down "local harness :${LOCAL_PORT}  (services.local.main) — run start.sh"
fi

# SearXNG — validate the JSON API the web-search skill actually uses
SEARXNG_PORT="${SEARXNG_PORT:-8080}"
if curl -fsS "http://127.0.0.1:${SEARXNG_PORT}/search?q=ping&format=json" 2>/dev/null | grep -q '"results"'; then
  ok "SearXNG :${SEARXNG_PORT}   (web-search skill; JSON API)"
else
  down "SearXNG :${SEARXNG_PORT}   (web-search skill) — run install.sh then start.sh"
fi

# Model server (Gemma 4 via llama.cpp)
if curl -fsS "http://127.0.0.1:8000/health" 2>/dev/null | grep -q '"status":"ok"'; then
  ok "Gemma4  :8000   (llama.cpp, OpenAI API at /v1)"
else
  down "Gemma4  :8000   (llama.cpp) — run serve-model.sh"
fi
