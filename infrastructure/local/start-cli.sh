#!/usr/bin/env bash
# start-cli.sh — Launch the Labmate CLI against the local native stack.
#
# Prerequisites (run in this order before this script):
#   1. ./serve-model.sh          # llama-server on :8000
#   2. ./start.sh                # MongoDB, Redis, Chroma, MCP bridge, orchestrator
#
# Usage:
#   ./start-cli.sh                        # interactive REPL with workspace picker
#   ./start-cli.sh "write a hello world"  # one-shot task
#   ./start-cli.sh --resume <session-id>  # resume a previous session
#   ./start-cli.sh --workspace <ws-id>    # open a specific workspace
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

info() { echo "[cli] $*"; }
fail() { echo "[cli] FAIL: $*" >&2; exit 1; }

# ─── Pre-flight: check that the stack is up ────────────────────────────────────
info "Checking infrastructure..."

# Redis
redis-cli -p 6379 ping 2>/dev/null | grep -q PONG \
  || fail "Redis not responding. Run: ./start.sh"

# Orchestrator (it writes its log; checking the pidfile is the lightest check)
PIDS="${REPO_ROOT}/.data/pids"
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
exec python -m services.cli "$@"
