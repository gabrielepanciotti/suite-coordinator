#!/usr/bin/env bash
# Host-mode fallback launcher: runs the Coordinator bot DIRECTLY on Windows
# (no Docker) when Docker Desktop/WSL is wedged. Same data/ dir and same
# claude session store as the container (CLAUDE_CONFIG_DIR=coordinator-home),
# host-path registry via COORDINATOR_PROJECTS_CONFIG=projects.host.json.
# NOTE: stop this process BEFORE `docker compose up -d` (two getUpdates
# pollers on one token = Telegram 409).
set -euo pipefail
shopt -s extglob
cd "$(dirname "$0")/.."

# Load .env (strip CRLF, skip comments/blanks, drop dotenv-style inline
# comments on unquoted values — same as docker compose env_file)
while IFS= read -r line; do
  line="${line%$'\r'}"
  [[ -z "$line" || "$line" == \#* ]] && continue
  val="${line#*=}"
  if [[ "$val" != \"* && "$val" != \'* ]]; then
    line="${line%%+([[:space:]])#*}"   # needs extglob
    line="${line%"${line##*[![:space:]]}"}"  # rtrim
  fi
  export "$line"
done < .env

# Host-mode overrides
export COORDINATOR_PROJECTS_CONFIG="$PWD/projects.host.json"
export STATE_PATH="$PWD/data/state.json"
export CONV_LOG_PATH="$PWD/data/conversations.jsonl"
export NOTIF_PATH="$PWD/data/notifications.json"
# Same session store + OAuth credentials the container uses
# coordinator-home sta accanto al repo (<SrvRoot>/coordinator-home): dedotto da
# $PWD, che qui e' gia' la root del repo (cd sopra), invece che hardcodato.
export CLAUDE_CONFIG_DIR="$(dirname "$PWD")/coordinator-home/.claude"
# Mirror compose service env
export MAX_THINKING_TOKENS=0
export CLAUDE_MAX_CONCURRENCY="${CLAUDE_MAX_CONCURRENCY:-3}"
export CLAUDE_LIVE_SESSIONS=1
export CLAUDE_MAX_LIVE_SESSIONS="${CLAUDE_MAX_LIVE_SESSIONS:-4}"
export HUB_LOCAL_AGENTS="${BOT_LOCAL_AGENTS:-}"

exec ./.venv/Scripts/python.exe bot.py
