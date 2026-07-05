#!/usr/bin/env bash
# stop.sh — Stop the native Labmate local harness started by start.sh.
#
# Data under <repo>/.data is preserved. Re-run start.sh to bring it back.
#
# By DEFAULT the model server (llama-server) is left running — it takes ~10 min
# to load Gemma 4 into VRAM, so killing it on every restart is expensive and
# surprising (it also contradicted the docs, which said the model is left up).
# Pass --all (or --model) to also stop llama-server.
#
# Usage:
#   ./stop.sh           # stop the local harness (services.local.main); LEAVE the model up
#   ./stop.sh --all     # also stop the model server (llama-server)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PIDS="${REPO_ROOT}/.data/pids"

STOP_MODEL=false
[[ "${1:-}" == "--all" || "${1:-}" == "--model" ]] && STOP_MODEL=true

info() { echo "[local] $*"; }

_stop_pid() {
  local name="$1" pidfile="$2" pid="" _i
  [[ -f "$pidfile" ]] && pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    info "stopping $name (pid $pid) ..."
    kill "$pid" 2>/dev/null || true
    # A cooperative-SIGTERM service (e.g. the orchestrator, whose handler only
    # schedules an async stop) blocked mid-generation can outlive a plain kill.
    # Wait for graceful exit, then escalate to SIGKILL so it never orphans.
    for _i in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then
      info "$name (pid $pid) ignored SIGTERM — sending SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pidfile"
  # ALWAYS sweep untracked strays (the leak fix): repeated restarts have left
  # processes the pidfile never tracked, silently splitting/contaminating later
  # runs (e.g. two local-harness processes both polling the same SQLite state).
  # Reap every match by module name, escalating to SIGKILL. (Safe: this script's
  # own argv does not contain "$name", so pgrep -f cannot match the caller.)
  local strays
  strays="$(pgrep -f "$name" 2>/dev/null || true)"
  if [[ -n "$strays" ]]; then
    info "reaping stray $name: $(echo "$strays" | tr '\n' ' ')"
    kill $strays 2>/dev/null || true
    for _i in $(seq 1 10); do pgrep -f "$name" >/dev/null 2>&1 || break; sleep 1; done
    strays="$(pgrep -f "$name" 2>/dev/null || true)"
    [[ -n "$strays" ]] && kill -9 $strays 2>/dev/null || true
  fi
}

# ─── Local harness (single process: gateway + orchestrator) ───────────────────
_stop_pid "services.local.main" "$PIDS/local.pid"

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

info "local stack stopped (data preserved under .data/)."
