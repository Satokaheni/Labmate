#!/usr/bin/env bash
# run-services.sh — Start Labmate Docker services using raw docker commands.
#
# No docker compose required. Re-running is safe (idempotent):
#   - already-running containers are left alone
#   - stopped containers are removed and recreated
#
# Usage:
#   ./run-services.sh                          # start infra + app services
#   ./run-services.sh --infra-only             # mongo + chroma + redis only
#   ./run-services.sh --workers 4              # 4 skill-worker replicas
#   ./run-services.sh --inference-url http://192.168.1.10:8000
#   ./run-services.sh --stop                   # stop and remove all containers
#   ./run-services.sh --status                 # show container status
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
NETWORK="labmate"
INFERENCE_URL="${INFERENCE_URL:-http://host.docker.internal:8000}"
SKILL_WORKER_REPLICAS=1
INFRA_ONLY=false
ACTION="start"

# Image names — override via env vars if you push to a registry
IMG_MCP_BRIDGE="${IMG_MCP_BRIDGE:-labmate/mcp-bridge:latest}"
IMG_ORCHESTRATOR="${IMG_ORCHESTRATOR:-labmate/orchestrator:latest}"
IMG_SKILL_WORKER="${IMG_SKILL_WORKER:-labmate/skill-worker:latest}"

LOG_LEVEL="${LOG_LEVEL:-info}"
MCP_PORT="${MCP_PORT:-9000}"
SEARXNG_PORT="${SEARXNG_PORT:-8888}"
SEARXNG_URL="${SEARXNG_URL:-http://searxng:8080}"   # in-network URL the web-search skill uses
# Host path to the bind-mounted SearXNG settings (JSON output enabled, limiter off).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEARXNG_CONFIG_DIR="${SEARXNG_CONFIG_DIR:-${SCRIPT_DIR}/../searxng-config}"

# ─── Parse arguments ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --infra-only)     INFRA_ONLY=true; shift ;;
    --workers)        shift; SKILL_WORKER_REPLICAS="${1:?'--workers requires a number'}"; shift ;;
    --inference-url)  shift; INFERENCE_URL="${1:?'--inference-url requires a value'}"; shift ;;
    --stop)           ACTION="stop"; shift ;;
    --status)         ACTION="status"; shift ;;
    --help|-h)
      sed -n '2,8p' "$0" | sed 's/^# //'
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      echo "Run '$0 --help' for usage." >&2
      exit 1
      ;;
  esac
done

# ─── Helpers ──────────────────────────────────────────────────────────────────
info()    { echo "  [+] $*"; }
success() { echo "  [✓] $*"; }
warn()    { echo "  [!] $*"; }
section() { echo ""; echo "══ $* ══"; }

# Returns 0 if the container exists (running or stopped), 1 if it doesn't.
container_exists() { docker inspect --type=container "$1" &>/dev/null; }

# Returns 0 if the container is currently running.
container_running() {
  [[ "$(docker inspect --format='{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]
}

# Returns 0 if the local image exists.
image_exists() { docker image inspect "$1" &>/dev/null; }

# Ensure the container is stopped and removed so we can recreate it cleanly.
remove_if_exists() {
  local name="$1"
  if container_exists "$name"; then
    if container_running "$name"; then
      info "Stopping existing container: $name"
      docker stop "$name" >/dev/null
    fi
    info "Removing existing container: $name"
    docker rm "$name" >/dev/null
  fi
}

# Start a container only if it isn't already running. If it exists but is
# stopped, remove it first so docker run always creates a fresh one.
ensure_running() {
  local name="$1"
  shift
  if container_running "$name"; then
    success "$name already running — skipping"
    return 0
  fi
  remove_if_exists "$name"
  info "Starting $name ..."
  docker run --name "$name" "$@"
  success "$name started"
}

# ─── Add host.docker.internal on Linux ────────────────────────────────────────
# On macOS/Docker Desktop this resolves natively. On Linux we need to add it.
HOST_GATEWAY_FLAG=()
if [[ "$(uname -s)" == "Linux" ]]; then
  HOST_GATEWAY_FLAG=(--add-host "host.docker.internal:host-gateway")
fi

# ─── Status ───────────────────────────────────────────────────────────────────
if [[ "$ACTION" == "status" ]]; then
  echo ""
  echo "Labmate container status:"
  echo ""
  docker ps -a --filter "network=${NETWORK}" \
    --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null \
    || docker ps -a --filter "name=lm-" \
         --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
  exit 0
fi

# ─── Stop ─────────────────────────────────────────────────────────────────────
if [[ "$ACTION" == "stop" ]]; then
  section "Stopping Labmate services"
  CONTAINERS=(lm-mcp-bridge lm-orchestrator lm-mongodb lm-chroma lm-redis lm-searxng)
  for i in $(seq 1 "${SKILL_WORKER_REPLICAS}"); do
    CONTAINERS+=("lm-skill-worker-${i}")
  done
  for c in "${CONTAINERS[@]}"; do
    if container_exists "$c"; then
      info "Stopping $c ..."
      docker stop "$c" >/dev/null && docker rm "$c" >/dev/null
      success "$c removed"
    fi
  done
  echo ""
  echo "All Labmate containers stopped."
  exit 0
fi

# ─── Start ────────────────────────────────────────────────────────────────────
section "Labmate Service Startup"
echo ""
echo "  Inference URL:    ${INFERENCE_URL}"
echo "  Skill workers:    ${SKILL_WORKER_REPLICAS}"
echo "  Infra-only mode:  ${INFRA_ONLY}"
echo ""

# ── Network ───────────────────────────────────────────────────────────────────
section "Network"
if docker network inspect "${NETWORK}" &>/dev/null; then
  success "Network '${NETWORK}' already exists"
else
  info "Creating bridge network '${NETWORK}' ..."
  docker network create "${NETWORK}"
  success "Network '${NETWORK}' created"
fi

# ── Volumes ───────────────────────────────────────────────────────────────────
section "Volumes"
for vol in mongo-data chroma-data redis-data; do
  if docker volume inspect "$vol" &>/dev/null; then
    success "Volume '$vol' already exists"
  else
    info "Creating volume '$vol' ..."
    docker volume create "$vol" >/dev/null
    success "Volume '$vol' created"
  fi
done

# ── MongoDB ───────────────────────────────────────────────────────────────────
section "MongoDB"
ensure_running lm-mongodb \
  --detach \
  --restart unless-stopped \
  --network "${NETWORK}" \
  --network-alias mongodb \
  --volume mongo-data:/data/db \
  --health-cmd 'mongosh --quiet --eval "db.adminCommand('"'"'ping'"'"').ok"' \
  --health-interval 10s \
  --health-timeout 5s \
  --health-retries 5 \
  --health-start-period 30s \
  mongo:7

# ── ChromaDB ──────────────────────────────────────────────────────────────────
section "ChromaDB"
ensure_running lm-chroma \
  --detach \
  --restart unless-stopped \
  --network "${NETWORK}" \
  --network-alias chroma \
  --volume chroma-data:/chroma/chroma \
  --env CHROMA_SERVER_HOST=0.0.0.0 \
  --env CHROMA_SERVER_HTTP_PORT=8000 \
  --env IS_PERSISTENT=TRUE \
  --env ANONYMIZED_TELEMETRY=FALSE \
  --health-cmd 'curl -fsS http://localhost:8000/api/v2/heartbeat' \
  --health-interval 10s \
  --health-timeout 5s \
  --health-retries 5 \
  --health-start-period 30s \
  chromadb/chroma:latest

# ── Redis ─────────────────────────────────────────────────────────────────────
section "Redis"
ensure_running lm-redis \
  --detach \
  --restart unless-stopped \
  --network "${NETWORK}" \
  --network-alias redis \
  --volume redis-data:/data \
  --health-cmd 'redis-cli ping' \
  --health-interval 10s \
  --health-timeout 3s \
  --health-retries 5 \
  --health-start-period 10s \
  redis:7-alpine \
  redis-server --appendonly yes --appendfsync everysec

# ── SearXNG (web-search skill backend) ────────────────────────────────────────
# Bind-mounts our settings.yml (JSON output enabled, limiter off). The web-search
# skill queries it at http://searxng:8080/search?format=json on the labmate network.
section "SearXNG"
ensure_running lm-searxng \
  --detach \
  --restart unless-stopped \
  --network "${NETWORK}" \
  --network-alias searxng \
  --publish "${SEARXNG_PORT}:8080" \
  --volume "${SEARXNG_CONFIG_DIR}:/etc/searxng:ro" \
  --env SEARXNG_BASE_URL="http://localhost:${SEARXNG_PORT}/" \
  --health-cmd 'wget -qO- "http://localhost:8080/search?q=ping&format=json" >/dev/null || exit 1' \
  --health-interval 10s \
  --health-timeout 5s \
  --health-retries 5 \
  --health-start-period 20s \
  searxng/searxng:latest

if [[ "${INFRA_ONLY}" == "true" ]]; then
  echo ""
  echo "══ Infra-only mode — skipping app services ══"
  echo ""
  docker ps --filter "name=lm-" --format "table {{.Names}}\t{{.Status}}"
  exit 0
fi

# ── App services: wait for infra to be healthy ────────────────────────────────
section "Waiting for infra services to be healthy"
wait_healthy() {
  local name="$1"
  local retries=30
  local i=0
  while [[ $i -lt $retries ]]; do
    status="$(docker inspect --format='{{.State.Health.Status}}' "$name" 2>/dev/null || echo 'missing')"
    if [[ "$status" == "healthy" ]]; then
      success "$name is healthy"
      return 0
    fi
    info "$name status: $status — waiting... ($((i+1))/$retries)"
    sleep 3
    (( i++ ))
  done
  warn "$name did not become healthy in time — continuing anyway"
}

wait_healthy lm-mongodb
wait_healthy lm-chroma
wait_healthy lm-redis
wait_healthy lm-searxng   # non-blocking: web-search is optional, app services start regardless

# ── MCP Bridge ────────────────────────────────────────────────────────────────
section "MCP Bridge"
if ! image_exists "${IMG_MCP_BRIDGE}"; then
  warn "Image '${IMG_MCP_BRIDGE}' not found — skipping mcp-bridge."
  warn "Build it first:  docker build -t ${IMG_MCP_BRIDGE} ./services/mcp-bridge"
else
  ensure_running lm-mcp-bridge \
    --detach \
    --restart unless-stopped \
    --network "${NETWORK}" \
    --network-alias mcp-bridge \
    --publish "${MCP_PORT}:9000" \
    --env INFERENCE_URL="${INFERENCE_URL}" \
    --env MCP_PORT=9000 \
    --env SEARXNG_URL="${SEARXNG_URL}" \
    --env LOG_LEVEL="${LOG_LEVEL}" \
    "${HOST_GATEWAY_FLAG[@]}" \
    "${IMG_MCP_BRIDGE}"
fi

# ── Python Orchestrator ───────────────────────────────────────────────────────
section "Python Orchestrator"
if ! image_exists "${IMG_ORCHESTRATOR}"; then
  warn "Image '${IMG_ORCHESTRATOR}' not found — skipping orchestrator."
  warn "Build it first:  docker build -t ${IMG_ORCHESTRATOR} ./services/orchestrator"
else
  ensure_running lm-orchestrator \
    --detach \
    --restart unless-stopped \
    --network "${NETWORK}" \
    --network-alias python-orchestrator \
    --env INFERENCE_URL="${INFERENCE_URL}" \
    --env MCP_BRIDGE_URL="http://mcp-bridge:9000" \
    --env MONGO_URI="mongodb://mongodb:27017/labmate" \
    --env CHROMA_URL="http://chroma:8000" \
    --env REDIS_URL="redis://redis:6379/0" \
    --env SEARXNG_URL="${SEARXNG_URL}" \
    --env LOG_LEVEL="${LOG_LEVEL}" \
    "${HOST_GATEWAY_FLAG[@]}" \
    "${IMG_ORCHESTRATOR}"
fi

# ── Skill Workers ─────────────────────────────────────────────────────────────
section "Skill Workers (${SKILL_WORKER_REPLICAS} replica(s))"
if ! image_exists "${IMG_SKILL_WORKER}"; then
  warn "Image '${IMG_SKILL_WORKER}' not found — skipping skill-worker(s)."
  warn "Build it first:  docker build -t ${IMG_SKILL_WORKER} ./services/skill-worker"
else
  for i in $(seq 1 "${SKILL_WORKER_REPLICAS}"); do
    ensure_running "lm-skill-worker-${i}" \
      --detach \
      --restart unless-stopped \
      --network "${NETWORK}" \
      --env REDIS_URL="redis://redis:6379/0" \
      --env MCP_BRIDGE_URL="http://mcp-bridge:9000" \
      --env WORKER_CONCURRENCY=2 \
      --env SEARXNG_URL="${SEARXNG_URL}" \
      --env LOG_LEVEL="${LOG_LEVEL}" \
      "${IMG_SKILL_WORKER}"
  done
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════"
echo "  Labmate services started"
echo "══════════════════════════════════════════════════"
echo ""
docker ps --filter "name=lm-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo "  Inference URL:  ${INFERENCE_URL}"
echo ""
echo "  Useful commands:"
echo "    docker logs -f lm-mcp-bridge"
echo "    docker logs -f lm-orchestrator"
echo "    $0 --status"
echo "    $0 --stop"
echo ""
echo "  Scale workers:"
echo "    $0 --workers 4"
