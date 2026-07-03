#!/usr/bin/env bash
# One-flag A/B: run the fixed seq_ab case set twice — FLAG=default vs FLAG=value —
# freezing model+code+fixtures, then print the CI-gated win verdict.
#
# Usage: FLAG=ROUTE_EDIT_TO_REACT DEFAULT=1 VALUE=0 TRIALS=3 bash eval/seq_ab/run_flag_ab.sh
# RunPod-only: hardcodes /workspace/Labmate like run_mode.sh. Adjust paths elsewhere.
set -euo pipefail

: "${FLAG:?set FLAG=NAME}"; : "${DEFAULT:?set DEFAULT=value}"; : "${VALUE:?set VALUE=value}"
TRIALS="${TRIALS:-3}"; MODE="${SEQUENCING_MODE:-skill_first}"
REPO=/workspace/Labmate; cd "$REPO"

run_arm () {  # $1 = flag value, $2 = out-tag
  echo "== arm $FLAG=$1 =="
  bash infrastructure/local/stop.sh >/dev/null 2>&1 || true
  env "$FLAG=$1" SEQUENCING_MODE="$MODE" bash infrastructure/local/start.sh
  sleep 5
  env TRIALS="$TRIALS" python -m eval.seq_ab.run_seq_ab "$MODE"
  mv "eval/seq_ab/results-$MODE.json" "eval/seq_ab/results-flagab-$2.json"
}

run_arm "$DEFAULT" "default"
run_arm "$VALUE" "variant"
python -m eval.seq_ab.compare \
  eval/seq_ab/results-flagab-default.json \
  eval/seq_ab/results-flagab-variant.json
