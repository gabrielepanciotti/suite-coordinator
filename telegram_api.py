"""Optional local Telegram Bot API server (lifts the 20 MB cloud download cap).

Everything is env-gated: without ``TELEGRAM_BOT_API_URL`` the bot talks to the
cloud API exactly as before. With it, the PTB builder is pointed at the local
server (``local_mode`` is on unless ``TELEGRAM_LOCAL_MODE`` says otherwise).
"""

from __future__ import annotations

import os
import re

URL_ENV = "TELEGRAM_BOT_API_URL"
LOCAL_MODE_ENV = "TELEGRAM_LOCAL_MODE"
CLOUD_ROOT = "https://api.telegram.org"

_FALSY = {"0", "false", "no", "off"}


def _suffix(bot: str) -> str:
    """``<NAME>`` of the per-bot overrides, same spelling as TELEGRAM_BOT_TOKEN_<NAME>."""
    return re.sub(r"[^A-Z0-9]", "_", bot.upper())


def api_server_config(env: dict[str, str] | None = None, bot: str = "") -> dict | None:
    """``{"url": ..., "local_mode": bool}`` when a local server is configured.

    ``bot`` names a node bot with its own routing: ``TELEGRAM_BOT_API_URL_<BOT>``
    wins over the general URL, and ``cloud`` (or empty) sends that bot straight to
    the cloud API. Needed for a bot whose polling must survive the host of the
    local server going down (the local server is a single machine).
    """
    env = os.environ if env is None else env
    key = f"{URL_ENV}_{_suffix(bot)}" if bot else ""
    if key and key in env:
        url = (env.get(key) or "").strip().rstrip("/")
        if url.lower() in ("", "cloud"):
            return None
        mode_raw = env.get(f"{LOCAL_MODE_ENV}_{_suffix(bot)}") or env.get(LOCAL_MODE_ENV) or "1"
    else:
        url = (env.get(URL_ENV) or "").strip().rstrip("/")
        if not url:
            return None
        mode_raw = env.get(LOCAL_MODE_ENV) or "1"
    return {"url": url, "local_mode": mode_raw.strip().lower() not in _FALSY}


def apply_api_server(builder, env: dict[str, str] | None = None, bot: str = ""):
    """Point a PTB ``ApplicationBuilder`` at the local server, if configured."""
    conf = api_server_config(env, bot)
    if conf is None:
        return builder
    builder = builder.base_url(f"{conf['url']}/bot").base_file_url(f"{conf['url']}/file/bot")
    if conf["local_mode"]:
        builder = builder.local_mode(True)
    return builder


def api_root(env: dict[str, str] | None = None, bot: str = "") -> str:
    conf = api_server_config(env, bot)
    return conf["url"] if conf else CLOUD_ROOT
