#!/usr/bin/env bash
# ⚠ STALE: assumes the pre-single-process split topology + Redis (separate
# services.orchestrator.main process, orchestrator.pid, a Redis consumer-group
# invariant). The local harness now runs as one process (services.local.main)
# backed by SQLite — this script will BREAK against that runtime. Needs a
# rewrite before use; left as-is (RunPod-only eval tooling, model currently
# off) rather than sunk-cost-fixed here. See Piece 7a brief item 6.
#
# Restart the orchestrator under a given SEQUENCING_MODE, then run the A/B harness.
# Usage: run_mode.sh <skill_first|react>
# Multi-trial: set TRIALS in the environment to run each case N times and score on
#   pass-rate (default 3; TRIALS=1 == single-shot). Example: TRIALS=5 run_mode.sh react
# RunPod-only: this script hardcodes /workspace/Labmate and the fixtures live under
#   /workspace/ (see run_seq_ab.py FIXTURES). On another host, run run_seq_ab.py directly.
set -uo pipefail
cd /workspace/Labmate
MODE="$1"
source infrastructure/local/local.env
source .data/creds.env
export PYTHONPATH="/workspace/Labmate"
export MCP_BRIDGE_ARGS="/workspace/Labmate/services/mcp-bridge/dist/index.js"
export SEQUENCING_MODE="$MODE"
export TRIALS="${TRIALS:-3}"   # per-case trials; pass-rate scoring (RunPod-only fixtures)

# Stop ALL existing orchestrators before starting a new one. A cooperative-SIGTERM
# orchestrator blocked mid-generation outlives a plain `kill`, and prior runs have
# leaked untracked instances; multiple orchestrators share the Redis consumer group
# and would silently split (and cross-contaminate) this run's trials with the wrong
# env config. The module-path pattern cannot match this script's own argv
# (`bash .../run_mode.sh <mode>`), so pgrep/pkill are safe here.
ORCH_PAT='services\.orchestrator\.main'
if pgrep -f "$ORCH_PAT" >/dev/null; then
  echo "[$MODE] stopping orchestrator(s): $(pgrep -f "$ORCH_PAT" | tr '\n' ' ')"
  kill $(pgrep -f "$ORCH_PAT") 2>/dev/null
  for _ in $(seq 1 30); do pgrep -f "$ORCH_PAT" >/dev/null || break; sleep 1; done
  if pgrep -f "$ORCH_PAT" >/dev/null; then
    echo "[$MODE] SIGTERM timed out — SIGKILL $(pgrep -f "$ORCH_PAT" | tr '\n' ' ')"
    kill -9 $(pgrep -f "$ORCH_PAT") 2>/dev/null; sleep 1
  fi
fi
rm -f .data/pids/orchestrator.pid

# Pre-flight guard: refuse to start on top of a survivor (would corrupt the A/B).
if pgrep -f "$ORCH_PAT" >/dev/null; then
  echo "[$MODE] FATAL: orchestrator(s) survived reap: $(pgrep -f "$ORCH_PAT" | tr '\n' ' ')" >&2
  exit 1
fi

nohup python -m services.orchestrator.main > .data/logs/orchestrator-$MODE.log 2>&1 &
echo $! > .data/pids/orchestrator.pid
NEW=$(cat .data/pids/orchestrator.pid)
echo "[$MODE] orchestrator pid $NEW"

# Pre-flight guard: confirm EXACTLY ONE orchestrator now serves the Redis group.
sleep 1
N_ORCH=$(pgrep -f "$ORCH_PAT" | wc -l | tr -d ' ')
if [ "$N_ORCH" -ne 1 ]; then
  echo "[$MODE] FATAL: expected exactly 1 orchestrator, found $N_ORCH: $(pgrep -f "$ORCH_PAT" | tr '\n' ' ')" >&2
  exit 1
fi

for _ in $(seq 1 90); do
  grep -qE "skill router ready" ".data/logs/orchestrator-$MODE.log" 2>/dev/null && break
  kill -0 "$NEW" 2>/dev/null || { echo "[$MODE] orchestrator died"; tail -10 ".data/logs/orchestrator-$MODE.log"; exit 1; }
  sleep 2
done
echo "[$MODE] orchestrator ready; running harness"
echo "[$MODE] TRIALS=$TRIALS"
python eval/seq_ab/run_seq_ab.py "$MODE"
echo "[$MODE] DONE"
