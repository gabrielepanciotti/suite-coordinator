#!/usr/bin/env bash
# Hold a single-flight claim on a hub agent so the Coordinator will NOT spawn a
# headless twin while a live session (e.g. an interactive Claude on this box)
# handles that agent's bus traffic itself. Heartbeats every 30s until killed;
# the Coordinator honours the claim only while fresh (HUB_CLAIM_TTL, default 90s),
# so killing this script (or a crash) auto-releases the agent.
#
# Usage: scripts/hub-claim.sh <tenant> <agent_id> [data_dir]
#   data_dir defaults to the Coordinator's ./data (bind-mounted to /app/data).
set -euo pipefail
tenant="${1:?usage: hub-claim.sh <tenant> <agent_id> [data_dir]}"
agent="${2:?usage: hub-claim.sh <tenant> <agent_id> [data_dir]}"
data_dir="${3:-$(cd "$(dirname "$0")/.." && pwd)/data}"
claims="$data_dir/claims"
mkdir -p "$claims"
safe="${tenant}__${agent}"; safe="${safe//\//_}"
f="$claims/$safe.json"
trap 'rm -f "$f"; echo "[hub-claim] released $tenant/$agent"; exit 0' INT TERM
echo "[hub-claim] holding $tenant/$agent -> $f (Ctrl-C / kill to release)"
while true; do
  printf '{"pid":%d,"host":"%s","ts":%d}' "$$" "$(hostname)" "$(date +%s)" > "$f.tmp"
  mv "$f.tmp" "$f"
  sleep 30
done
