#!/usr/bin/env bash
# Watches for the Docker engine to come back; when it does, stops the
# host-mode bot (PID of the listener on :8013) and starts the container.
# Prevents two getUpdates pollers on the same Telegram token (409).
set -u
cd "$(dirname "$0")/.."

until docker ps >/dev/null 2>&1; do sleep 15; done
echo "DOCKER_BACK $(date)"

# Kill host-mode bot: the process listening on the ownership port 8013
pid=$(netstat -ano 2>/dev/null | awk '/:8013 .*LISTENING/{print $5; exit}')
if [ -n "${pid:-}" ]; then
  taskkill //F //PID "$pid" >/dev/null 2>&1 && echo "host-bot pid $pid killed"
fi
# Belt & braces: also kill any python started by run-bot-host.sh via marker env
sleep 3

docker compose up -d 2>&1 | tail -3
sleep 15
docker compose logs --tail=20 bot 2>&1 | tail -20
