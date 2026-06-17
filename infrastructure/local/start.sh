#!/usr/bin/env bash
# start.sh — Start the Labmate support stack NATIVELY on the host (no Docker).
#
# Why this exists: this pod cannot run any container engine (no NET_ADMIN, and
# the unshare/clone namespace syscalls are blocked by seccomp). Docker/Podman
# both fail at network/namespace creation. So the three support services run as
# ordinary host processes here. The Docker path lives in ../docker/ for when
# Labmate runs on a privileged host / your own server.
#
# Services started:
#   - MongoDB  :27017  (single-node replica set rs0 — required for change streams)
#   - Redis    :6379   (appendonly, everysec — Streams for task queues)
#   - Chroma   :8765   (client-server mode; :8000=host vLLM, :8001=RunPod proxy)
#
# Data + logs + pidfiles live under <repo>/.data (gitignored).
#
# Usage:
#   ./start.sh           # start everything that isn't already running (idempotent)
#   ./start.sh --status  # alias for ./status.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DATA="${REPO_ROOT}/.data"
MONGO_DB="${DATA}/mongo"
REDIS_DIR="${DATA}/redis"
CHROMA_DIR="${DATA}/chroma"
LOGS="${DATA}/logs"
PIDS="${DATA}/pids"
mkdir -p "$MONGO_DB" "$REDIS_DIR" "$CHROMA_DIR" "$LOGS" "$PIDS"

info() { echo "[local] $*"; }
pass() { echo "[local] PASS: $*"; }
fail() { echo "[local] FAIL: $*" >&2; exit 1; }

if [[ "${1:-}" == "--status" ]]; then exec "${SCRIPT_DIR}/status.sh"; fi

# ─── MongoDB (replica set rs0) ────────────────────────────────────────────────
if pgrep -x mongod >/dev/null 2>&1; then
  info "mongod already running"
else
  info "starting mongod (replSet rs0) on :27017 ..."
  mongod \
    --replSet rs0 \
    --bind_ip 127.0.0.1 \
    --port 27017 \
    --dbpath "$MONGO_DB" \
    --logpath "$LOGS/mongod.log" \
    --pidfilepath "$PIDS/mongod.pid" \
    --fork >/dev/null
  pass "mongod forked"
fi

# Wait for mongod to answer, then ensure the replica set is initiated.
info "waiting for mongod to accept connections ..."
for i in $(seq 1 30); do
  if mongosh --quiet --host 127.0.0.1 --port 27017 --eval 'db.adminCommand("ping").ok' >/dev/null 2>&1; then break; fi
  sleep 1
  [[ $i -eq 30 ]] && fail "mongod did not become reachable — see $LOGS/mongod.log"
done

rs_state="$(mongosh --quiet --host 127.0.0.1 --port 27017 --eval 'try { rs.status().myState } catch (e) { print(e.codeName || "NOINIT") }' 2>/dev/null | tail -1)"
if [[ "$rs_state" == "NotYetInitialized" || "$rs_state" == "NOINIT" ]]; then
  info "initiating single-node replica set rs0 ..."
  mongosh --quiet --host 127.0.0.1 --port 27017 --eval \
    'rs.initiate({_id:"rs0", members:[{_id:0, host:"127.0.0.1:27017"}]})' >/dev/null
fi
info "waiting for replica set PRIMARY ..."
for i in $(seq 1 30); do
  state="$(mongosh --quiet --host 127.0.0.1 --port 27017 --eval 'rs.status().myState' 2>/dev/null | tail -1)"
  [[ "$state" == "1" ]] && break
  sleep 1
  [[ $i -eq 30 ]] && fail "replica set did not reach PRIMARY"
done
pass "MongoDB ready (rs0 PRIMARY) :27017"

# ─── Redis ────────────────────────────────────────────────────────────────────
if redis-cli -p 6379 ping >/dev/null 2>&1; then
  info "redis already running"
else
  info "starting redis-server on :6379 ..."
  redis-server \
    --daemonize yes \
    --port 6379 \
    --dir "$REDIS_DIR" \
    --appendonly yes \
    --appendfsync everysec \
    --pidfile "$PIDS/redis.pid" \
    --logfile "$LOGS/redis.log"
fi
for i in $(seq 1 15); do
  redis-cli -p 6379 ping 2>/dev/null | grep -q PONG && break
  sleep 1
  [[ $i -eq 15 ]] && fail "redis did not respond to PING — see $LOGS/redis.log"
done
pass "Redis ready :6379"

# ─── Chroma (client-server) ───────────────────────────────────────────────────
# Validate the body, not just HTTP success: RunPod's proxy returns a 200 HTML
# error page for unbound ports, which would fool a status-only check.
chroma_alive() { curl -fsS "http://127.0.0.1:8765/api/v2/heartbeat" 2>/dev/null | grep -q heartbeat; }
if chroma_alive; then
  info "chroma already running"
else
  command -v chroma >/dev/null 2>&1 || fail "chroma CLI not found — pip install chromadb"
  info "starting chroma on :8765 (path $CHROMA_DIR) ..."
  ANONYMIZED_TELEMETRY=FALSE nohup chroma run \
    --host 127.0.0.1 \
    --port 8765 \
    --path "$CHROMA_DIR" \
    >"$LOGS/chroma.log" 2>&1 &
  echo $! >"$PIDS/chroma.pid"
fi
for i in $(seq 1 30); do
  chroma_alive && break
  sleep 1
  [[ $i -eq 30 ]] && fail "chroma heartbeat never responded — see $LOGS/chroma.log"
done
pass "Chroma ready :8765"

echo
pass "Local support stack is up. Connection settings:"
echo "    MONGO_URI=mongodb://localhost:27017/labmate?replicaSet=rs0"
echo "    REDIS_URL=redis://localhost:6379/0"
echo "    CHROMA_URL=http://localhost:8765  (CHROMA_HOST=localhost CHROMA_PORT=8765)"
echo "    -> source infrastructure/local/local.env to export these."
