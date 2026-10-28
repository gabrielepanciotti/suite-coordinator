"""Detach every configured bot from the cloud Bot API (``logOut``).

Required once before a bot can be served by the local Bot API server. Idempotent:
a second ``logOut`` is refused by Telegram and reported as "already done".
Tokens are never printed. Run from the repo root:
``python scripts/telegram_local_migrate.py [--dry-run]``.
Other modes: ``--check-local URL`` (getMe on the local server per bot, exit 1 on
any ko) and ``--logout-from URL`` (logOut on the LOCAL server, rollback step).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_registry  # noqa: E402
import telegram_api  # noqa: E402

CLOUD = "https://api.telegram.org"


def bot_tokens(env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """``[(name, token)]``: the default bot first, then COORDINATOR_BOTS extras."""
    env = os.environ if env is None else env
    out: list[tuple[str, str]] = []
    default = bot_registry.default_token(env)
    if default:
        out.append((bot_registry.DEFAULT_BOT, default))
    # A bot routed to the cloud (TELEGRAM_BOT_API_URL_<NAME>=cloud) must never be
    # logged out of it: that is exactly what would break it.
    out.extend(
        (c.name, c.token)
        for c in bot_registry.load_extra_bots(env)
        if telegram_api.api_server_config(env, c.name) is not None
    )
    return out


def _call(root: str, token: str, method: str, timeout: float = 15.0) -> tuple[int, str]:
    """POST ``method`` and return (http status, Telegram ``description``); 0 = no answer.

    The description is read from the JSON body, also of HTTP errors. Never
    includes the token.
    """
    req = urllib.request.Request(f"{root.rstrip('/')}/bot{token}/{method}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
        return 200, str(body.get("description") or ("ok" if body.get("ok") else ""))
    except urllib.error.HTTPError as exc:
        try:
            desc = str(json.load(exc).get("description") or "")
        except Exception:  # noqa: BLE001 - body is optional
            desc = ""
        return exc.code, desc
    except Exception as exc:  # noqa: BLE001 - report the type, never the URL (token)
        return 0, type(exc).__name__


def log_out(token: str, root: str = CLOUD, timeout: float = 15.0) -> str:
    """'ok' | 'maybe' (400: probably already logged out) | 'error: ...'.

    Telegram does not document the exact answer to a repeated logOut, so only
    HTTP 400 is treated as "probably already done" (verify with --check-local).
    401/404 mean an invalid, revoked or malformed token: that is an error.
    """
    status, desc = _call(root, token, "logOut", timeout)
    if status == 200:
        return "ok"
    if status == 400:
        return "maybe"
    return f"error: HTTP {status}" + (f" ({desc})" if desc else "")


def check_local(token: str, local_url: str, timeout: float = 15.0) -> str:
    """'ok' if the local server answers getMe for this token, else 'ko: ...'."""
    status, desc = _call(local_url, token, "getMe", timeout)
    return "ok" if status == 200 else f"ko: HTTP {status}" + (f" ({desc})" if desc else "")


USAGE = ("usage: telegram_local_migrate.py [--dry-run] "
         "[--check-local URL | --logout-from URL]")


class UsageError(ValueError):
    pass


def _option(argv: list[str], flag: str) -> str | None:
    """Value of ``flag`` (None if absent); a missing or flag-like value is a UsageError."""
    if flag not in argv:
        return None
    i = argv.index(flag) + 1
    if i >= len(argv) or not argv[i] or argv[i].startswith("-"):
        raise UsageError(f"{flag} needs a URL")
    return argv[i]


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    # Validate before any network call: a bad --check-local/--logout-from must
    # never fall through to a real logOut on the cloud.
    try:
        local = _option(argv, "--check-local")
        back = _option(argv, "--logout-from")
    except UsageError as exc:
        print(exc, USAGE, sep="\n", file=sys.stderr)
        return 2
    bots = bot_tokens()
    if not bots:
        print("no bot token configured")
        return 1
    failed = 0
    for name, token in bots:
        if local:
            verdict = check_local(token, local)
            failed += verdict != "ok"
            print(f"{name}: local getMe {verdict}")
        elif dry:
            print(f"{name}: would logOut from {back or CLOUD}")
        else:
            verdict = log_out(token, back or CLOUD)
            failed += verdict.startswith("error")
            label = {"ok": "logged out", "maybe": "probably already done - verify with --check-local"}
            print(f"{name}: {label.get(verdict, verdict)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
