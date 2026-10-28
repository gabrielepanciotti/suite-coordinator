# notif_store.py
"""Persistence for M4 notifications: data/notifications.json, atomic writes.

Gemello di bot.py's state persistence. Best-effort: a missing/unwritable data
dir keeps the bot running (tests, fresh installs) without raising.
"""

from __future__ import annotations

import json
import logging
import os

from atomic_io import replace_with_retry
from notifications import Notification

logger = logging.getLogger("coordinator.notif_store")

NOTIF_PATH = os.environ.get("NOTIF_PATH", "/app/data/notifications.json")
# Last successful trigger time per notification key (ISO 8601). Powers the
# scheduler's startup catch-up: a trigger missed while the bot was down is
# re-fired once if it still falls inside the recovery window.
RUNS_PATH = os.environ.get("NOTIF_RUNS_PATH", "/app/data/notif_runs.json")
# Outcome ledger per notification key: WHAT happened on every fire, not just
# WHEN it was triggered. RUNS_PATH above records the trigger even when the fire
# was dropped by the policy gate — which is precisely why 14 mute notifications
# looked alive for a month: "last_run" was fresh, and nothing recorded that the
# run had sent nothing. This file keeps the outcome, its category, the last time
# the notification actually REACHED someone (last_sent_at), and a first_seen
# baseline so a brand-new notification is not accused of being silent.
#
# It is a SEPARATE file from notifications.json on purpose: notif_sync
# .sync_from_manifests() upserts whole Notification objects from the nodes'
# coordinator.toml at every boot, so anything stored inside a Notification
# (params included) is silently overwritten. History kept here survives.
OUTCOMES_PATH = os.environ.get("NOTIF_OUTCOMES_PATH", "/app/data/notif_outcomes.json")
# How many recent fires to keep per key (bounded: this file is read by an HTTP
# endpoint and must not grow without limit).
RECENT_MAX = 20


def default_instance_id() -> str | None:
    """L'istanza di default del device, o None in legacy/framework.

    Le righe scritte prima della multi-istanza non portano ``instance_id``: si
    etichettano qui con l'istanza che le ha prodotte *oggi* (contratto §4.2,
    migrazione additiva). Un registro assente non e' un errore: None.
    """
    import instances  # local import: keeps notif_store importable standalone

    try:
        return instances.default_instance_id()
    except Exception:
        logger.warning("could not resolve the default instance", exc_info=True)
        return None


def _seed() -> list[Notification]:
    """No hardcoded defaults (2026-07-31).

    This instance is life-work: nothing outside work instance belongs in it. The old
    seed shipped three personal-Life rows (growbox, personalChef) that came back
    on every empty store, so deleting them never stuck. Notifications now come
    only from the project manifests via notif_sync.sync_from_manifests().
    """
    return []


def load() -> list[Notification]:
    try:
        with open(NOTIF_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception:
        logger.exception("could not load notifications from %s", NOTIF_PATH)
        return []
    out: list[Notification] = []
    fallback = None
    for d in data.get("notifications", []):
        try:
            notif = Notification.from_dict(d)
        except Exception:
            logger.warning("skipping malformed notification: %r", d)
            continue
        if notif.instance_id is None:
            # Additive backfill: a row without instance_id is of the instance
            # that produced it, i.e. the device default at migration time.
            if fallback is None:
                fallback = default_instance_id()
            notif.instance_id = fallback
        out.append(notif)
    return out


def save(items: list[Notification]) -> None:
    d = os.path.dirname(NOTIF_PATH)
    if d and not os.path.isdir(d):
        return  # no writable data dir -> stay in-memory (tests / fresh install)
    fallback = None
    for n in items:
        if n.instance_id is None:
            if fallback is None:
                fallback = default_instance_id()
            n.instance_id = fallback
    try:
        payload = {"notifications": [n.to_dict() for n in items]}
        tmp = NOTIF_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        replace_with_retry(tmp, NOTIF_PATH)
    except Exception:
        logger.exception("could not persist notifications to %s", NOTIF_PATH)


def get(key: str) -> Notification | None:
    return next((n for n in load() if n.key == key), None)


def upsert(notif: Notification) -> None:
    items = [n for n in load() if n.key != notif.key]
    items.append(notif)
    save(items)


def delete(key: str) -> bool:
    items = load()
    rest = [n for n in items if n.key != key]
    if len(rest) == len(items):
        return False
    save(rest)
    return True


def remap_chat(old_id: int, new_id: int) -> list[str]:
    """Point every ``chat:<old_id>`` destination at ``new_id``. Returns the keys moved.

    Telegram changes a group's id when it is promoted to a supergroup — a thing
    that happens on its own, months later, when someone enables topics or the
    member count grows. Every notification aimed at that chat then fails
    forever, and the way you notice is that a report stops arriving: silence,
    which reads exactly like "nothing to report".

    The thread suffix is dropped on purpose: a basic group has no topics, so a
    ``chat:<id>:<thread>`` destination cannot survive as-is, and the supergroup's
    General topic is thread 0.
    """
    items = load()
    moved: list[str] = []
    for notif in items:
        dest = (notif.destination or "").strip()
        if not dest.lower().startswith("chat:"):
            continue
        parts = dest.split(":")
        if len(parts) < 2 or parts[1].strip() != str(old_id):
            continue
        notif.destination = f"chat:{new_id}"
        moved.append(notif.key)
    if moved:
        save(items)
    return moved


def load_runs() -> dict[str, str]:
    """Map notification key -> ISO timestamp of its last fired trigger."""
    try:
        with open(RUNS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("could not load notif runs from %s", RUNS_PATH)
        return {}
    return data if isinstance(data, dict) else {}


def record_run(key: str, when_iso: str) -> None:
    """Persist ``key``'s last fired trigger time (atomic, best-effort)."""
    d = os.path.dirname(RUNS_PATH)
    if d and not os.path.isdir(d):
        return  # no writable data dir -> stay in-memory (tests / fresh install)
    runs = load_runs()
    runs[key] = when_iso
    try:
        tmp = RUNS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(runs, fh, ensure_ascii=False)
        replace_with_retry(tmp, RUNS_PATH)
    except Exception:
        logger.exception("could not persist notif runs to %s", RUNS_PATH)


# --- outcome ledger --------------------------------------------------------


def load_outcomes() -> dict[str, dict]:
    """Map notification key -> outcome record (see ``record_outcome``)."""
    try:
        with open(OUTCOMES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("could not load notif outcomes from %s", OUTCOMES_PATH)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save_outcomes(data: dict[str, dict]) -> bool:
    d = os.path.dirname(OUTCOMES_PATH)
    if d and not os.path.isdir(d):
        return False  # no writable data dir -> stay in-memory (tests / fresh install)
    try:
        tmp = OUTCOMES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        replace_with_retry(tmp, OUTCOMES_PATH)
        return True
    except Exception:
        logger.exception("could not persist notif outcomes to %s", OUTCOMES_PATH)
        return False


def _blank_record(when_iso: str) -> dict:
    return {"first_seen": when_iso, "last_attempt_at": None, "last_outcome": None,
            "last_category": None, "last_sent_at": None, "totals": {}, "recent": []}


def record_outcome(key: str, outcome: str, when_iso: str) -> None:
    """Persist WHAT happened on one fire of ``key`` (atomic, best-effort).

    Updates ``last_attempt_at`` on every call and ``last_sent_at`` only when the
    outcome means the message actually went out. Never raises: a failure to
    record must not take a notification down with it.
    """
    import notifications  # local import: notifications imports nothing from here

    try:
        category = notifications.classify_outcome(outcome)
        data = load_outcomes()
        rec = data.get(key) or _blank_record(when_iso)
        rec.setdefault("first_seen", when_iso)
        rec["last_attempt_at"] = when_iso
        rec["last_outcome"] = outcome
        rec["last_category"] = category
        if category == notifications.CAT_SENT:
            rec["last_sent_at"] = when_iso
        totals = dict(rec.get("totals") or {})
        totals[category] = int(totals.get(category, 0)) + 1
        rec["totals"] = totals
        recent = list(rec.get("recent") or [])
        recent.append({"at": when_iso, "outcome": outcome, "category": category})
        rec["recent"] = recent[-RECENT_MAX:]
        data[key] = rec
        _save_outcomes(data)
    except Exception:
        logger.exception("could not record outcome for %s", key)


def note_seen(keys: list[str] | set[str] | tuple[str, ...], when_iso: str) -> list[str]:
    """Give never-fired notifications a ``first_seen`` baseline. Returns new keys.

    Without this, a notification created five minutes ago is indistinguishable
    from one that has been mute since March — both have no send on record — and
    the watchdog would cry wolf on every freshly created notification. Called by
    the health report itself, so the baseline exists from the first observation.
    """
    try:
        data = load_outcomes()
        added = [k for k in keys if k not in data]
        if not added:
            return []
        for k in added:
            data[k] = _blank_record(when_iso)
        return added if _save_outcomes(data) else []
    except Exception:
        logger.exception("could not note first_seen baseline")
        return []


def seed_if_empty() -> list[Notification]:
    items = load()
    if not items:
        items = _seed()
        save(items)
    return items
