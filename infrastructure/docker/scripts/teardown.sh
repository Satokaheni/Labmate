#!/usr/bin/env bash
# teardown.sh — Tear down k3d cluster and clean up Docker resources
# Usage: ./teardown.sh [--volumes] [--images]
#
# Flags:
#   --volumes   Also remove named Docker volumes (model-cache, mongo-data, etc.)
#               WARNING: This deletes model weights. Re-download will be required.
#   --images    Also remove Labmate Docker images (labmate/*)
#   --all       Equivalent to --volumes --images
#
# By default (no flags): deletes k3d cluster and prunes dangling resources only.
# Named volumes and images are left intact to avoid re-downloading model weights.
set -euo pipefail

CLUSTER_NAME="labmate"
COMPOSE_PROJECT="infrastructure"  # matches directory name used by docker compose

# Named volumes created by docker-compose.yml
NAMED_VOLUMES=(
  "${COMPOSE_PROJECT}_model-cache"
  "${COMPOSE_PROJECT}_mongo-data"
  "${COMPOSE_PROJECT}_chroma-data"
  "${COMPOSE_PROJECT}_redis-data"
)

# Labmate images
LOCAL_IMAGES=(
  "labmate/inference:latest"
  "labmate/mcp-bridge:latest"
  "labmate/orchestrator:latest"
  "labmate/skill-worker:latest"
)

# ─── Parse flags ──────────────────────────────────────────────────────────────
REMOVE_VOLUMES=false
REMOVE_IMAGES=false

for arg in "$@"; do
  case "${arg}" in
    --volumes) REMOVE_VOLUMES=true ;;
    --images)  REMOVE_IMAGES=true ;;
    --all)     REMOVE_VOLUMES=true; REMOVE_IMAGES=true ;;
    *)
      echo "[teardown] Unknown argument: ${arg}" >&2
      echo "Usage: $0 [--volumes] [--images] [--all]" >&2
      exit 1
      ;;
  esac
done

# ─── Helpers ──────────────────────────────────────────────────────────────────
info()  { echo "[teardown] $*"; }
pass()  { echo "[teardown] OK: $*"; }
warn()  { echo "[teardown] WARNING: $*" >&2; }

# ─── Step 1: Stop Docker Compose (Tier 1) ────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/../docker-compose.yml"

if [ -f "${COMPOSE_FILE}" ]; then
  info "Step 1: Stopping Docker Compose services..."
  if docker compose -f "${COMPOSE_FILE}" ps --quiet 2>/dev/null | grep -q .; then
    if [ "${REMOVE_VOLUMES}" = "true" ]; then
      warn "Removing named volumes (--volumes). Model weights will need re-download."
      docker compose -f "${COMPOSE_FILE}" down --volumes 2>/dev/null || true
    else
      docker compose -f "${COMPOSE_FILE}" down 2>/dev/null || true
    fi
    pass "Docker Compose services stopped"
  else
    info "No running Compose services found (already stopped or never started)"
  fi
else
  info "docker-compose.yml not found at expected path, skipping Compose teardown"
fi

# ─── Step 2: Delete k3d cluster (Tier 2) ─────────────────────────────────────
info "Step 2: Deleting k3d cluster '${CLUSTER_NAME}'..."
if command -v k3d >/dev/null 2>&1; then
  if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}"; then
    k3d cluster delete "${CLUSTER_NAME}"
    pass "k3d cluster '${CLUSTER_NAME}' deleted"
  else
    info "k3d cluster '${CLUSTER_NAME}' not found (already deleted or never created)"
  fi
else
  info "k3d not installed, skipping cluster deletion"
fi

# ─── Step 3: Optional — remove named volumes ─────────────────────────────────
if [ "${REMOVE_VOLUMES}" = "true" ]; then
  info "Step 3: Removing named Docker volumes..."
  for vol in "${NAMED_VOLUMES[@]}"; do
    if docker volume inspect "${vol}" >/dev/null 2>&1; then
      docker volume rm "${vol}"
      pass "Volume removed: ${vol}"
    else
      info "Volume not found (skip): ${vol}"
    fi
  done
else
  info "Step 3: Skipping named volume removal (pass --volumes to remove)"
  info "  Volumes preserved: ${NAMED_VOLUMES[*]}"
fi

# ─── Step 4: Optional — remove Labmate images ─────────────────────────────
if [ "${REMOVE_IMAGES}" = "true" ]; then
  info "Step 4: Removing Labmate Docker images..."
  for img in "${LOCAL_IMAGES[@]}"; do
    if docker image inspect "${img}" >/dev/null 2>&1; then
      docker rmi "${img}" || warn "Could not remove ${img} (may be in use)"
      pass "Image removed: ${img}"
    else
      info "Image not found (skip): ${img}"
    fi
  done
else
  info "Step 4: Skipping image removal (pass --images to remove)"
fi

# ─── Step 5: Prune dangling (untagged) Docker resources ──────────────────────
info "Step 5: Pruning dangling Docker resources (containers, networks, build cache)..."
docker system prune -f >/dev/null 2>&1 && pass "Dangling resources pruned" || warn "docker system prune failed (non-fatal)"

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║        Teardown Complete                                 ║"
echo "╚══════════════════════════════════════════════════════════╝"

if [ "${REMOVE_VOLUMES}" = "false" ]; then
  info ""
  info "Named volumes were preserved. To inspect:"
  for vol in "${NAMED_VOLUMES[@]}"; do
    info "  docker volume inspect ${vol}"
  done
  info ""
  info "To delete ALL data including model weights:"
  info "  $0 --all"
fi
