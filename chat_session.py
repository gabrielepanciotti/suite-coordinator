# chat_session.py
"""Pure helpers for the per-chat conversation behaviour of project groups/DMs.

No Telegram imports and no bot state: the session-day rule, the reply-to line
added to prompts, the topic-name prefix, the chat log writer and the
``/argomenti`` argument parser. ``bot.py`` wires them in.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import os
import re

logger = logging.getLogger("coordinator.chat_session")

REPLY_TO_MAX_CHARS = 1500


def _zone(tz_name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        return dt.UTC


def session_expired(
    last: dt.datetime,
    now: dt.datetime,
    *,
    ttl_sec: int,
    scope: str | None = None,
    tz_name: str = "Europe/Rome",
) -> bool:
    """Whether a session last used at ``last`` is stale at ``now``.

    Naive datetimes are read as the process' local time (that is how the bot
    stores them). The idle TTL always applies; ``scope="day"`` also expires a
    session last used on an earlier local day in ``tz_name``.
    """
    tz = _zone(tz_name)
    last_l = last.astimezone(tz)
    now_l = now.astimezone(tz)
    if (now_l - last_l).total_seconds() > ttl_sec:
        return True
    if scope == "day" and last_l.date() != now_l.date():
        return True
    return False


def reply_to_line(reply_msg, *, max_chars: int = REPLY_TO_MAX_CHARS) -> str | None:
    """``In risposta a (bot|<name>): «…»`` for a replied-to message with text.

    ``reply_msg`` is a Telegram Message (or anything with ``text``/``caption``/
    ``from_user``). None when there is nothing to quote. Forum-topic service
    messages (the implicit "reply" every message in a topic carries) have no
    text, so they yield None too.
    """
    if reply_msg is None:
        return None
    text = (getattr(reply_msg, "text", None) or getattr(reply_msg, "caption", None) or "").strip()
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    user = getattr(reply_msg, "from_user", None)
    if user is not None and getattr(user, "is_bot", False):
        who = "bot"
    elif user is not None:
        who = (getattr(user, "first_name", None) or getattr(user, "username", None)
               or str(getattr(user, "id", "?")))
    else:
        who = "sconosciuto"
    return f"In risposta a ({who}): «{text}»"


def topic_prefix(name: str | None) -> str:
    """``[Argomento: <name>]`` or empty string."""
    name = (name or "").strip()
    return f"[Argomento: {name}]" if name else ""


def parse_topic_names(args: str) -> list[str]:
    """``/argomenti a | b | c`` -> ["a", "b", "c"] (trimmed, deduplicated, ≤128 chars)."""
    out: list[str] = []
    for part in (args or "").split("|"):
        name = part.strip()[:128]
        if name and name not in out:
            out.append(name)
    return out


_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(s: str) -> str:
    """Telegram-HTML reply -> plain text for the log."""
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


def append_chat_log(
    workspace: str,
    rel_dir: str,
    *,
    when: dt.datetime,
    sender: str,
    user_text: str,
    reply_text: str,
    reply_to: str | None = None,
    topic: str | None = None,
    bot_label: str = "Tutor",
) -> str | None:
    """Append one turn to ``<workspace>/<rel_dir>/YYYY-MM-DD.md`` (``when``'s date).

    Never raises: an IO error is logged and None returned, the turn goes on.
    Returns the file path written.
    """
    try:
        folder = os.path.join(workspace, *rel_dir.split("/"))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{when:%Y-%m-%d}.md")
        new = not os.path.exists(path)
        head = f"### {when:%H:%M} — {sender}"
        if topic:
            head += f" [{topic}]"
        lines = [head, "", (user_text or "").strip() or "(nessun testo)"]
        if reply_to:
            lines.append(f"\n_({reply_to})_")
        lines += ["", f"**{bot_label}:**", "", (reply_text or "").strip(), "", ""]
        with open(path, "a", encoding="utf-8") as fh:
            if new:
                fh.write(f"# Conversazioni {when:%Y-%m-%d}\n\n")
            fh.write("\n".join(lines))
        return path
    except Exception as exc:  # the log must never break a turn
        logger.warning("chat log append failed (%s/%s): %s", workspace, rel_dir, exc)
        return None


class CallbackRegistry:
    """Short id -> payload, bounded (oldest dropped first).

    Inline keyboards can only carry 64 bytes of callback data; whatever does
    not fit stays here and the button carries the id. In memory on purpose: a
    stale id after a restart just asks the user to reopen /menu.
    """

    def __init__(self, cap: int = 2000, prefix: str | None = None) -> None:
        import secrets
        from collections import OrderedDict

        self._items: OrderedDict[str, dict] = OrderedDict()
        self._cap = cap
        self._next = 0
        # Random per process: after a restart the counter starts again at 1,
        # and without this an old keyboard's ids would land on new entries
        # (possibly of another chat). 6 hex + base36 counter stays far below
        # Telegram's 64-byte callback_data limit.
        self._prefix = prefix if prefix is not None else secrets.token_hex(3)

    def register(self, payload: dict) -> str:
        self._next += 1
        key = self._prefix + _b36(self._next)
        self._items[key] = payload
        while len(self._items) > self._cap:
            self._items.popitem(last=False)
        return key

    def get(self, key: str) -> dict | None:
        return self._items.get(key)


def _b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def topic_welcome(name: str) -> str:
    """First message posted in a topic created by /argomenti."""
    return (f"Qui parliamo di {name}. Scrivimi domande, audio delle lezioni "
            "o foto degli appunti di questo corso.")
