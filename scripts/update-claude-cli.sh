#!/bin/sh
# Keep the Claude Code CLI current at every container start, then exec "$@".
# The image bakes a CLI at build time, and a long-lived container would stay on
# it for weeks: the woken sessions then run an old CLI (old default model, old
# fixes) while every host has moved on. Here we compare the CLI on PATH with
# the registry's latest and, if they differ, install the new one into a prefix
# owned by the app user ($HOME/.npm-cli/bin is first on PATH, see Dockerfile).
# Best-effort: it NEVER blocks startup (no `set -e`). No network, a slow
# registry or a failed install leave the baked CLI in place and say so.
# CLAUDE_CLI_AUTOUPDATE=0 in .env turns it off.
PREFIX="${HOME}/.npm-cli"
# Put the prefix first on PATH here, not only in the Dockerfile: an image built
# before that ENV line runs this (bind-mounted) script too, and there an update
# landed in ~/.npm-cli while the process kept running the old baked CLI.
case ":${PATH}:" in
  *":${PREFIX}/bin:"*) ;;
  *) PATH="${PREFIX}/bin:${PATH}"; export PATH ;;
esac
if [ "${CLAUDE_CLI_AUTOUPDATE:-1}" = "1" ]; then
  current=$(claude --version 2>/dev/null | cut -d' ' -f1)
  latest=$(timeout 20 npm view @anthropic-ai/claude-code version 2>/dev/null)
  if [ -z "$latest" ]; then
    echo "[entrypoint] claude cli: registry unreachable, staying on ${current:-?}" >&2
  elif [ "$latest" != "$current" ]; then
    if timeout 300 npm install -g --prefix "$PREFIX" --no-fund --no-audit \
         "@anthropic-ai/claude-code@${latest}" >/dev/null 2>&1; then
      now=$(claude --version 2>/dev/null | cut -d' ' -f1)
      if [ "$now" = "$latest" ]; then
        echo "[entrypoint] claude cli updated ${current:-?} -> ${latest}" >&2
      else
        echo "[entrypoint] claude cli: ${latest} installed but PATH resolves ${now:-?}" >&2
      fi
    else
      echo "[entrypoint] claude cli: update to ${latest} FAILED, staying on ${current:-?}" >&2
    fi
  else
    echo "[entrypoint] claude cli ${current} is current" >&2
  fi
fi
exec "$@"
