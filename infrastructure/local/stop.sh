#!/usr/bin/env bash
# stop.sh — Stop the native Labmate support stack started by start.sh.
#
# Data under <repo>/.data is preserved. Re-run start.sh to bring it back.
#
# By DEFAULT the model server (llama-server) is left running — it takes ~10 min
# to load Gemma 4 into VRAM, so killing it on every support-stack restart is
# expensive and surprising (it also contradicted the docs, which said the model
# is left up). Pass --all (or --model) to also stop llama-server.
#
# Usage:
#   ./stop.sh           # stop support stack + orchestrator; LEAVE the model up
#   ./stop.sh --all     # also stop the model server (llama-server)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PIDS="${REPO_ROOT}/.data/pids"

STOP_MODEL=false
[[ "${1:-}" == "--all" || "${1:-}" == "--model" ]] && STOP_MODEL=true

info() { echo "[local] $*"; }

_stop_pid() {
  local name="$1" pidfile="$2"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    info "stopping $name (pid $(cat "$pidfile")) ..."
    kill "$(cat "$pidfile")" 2>/dev/null || true
    rm -f "$pidfile"
  else
    pkill -f "$name" 2>/dev/null && info "stopped stray $name" || info "$name not running"
  fi
}

# ─── Discord connector ────────────────────────────────────────────────────────
_stop_pid "discord-connector" "$PIDS/discord-connector.pid"

# ─── Orchestrator ─────────────────────────────────────────────────────────────
_stop_pid "orchestrator.main" "$PIDS/orchestrator.pid"

# ─── Skill worker ─────────────────────────────────────────────────────────────
_stop_pid "skill_worker.worker" "$PIDS/skill-worker.pid"

# ─── SearXNG (native metasearch) ──────────────────────────────────────────────
_stop_pid "searx.webapp" "$PIDS/searxng.pid"

# ─── Model server (Gemma 4 via llama.cpp) — only with --all/--model ───────────
if $STOP_MODEL; then
  if [[ -f "$PIDS/llama-server.pid" ]] && kill -0 "$(cat "$PIDS/llama-server.pid")" 2>/dev/null; then
    info "stopping llama-server (pid $(cat "$PIDS/llama-server.pid")) ..."
    kill "$(cat "$PIDS/llama-server.pid")" 2>/dev/null || true
    rm -f "$PIDS/llama-server.pid"
  else
    pkill -f "llama-server" 2>/dev/null && info "stopped stray llama-server" || info "llama-server not running"
  fi
else
  info "leaving llama-server running (pass --all to stop it; ~10 min to reload)"
fi

# ─── Chroma ───────────────────────────────────────────────────────────────────
if [[ -f "$PIDS/chroma.pid" ]] && kill -0 "$(cat "$PIDS/chroma.pid")" 2>/dev/null; then
  info "stopping chroma (pid $(cat "$PIDS/chroma.pid")) ..."
  kill "$(cat "$PIDS/chroma.pid")" 2>/dev/null || true
  rm -f "$PIDS/chroma.pid"
else
  pkill -f "chroma run" 2>/dev/null && info "stopped stray chroma" || info "chroma not running"
fi

# ─── Redis ────────────────────────────────────────────────────────────────────
if redis-cli -p 6379 ping >/dev/null 2>&1; then
  info "stopping redis (shutdown nosave-safe) ..."
  redis-cli -p 6379 shutdown 2>/dev/null || true
else
  info "redis not running"
fi

# ─── MongoDB ──────────────────────────────────────────────────────────────────
if pgrep -x mongod >/dev/null 2>&1; then
  info "stopping mongod (clean shutdown) ..."
  mongosh --quiet --host 127.0.0.1 --port 27017 --eval 'db.getSiblingDB("admin").shutdownServer()' >/dev/null 2>&1 || true
  sleep 2
  pgrep -x mongod >/dev/null 2>&1 && { info "forcing mongod stop"; pkill -x mongod; }
else
  info "mongod not running"
fi

info "local stack stopped (data preserved under .data/)."
