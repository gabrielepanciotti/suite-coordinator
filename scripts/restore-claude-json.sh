#!/bin/sh
# Restore Claude Code's main config (.claude.json) after a container recreate.
# It lives at $HOME/.claude.json -- OUTSIDE the mounted ~/.claude dir -- so every
# `docker compose --force-recreate` (i.e. every deploy) wipes it, and the bot
# then dies with "Claude configuration file not found". Claude Code keeps
# timestamped backups inside the mounted .claude/backups/ (persistent), so we
# recover the most-recent real one before launching. Best-effort: it NEVER
# blocks startup (no `set -e`); on first boot there is no backup and Claude
# writes a fresh config itself. Stub backups (~50 bytes) are skipped.
CFG="${HOME}/.claude.json"
BAK_DIR="${HOME}/.claude/backups"
if [ ! -s "$CFG" ] && [ -d "$BAK_DIR" ]; then
  for f in $(ls -t "$BAK_DIR"/.claude.json.backup.* 2>/dev/null); do
    if [ "$(wc -c < "$f" 2>/dev/null || echo 0)" -ge 1000 ]; then
      cp "$f" "$CFG" 2>/dev/null && echo "[entrypoint] restored .claude.json from $f" >&2
      break
    fi
  done
fi
exec "$@"
