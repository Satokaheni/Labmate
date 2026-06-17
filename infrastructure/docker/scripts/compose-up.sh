#!/usr/bin/env bash
# compose-up.sh — Start Labmate support services via Docker Compose
#
# The inference server (vLLM/Gemma 4) runs on the HOST, not in Docker.
# Start it BEFORE running this script. See gpu-check.sh for host GPU verification.
#
# Usage:
#   ./compose-up.sh            # Start with 1 skill-worker (default)
#   ./compose-up.sh 4          # Start with 4 skill-worker replicas
#   ./compose-up.sh --url http://192.168.1.10:8000   # Custom inference server URL
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/../docker-compose.yml"

# ─── Parse arguments ──────────────────────────────────────────────────────────
SKILL_WORKER_REPLICAS=1
INFERENCE_URL="${INFERENCE_URL:-http://host.docker.internal:8000}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      shift
      INFERENCE_URL="${1:?'--url requires a value'}"
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--url <inference-url>] [<skill-worker-replicas>]"
      echo ""
      echo "  --url <url>   URL of the host-side inference server (default: http://host.docker.internal:8000)"
      echo "  <N>           Number of skill-worker replicas (default: 1)"
      echo ""
      echo "The inference server must be running on the HOST before this script is called."
      echo "Start it with:  vllm serve <model> --port 8000"
      echo "Verify GPU:     ./gpu-check.sh"
      exit 0
      ;;
    [0-9]*)
      SKILL_WORKER_REPLICAS="$1"
      shift
      ;;
    *)
      echo "[compose-up] ERROR: Unknown argument: $1" >&2
      echo "Run '$0 --help' for usage." >&2
      exit 1
      ;;
  esac
done

info() { echo "[compose-up] $*"; }
pass() { echo "[compose-up] PASS: $*"; }
fail() { echo "[compose-up] FAIL: $*" >&2; exit 1; }

if [ ! -f "${COMPOSE_FILE}" ]; then
  fail "docker-compose.yml not found at: ${COMPOSE_FILE}"
fi

# ─── Step 1: Verify inference server is reachable ────────────────────────────
info "Step 1/3: Checking inference server at ${INFERENCE_URL} ..."
HEALTH_URL="${INFERENCE_URL%/}/health"
if curl -fsS --max-time 5 "${HEALTH_URL}" > /dev/null 2>&1; then
  pass "Inference server is reachable"
else
  echo ""
  echo "  [compose-up] WARNING: Inference server not reachable at ${HEALTH_URL}"
  echo "  Containers will start but mcp-bridge will fail until the server is up."
  echo ""
  echo "  To start vLLM on the host:"
  echo "    vllm serve google/gemma-4-9b-it \\"
  echo "      --quantization bitsandbytes \\"
  echo "      --tool-call-parser gemma4 \\"
  echo "      --enable-auto-tool-choice \\"
  echo "      --port 8000"
  echo ""
  read -r -p "[compose-up] Continue anyway? (y/N): " CONFIRM
  if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
    fail "Aborted. Start the inference server first."
  fi
fi

# ─── Step 2: Check Docker Compose version ─────────────────────────────────────
info "Step 2/3: Checking Docker Compose version..."
COMPOSE_VERSION="$(docker compose version --short 2>/dev/null || echo "0.0.0")"
info "  Docker Compose version: ${COMPOSE_VERSION}"

MIN_COMPOSE="2.20.0"
if [ "$(printf '%s\n%s\n' "${MIN_COMPOSE}" "${COMPOSE_VERSION}" | sort -V | head -n1)" != "${MIN_COMPOSE}" ]; then
  info "  WARNING: Docker Compose ${COMPOSE_VERSION} < ${MIN_COMPOSE}."
  info "           The 'start_interval' healthcheck field requires 2.20+."
  info "           Upgrade: https://docs.docker.com/compose/install/"
fi

# ─── Step 3: Start services ───────────────────────────────────────────────────
info "Step 3/3: Starting support services..."
info "  Compose file:       ${COMPOSE_FILE}"
info "  Inference URL:      ${INFERENCE_URL}"
info "  skill-worker count: ${SKILL_WORKER_REPLICAS}"

INFERENCE_URL="${INFERENCE_URL}" docker compose \
  -f "${COMPOSE_FILE}" \
  up -d \
  --scale skill-worker="${SKILL_WORKER_REPLICAS}" \
  --remove-orphans

pass "docker compose up -d completed"

echo ""
docker compose -f "${COMPOSE_FILE}" ps
echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║        Labmate Support Services Started                ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
info "Useful commands:"
info "  docker compose -f ${COMPOSE_FILE} logs -f"
info "  docker compose -f ${COMPOSE_FILE} ps"
info "  docker compose -f ${COMPOSE_FILE} down           # stop (keeps volumes)"
info "  docker compose -f ${COMPOSE_FILE} down --volumes # DESTROYS ALL DATA"
info ""
info "Scale workers: docker compose -f ${COMPOSE_FILE} up -d --scale skill-worker=<N>"
