#!/usr/bin/env bash
# pre-push guard: refuse a direct push to main — open a PR from a feature branch.
# Client-side stand-in for branch protection (the repo is private on the free
# plan, where GitHub rulesets are unavailable). Bypass for emergencies:
#   git push --no-verify
#
# pre-commit's pre-push stage passes the refs being pushed on stdin as:
#   <local_ref> <local_sha> <remote_ref> <remote_sha>
set -euo pipefail
protected="refs/heads/main"
blocked=0
while read -r _local_ref _local_sha remote_ref _remote_sha; do
  if [[ "$remote_ref" == "$protected" ]]; then
    blocked=1
  fi
done
if [[ "$blocked" -eq 1 ]]; then
  echo "✋ Direct push to 'main' is blocked — open a PR from a feature branch." >&2
  echo "   Emergency override: git push --no-verify" >&2
  exit 1
fi
exit 0
