# dm_replies.py
"""Route a person's private reply back to the node that asked (issue #55).

A node can ask a person something through the bot: a ``destination: dm``
notification lands in that person's private chat. Until #55 the answer went
nowhere — it was archived in ``messages.jsonl`` and that was the end of it. The
person believed they had answered, the node believed they had not, and Health
kept a plan in draft for four days waiting for a reply that had already arrived.

This module is the missing link, and it is a leaf (no bot/hub imports) so the
scheduler can call it without creating a cycle:

- ``record_sent`` — called by the scheduler after a DM delivery. It remembers,
  per private chat, *which notification* was sent and *which node* owns it.
- ``pending_for`` — called by the bot on an incoming private message: is there
  an open question this text is the answer to?
- ``mark_forwarded`` / ``log_reply`` — bookkeeping after routing.

Which node owns a notification: ``params.reply_to`` if set (a node key, or
``false`` to opt out), else ``params.project``, else the key prefix before the
first ``-`` — the same fallback ``overview_snapshot._project_of`` uses, so the
node that owns a notification for the pause gate owns its replies too.

Who is asked: only non-admin recipients by default. An admin's private chat is
their console with the bot (bound sessions, commands); every agent-provider
digest they receive would otherwise hijack it for days. A notification that
really wants the admin's answer declares ``params.reply_to`` explicitly.

How long a question stays open: ``DM_REPLY_TTL_SEC`` after it was sent (default
72h). Once the person has answered, follow-ups keep going to the same node for
``DM_REPLY_FOLLOWUP_SEC`` (default 1h) after the last forwarded message — then
the chat is theirs again. A person who re-sends the same answer (what a member did)
therefore reaches the node twice instead of zero times.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

from atomic_io import replace_with_retry

logger = logging.getLogger("coordinator.dm_replies")

PENDING_PATH = os.environ.get("DM_PENDING_PATH", "/app/data/dm_pending.json")
REPLIES_PATH = os.environ.get("DM_REPLIES_PATH", "/app/data/dm_replies.jsonl")
TTL_SEC = int(os.environ.get("DM_REPLY_TTL_SEC", str(72 * 60 * 60)))
FOLLOWUP_SEC = int(os.environ.get("DM_REPLY_FOLLOWUP_SEC", str(60 * 60)))
# The question travels with the reply so the node can tell which one it was;
# bounded because a digest-sized notification would drown the answer.
QUESTION_MAX_CHARS = 1500


def reply_node(notif) -> str | None:
    """The node key that owns replies to ``notif``, or ``None`` if opted out."""
    params = notif.params or {}
    if "reply_to" in params:
        # Declared: a node key routes there; false/empty opts out. A missing
        # key is not an opt-out — it falls through to the derivation below.
        v = params["reply_to"]
        if isinstance(v, str) and v.strip() and v.strip().lower() not in ("false", "no", "off"):
            return v.strip()
        return None
    project = params.get("project")
    if isinstance(project, str) and project.strip():
        return project.strip()
    return notif.key.split("-", 1)[0] or None


def _load() -> dict[str, dict]:
    try:
        with open(PENDING_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("could not read %s", PENDING_PATH)
        return {}


def _save(data: dict[str, dict]) -> None:
    d = os.path.dirname(PENDING_PATH)
    if d and not os.path.isdir(d):
        return  # no writable data dir (e.g. under tests)
    tmp = PENDING_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        replace_with_retry(tmp, PENDING_PATH)
    except Exception:
        logger.exception("could not persist %s", PENDING_PATH)


def record_sent(notif, chat_id: int, text: str, *, is_admin: bool, now: datetime) -> bool:
    """Remember that ``notif`` was delivered to the private chat ``chat_id``.

    Returns True when a question was opened. A later send to the same chat
    replaces the previous one: the person answers the last thing they read.
    """
    node = reply_node(notif)
    if node is None:
        return False
    if is_admin and "reply_to" not in (notif.params or {}):
        return False
    data = _load()
    data[str(chat_id)] = {
        "key": notif.key,
        "title": notif.title,
        "node": node,
        "sent_at": now.isoformat(timespec="seconds"),
        "question": (text or "")[:QUESTION_MAX_CHARS],
        "forwarded_at": None,
    }
    _save(data)
    return True


def _is_open(entry: dict, now: datetime) -> bool:
    try:
        sent = datetime.fromisoformat(entry["sent_at"])
    except (KeyError, TypeError, ValueError):
        return False
    if now - sent > timedelta(seconds=TTL_SEC):
        return False
    fwd = entry.get("forwarded_at")
    if fwd:
        try:
            last = datetime.fromisoformat(fwd)
        except (TypeError, ValueError):
            return False
        return now - last <= timedelta(seconds=FOLLOWUP_SEC)
    return True


def pending_for(chat_id: int, now: datetime) -> dict | None:
    """The open question in ``chat_id``, or ``None``. Expired entries are dropped."""
    data = _load()
    entry = data.get(str(chat_id))
    if entry is None:
        return None
    if not _is_open(entry, now):
        del data[str(chat_id)]
        _save(data)
        return None
    return entry


def mark_forwarded(chat_id: int, now: datetime) -> None:
    """Start (or extend) the follow-up window after a reply reached its node."""
    data = _load()
    entry = data.get(str(chat_id))
    if entry is None:
        return
    entry["forwarded_at"] = now.isoformat(timespec="seconds")
    _save(data)


def log_reply(rec: dict) -> None:
    """Append one routed reply (and how it was routed) to REPLIES_PATH.

    This is the ledger the node — or the owner — reads when the bus was not
    reachable: ``GET /api/dm-replies`` serves it. Best-effort, never raises.
    """
    d = os.path.dirname(REPLIES_PATH)
    if d and not os.path.isdir(d):
        return
    try:
        with open(REPLIES_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("could not append to %s", REPLIES_PATH)


def read_replies(node: str | None = None, limit: int = 100) -> list[dict]:
    """Most recent routed replies, newest last, optionally for one node."""
    try:
        with open(REPLIES_PATH, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []
    except Exception:
        logger.exception("could not read %s", REPLIES_PATH)
        return []
    if node:
        rows = [r for r in rows if r.get("node") == node]
    return rows[-limit:]


def forward_body(entry: dict, who: str, text: str) -> str:
    """The bus message the node receives: the answer first, the question after."""
    return (
        f"Risposta di {who} alla notifica «{entry.get('title') or entry.get('key')}» "
        f"(`{entry.get('key')}`, inviata {entry.get('sent_at')}), "
        f"arrivata in privato al bot:\n\n{text}\n\n"
        f"— Domanda originale:\n{entry.get('question') or '(non registrata)'}"
    )
