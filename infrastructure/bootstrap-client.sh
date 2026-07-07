#!/usr/bin/env bash
# bootstrap-client.sh — one-command setup of the Labmate CLIENT (harness + frontend)
# on a fresh Mac. The model lives on a separate machine; set GEMMA_BASE in the app
# on first launch. This wraps the existing installer + the frontend build; the .dmg
# packaging is a separate later effort.
#
# Usage:
#   infrastructure/bootstrap-client.sh            # install deps + build the frontend
#   infrastructure/bootstrap-client.sh --dry-run  # print the steps without running
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

run() { echo "+ $*"; [[ "$DRY" == "1" ]] || "$@"; }

echo "[bootstrap] Labmate client setup"
run bash "${SCRIPT_DIR}/install.sh" --client-only
run bash -c "cd '${REPO_ROOT}/services/frontend' && npm ci && npm run build:main"
echo "[bootstrap] Done. Now launch the app: cd services/frontend && npm run dev:electron"
