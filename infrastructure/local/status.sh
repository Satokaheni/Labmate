#!/usr/bin/env bash
# status.sh — Report health of the native Labmate support stack.
set -uo pipefail

ok()   { echo "  [UP]   $*"; }
down() { echo "  [DOWN] $*"; }

echo "Labmate local support stack:"

# MongoDB + replica set state
if mongosh --quiet --host 127.0.0.1 --port 27017 --eval 'db.adminCommand("ping").ok' >/dev/null 2>&1; then
  state="$(mongosh --quiet --host 127.0.0.1 --port 27017 --eval 'try{rs.status().myState}catch(e){"no-rs"}' 2>/dev/null | tail -1)"
  case "$state" in
    1) ok "MongoDB :27017  (replica set rs0, PRIMARY)" ;;
    *) ok "MongoDB :27017  (replica set state=$state — change streams need PRIMARY=1)" ;;
  esac
else
  down "MongoDB :27017"
fi

# Redis
if redis-cli -p 6379 ping 2>/dev/null | grep -q PONG; then
  ok "Redis   :6379   ($(redis-cli -p 6379 info server 2>/dev/null | grep -i redis_version | tr -d '\r'))"
else
  down "Redis   :6379"
fi

# Chroma — check the body, not just HTTP status (RunPod proxy returns 200 pages)
if curl -fsS "http://127.0.0.1:8765/api/v2/heartbeat" 2>/dev/null | grep -q heartbeat; then
  ok "Chroma  :8765"
else
  down "Chroma  :8765"
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

# Vision server (Gemma 3 vision via llama.cpp, optional / GPU 1)
if curl -fsS "http://localhost:${VISION_PORT:-8001}/health" >/dev/null 2>&1; then
  ok "vision  llama-server :${VISION_PORT:-8001}  UP"
else
  echo "  vision  llama-server :${VISION_PORT:-8001}  (down / not configured)"
fi
