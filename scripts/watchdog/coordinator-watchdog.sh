#!/usr/bin/env bash
# External watchdog of coordinator-bot (Coordinator#76).
#
# Runs OUTSIDE the bot (systemd user timer): a presidio that dies in silence is
# worse than one that is off, and a monitor living inside the bot dies with it.
# Every run looks at the compose service `coordinator-bot` (by label, so a
# container renamed to `<hash>_coordinator-bot` by a half-done recreate is still
# found). Not `running` for FAIL_THRESHOLD consecutive runs (minutes) ->
# Telegram warning + recovery: start the existing Created/Exited container
# (renaming it back to the canonical name if needed), else `compose up -d`.
# At most MAX_RESTARTS attempts, then NEEDS HUMAN once and hands off.
# Recovery resets everything. Same Telegram config as network-watchdog.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE="${WATCHDOG_SERVICE:-coordinator-bot}"
PROJECT="${WATCHDOG_PROJECT:-coordinator}"
FAIL_THRESHOLD="${WATCHDOG_FAIL_THRESHOLD:-3}"
MAX_RESTARTS="${WATCHDOG_MAX_RESTARTS:-3}"
STATE_DIR="${WATCHDOG_STATE_DIR:-$HOME/.local/state/coordinator-watchdog}"
DRY_RUN="${WATCHDOG_DRY_RUN:-0}"
DOCKER="${WATCHDOG_DOCKER:-docker}"
mkdir -p "$STATE_DIR"

# shellcheck disable=SC1090
[ -f "$HOME/.config/network-watchdog.env" ] && . "$HOME/.config/network-watchdog.env"
TOKEN="${WATCHDOG_TELEGRAM_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -n "${WATCHDOG_TELEGRAM_TOKEN_FILE:-}" ]; then
  TOKEN="$(tr -d '\r\n ' < "$WATCHDOG_TELEGRAM_TOKEN_FILE" 2>/dev/null)"
fi
CHAT="${WATCHDOG_TELEGRAM_CHAT:-}"

notify() {
  echo "$(date -Is) $1"
  [ "$DRY_RUN" = 1 ] && return 0
  if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
    curl -fsS -m 10 -o /dev/null "https://api.telegram.org/bot${TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=Coordinator watchdog: $1" \
      || echo "$(date -Is) telegram send FAILED"
  else
    echo "$(date -Is) telegram not configured"
  fi
}

get() { cat "$STATE_DIR/$1" 2>/dev/null || echo 0; }
put() { echo "$2" > "$STATE_DIR/$1"; }

filters=(--filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$SERVICE")
# "<id> <status> <name>" for every container of the service, any state.
mapfile -t rows < <("$DOCKER" ps -a "${filters[@]}" --format '{{.ID}} {{.State}} {{.Names}}' 2>/dev/null)
running=0
for r in "${rows[@]}"; do
  [ "$(echo "$r" | cut -d' ' -f2)" = running ] && running=1
done

if [ "$running" = 1 ]; then
  if [ "$(get fails)" -ge "$FAIL_THRESHOLD" ] || [ "$(get restarts)" -gt 0 ]; then
    notify "$SERVICE e' di nuovo Up dopo $(get fails) controlli falliti e $(get restarts) tentativi di ripristino."
  fi
  put fails 0; put restarts 0; put escalated 0
  exit 0
fi

fails=$(( $(get fails) + 1 )); put fails "$fails"
echo "$(date -Is) $SERVICE not running ($fails): ${rows[*]:-nessun container}"
[ "$fails" -lt "$FAIL_THRESHOLD" ] && exit 0
[ $(( (fails - FAIL_THRESHOLD) % FAIL_THRESHOLD )) -ne 0 ] && exit 0

restarts="$(get restarts)"
if [ "$restarts" -ge "$MAX_RESTARTS" ]; then
  if [ "$(get escalated)" = 0 ]; then
    notify "NEEDS HUMAN: $SERVICE non e' Up da $fails minuti, $restarts tentativi non sono bastati. Serve un intervento a mano su dev-linux."
    put escalated 1
  fi
  exit 0
fi

put restarts $((restarts + 1))
notify "$SERVICE non e' Up da $fails minuti (${rows[0]:-nessun container}): ripristino (tentativo $((restarts + 1))/$MAX_RESTARTS)."
[ "$DRY_RUN" = 1 ] && exit 0

if [ "${#rows[@]}" -gt 0 ]; then
  id="$(echo "${rows[0]}" | cut -d' ' -f1)"
  name="$(echo "${rows[0]}" | cut -d' ' -f3)"
  # A half-done recreate leaves "<hash>_coordinator-bot": give the canonical name back.
  if [ "$name" != "$SERVICE" ] && ! "$DOCKER" inspect "$SERVICE" >/dev/null 2>&1; then
    "$DOCKER" rename "$id" "$SERVICE" >/dev/null 2>&1 || notify "docker rename $name fallito."
  fi
  "$DOCKER" start "$id" >/dev/null 2>&1 || notify "docker start $id fallito."
else
  (cd "$REPO" && "$DOCKER" compose up -d "$SERVICE" >/dev/null 2>&1) || notify "docker compose up -d $SERVICE fallito."
fi
sleep 5
state="$("$DOCKER" ps "${filters[@]}" --format '{{.State}}' 2>/dev/null | head -1)"
[ "$state" = running ] || echo "$(date -Is) dopo il ripristino lo stato e' '${state:-assente}'"
