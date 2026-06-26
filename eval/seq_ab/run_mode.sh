#!/usr/bin/env bash
# Restart the orchestrator under a given SEQUENCING_MODE, then run the A/B harness.
# Usage: run_mode.sh <skill_first|react|replan>
set -uo pipefail
cd /workspace/Labmate
MODE="$1"
source infrastructure/local/local.env
source .data/creds.env
export PYTHONPATH="/workspace/Labmate"
export MCP_BRIDGE_ARGS="/workspace/Labmate/services/mcp-bridge/dist/index.js"
export SEQUENCING_MODE="$MODE"

# stop existing orchestrator by pidfile (never pkill -f — it matches this script)
if [ -f .data/pids/orchestrator.pid ]; then
  kill "$(cat .data/pids/orchestrator.pid)" 2>/dev/null
  for _ in $(seq 1 30); do kill -0 "$(cat .data/pids/orchestrator.pid)" 2>/dev/null || break; sleep 1; done
fi

nohup python -m services.orchestrator.main > .data/logs/orchestrator-$MODE.log 2>&1 &
echo $! > .data/pids/orchestrator.pid
NEW=$(cat .data/pids/orchestrator.pid)
echo "[$MODE] orchestrator pid $NEW"

for _ in $(seq 1 90); do
  grep -qE "skill router ready" ".data/logs/orchestrator-$MODE.log" 2>/dev/null && break
  kill -0 "$NEW" 2>/dev/null || { echo "[$MODE] orchestrator died"; tail -10 ".data/logs/orchestrator-$MODE.log"; exit 1; }
  sleep 2
done
echo "[$MODE] orchestrator ready; running harness"
python eval/seq_ab/run_seq_ab.py "$MODE"
echo "[$MODE] DONE"
