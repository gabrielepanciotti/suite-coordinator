#!/usr/bin/env bash
# One-time migration: build the dedicated (Syncthing-shared) Coordinator
# claude-home out of the current CLAUDE_HOST_DIR. Only the container-path
# transcript slugs matter (/workspaces/<key> -> -workspaces-<key>); desktop
# sessions stay in the real ~/.claude.
#
# Usage: scripts/utility/migrate-coordinator-home.sh <current-claude-dir> <new-home>
#   e.g. scripts/utility/migrate-coordinator-home.sh $HOME/.claude ${COORDINATOR_HOME}/.claude
set -euo pipefail

SRC="${1:?usage: migrate-coordinator-home.sh <current-claude-dir> <new-home>}"
DST="${2:?usage: migrate-coordinator-home.sh <current-claude-dir> <new-home>}"

mkdir -p "$DST/projects"

# Coordinator topic sessions: container-path slugs only.
shopt -s nullglob
copied=0
for d in "$SRC"/projects/-workspaces-*; do
  cp -r "$d" "$DST/projects/"
  copied=$((copied + 1))
done
echo "copied $copied transcript dir(s)"

# Same account on both homes: reuse the OAuth credentials (NOT synced — it sits
# outside projects/, and each device may refresh it independently).
if [ -f "$SRC/.credentials.json" ]; then
  cp "$SRC/.credentials.json" "$DST/"
  echo "copied .credentials.json"
else
  echo "WARN: no .credentials.json in $SRC — run 'claude login' against the new home"
fi

cat <<EOF

Next steps:
  1. Set CLAUDE_HOST_DIR=$DST in .env
  2. Share $DST/projects and <repo>/data via Syncthing with the peer device
  3. docker compose up -d --build bot
EOF
