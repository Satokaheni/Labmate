#!/usr/bin/env bash
# start-cli.sh — Launch the Labmate CLI against the local native stack.
#
# Prerequisites (run in this order before this script):
#   1. ./serve-model.sh          # llama-server on :8000
#   2. ./start.sh                # MongoDB, Redis, Chroma, MCP bridge, orchestrator
#
# This script always starts a REPL session. Positional args (which would trigger
# one-shot mode) are silently dropped. For one-shot use python -m services.cli directly.
#
# Usage:
#   ./start-cli.sh                        # interactive REPL with workspace picker
#   ./start-cli.sh --resume <session-id>  # resume a previous session
#   ./start-cli.sh --workspace <ws-id>    # open a specific workspace
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

info() { echo "[cli] $*"; }
fail() { echo "[cli] FAIL: $*" >&2; exit 1; }

# ─── Pre-flight: check that the stack is up ────────────────────────────────────
info "Checking infrastructure..."

PIDS="${REPO_ROOT}/.data/pids"

# ws-gateway (the CLI connects here — replaces direct Redis access)
if [[ -f "${PIDS}/ws-gateway.pid" ]]; then
  kill -0 "$(cat "${PIDS}/ws-gateway.pid")" 2>/dev/null \
    || fail "ws-gateway pidfile exists but process is dead. Run: ./start.sh"
else
  fail "ws-gateway not started. Run: ./start.sh"
fi
curl -fsS "http://localhost:8787/healthz" >/dev/null 2>&1 \
  || fail "ws-gateway /health not responding. Run: ./start.sh"

# Orchestrator
if [[ -f "${PIDS}/orchestrator.pid" ]]; then
  kill -0 "$(cat "${PIDS}/orchestrator.pid")" 2>/dev/null \
    || fail "Orchestrator pidfile exists but process is dead. Run: ./start.sh"
else
  fail "Orchestrator not started. Run: ./start.sh"
fi

info "Stack looks healthy. Starting CLI..."
echo ""

# ─── Source env and run ────────────────────────────────────────────────────────
# shellcheck source=local.env
source "${SCRIPT_DIR}/local.env"
export PYTHONPATH="${REPO_ROOT}"

cd "${REPO_ROOT}"

# Pass through --resume/-r and --workspace/-w flags; drop positional args so
# this script always launches the interactive REPL, never one-shot mode.
CLI_ARGS=()
skip_next=false
for arg in "$@"; do
  if $skip_next; then
    CLI_ARGS+=("$arg")
    skip_next=false
    continue
  fi
  case "$arg" in
    --resume|-r|--workspace|-w)
      CLI_ARGS+=("$arg")
      skip_next=true
      ;;
    -*)
      CLI_ARGS+=("$arg")
      ;;
    # Positional args (no leading dash) are dropped — forces REPL mode
  esac
done

if [ "${#CLI_ARGS[@]}" -gt 0 ]; then
    exec python -m services.cli "${CLI_ARGS[@]}"
else
    exec python -m services.cli
fi
