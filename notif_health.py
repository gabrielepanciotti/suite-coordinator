# notif_health.py
"""Watchdog over the notification subsystem: who has stopped sending, and why.

The failure this module exists for: a notification stops going out and nothing
changes colour. No exception, no red status — just an absence, and an absence
looks exactly like "there was nothing to say". Fourteen notifications stayed
mute for a month that way, discovered by accident.

So the question answered here is not "is the scheduler up" (it always is) but
"which ENABLED notification has not actually REACHED anyone for longer than its
own schedule can justify". Two consequences shape everything below:

* **Content, not status.** A notification is healthy when ``last_sent_at`` is
  recent — not when it has a job, not when it was triggered. The trigger ledger
  (notif_store.RUNS_PATH) says a fire happened; the outcome ledger
  (notif_store.OUTCOMES_PATH) says whether anything came out of it.
* **N is derived from the schedule.** A weekly report mute for three days is
  fine; a daily one is not. The tolerated silence is
  ``expected_interval * MISSED_FIRES_BEFORE_ALARM + GRACE_SEC`` — i.e. roughly
  "two consecutive occurrences missed, plus slack".

Every state is NAMED, and the names keep apart the two things whose confusion
was the original bug:

  ok         sent within its tolerated silence
  pending    never sent yet, but first seen too recently to judge (no false
             alarm on a notification created five minutes ago)
  suppressed silent, and the last fire says it was ON PURPOSE (gate closed,
             nothing to report, project paused) — "non doveva partire"
  blocked    silent because policy refuses it (personal/work ban). Not an error,
             not health either: this is the state the 14 were in
  silent     silent past its tolerance and nothing explains why
  broken     it tried and failed (no target resolved, no channel, send error)

``silent`` and ``broken`` are the alarms: "doveva partire e non è partita".
``blocked`` and ``suppressed`` are reported, counted, and never hidden — but
they do not cry wolf.

Pure logic + the two stores: no Telegram, no bot import (providers.py and api.py
are the two callers).
"""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timedelta

from apscheduler.util import astimezone

import notif_store
import notifications

logger = logging.getLogger("coordinator.notif_health")

# Tolerated silence = expected_interval * MISSED_FIRES_BEFORE_ALARM + GRACE_SEC.
# Two missed occurrences, not one: a single miss is noise (a restart, a stalled
# event loop, a Telegram hiccup) and an alarm that fires on noise gets muted by
# the person, which would rebuild the original problem out of human habit.
MISSED_FIRES_BEFORE_ALARM = float(os.environ.get("NOTIF_HEALTH_MISSED_FIRES", "2"))
GRACE_SEC = int(os.environ.get("NOTIF_HEALTH_GRACE_SEC", str(6 * 3600)))

# The key of the notification that carries the periodic health alert to Telegram.
# It is itself an ordinary notification (same store, same scheduler, same
# delivery) — see WATCHDOG SELF-CHECK below.
WATCHDOG_KEY = os.environ.get("NOTIF_HEALTH_KEY", "coordinator-salute-notifiche")
WATCHDOG_PROVIDER = "health"

STATUS_OK = "ok"
STATUS_PENDING = "pending"
STATUS_SUPPRESSED = "suppressed"
STATUS_BLOCKED = "blocked"
STATUS_SILENT = "silent"
STATUS_BROKEN = "broken"
STATUS_DISABLED = "disabled"

ALARM_STATUSES = (STATUS_SILENT, STATUS_BROKEN)
DEGRADED_STATUSES = (STATUS_BLOCKED, STATUS_SUPPRESSED)

_REASONS = {
    STATUS_OK: "inviata entro la cadenza prevista",
    STATUS_PENDING: "creata da poco, non ancora scaduta la prima cadenza",
    STATUS_SUPPRESSED: "non invia, ma l'ultimo esito dice che non doveva partire",
    STATUS_BLOCKED: "bloccata dalla policy: non partirà finché resta così",
    STATUS_SILENT: "doveva partire e non è partita: nessun invio e nessuna spiegazione",
    STATUS_BROKEN: "doveva partire e non è partita: l'ultimo tentativo è fallito",
    STATUS_DISABLED: "disabilitata (esclusa dal controllo)",
}

_TZ_ENV = os.environ.get("TZ_NAME", "Europe/Rome")


def _tzobj():
    return astimezone(_TZ_ENV)


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=_tzobj()) if dt.tzinfo is None else dt


def expected_interval_sec(schedule: str, now: datetime | None = None) -> float | None:
    """How often ``schedule`` is meant to fire, in seconds. None if unparsable.

    Measured from the crontab itself rather than declared, so "0 8 * * *" and
    "0 11 * * sun" get 1 day and 7 days without a table to maintain. The WIDEST
    of the next few gaps is used: for an irregular cadence (e.g. "0 8 * * mon,tue")
    the tolerance must cover the long gap, or the watchdog alarms every weekend.
    """
    try:
        trig = notifications.cron_trigger(schedule, timezone=_tzobj())
    except (ValueError, TypeError):
        return None
    cursor = now or datetime.now(_tzobj())
    fires: list[datetime] = []
    for _ in range(6):
        nxt = trig.get_next_fire_time(None, cursor)
        if nxt is None:
            break
        fires.append(nxt)
        cursor = nxt + timedelta(seconds=1)
    if len(fires) < 2:
        return None
    gaps = [(b - a).total_seconds() for a, b in zip(fires, fires[1:], strict=False)]
    return max(gaps) or None


def silence_limit_sec(schedule: str, now: datetime | None = None) -> float | None:
    """Longest silence a notification on ``schedule`` may have before alarming."""
    interval = expected_interval_sec(schedule, now)
    if interval is None:
        return None
    return interval * MISSED_FIRES_BEFORE_ALARM + GRACE_SEC


def _entry(notif, rec: dict, now: datetime) -> dict:
    limit = silence_limit_sec(notif.schedule, now)
    last_sent = _parse(rec.get("last_sent_at"))
    last_attempt = _parse(rec.get("last_attempt_at"))
    first_seen = _parse(rec.get("first_seen"))
    category = rec.get("last_category")

    # What counts as "alive" for this notification. For everything else it is a
    # SEND — measuring the trigger instead of the delivery is the whole bug. The
    # health provider is the one exception: it is *supposed* to send nothing when
    # the network is fine, so judging it by its sends would mark a working
    # watchdog as mute. It is judged by having RUN (see _watchdog_block).
    attempt_based = notif.provider == WATCHDOG_PROVIDER
    reference = last_attempt if attempt_based else last_sent
    silent_for = (now - reference).total_seconds() if reference else None

    if not notif.enabled:
        status = STATUS_DISABLED
    elif limit is None:
        # An unparsable crontab is a degradation in its own right: the job was
        # never registered, so this notification is mute by construction.
        status = STATUS_BROKEN
    elif attempt_based and category == notifications.CAT_BROKEN:
        # A watchdog that runs but cannot deliver its own alarm is worse than a
        # mute report: it is the thing that was supposed to notice.
        status = STATUS_BROKEN
    elif (rec.get("last_outcome") or "").startswith(notifications.OUTCOME_ERROR_PREFIX):
        # The provider failed and the chat was spared: the error is only here,
        # so it alarms at once instead of waiting for the silence limit.
        status = STATUS_BROKEN
    elif silent_for is not None and silent_for <= limit:
        status = STATUS_OK
    elif last_attempt is None and (first_seen is None or
                                   (now - first_seen).total_seconds() <= limit):
        # Never fired yet AND young enough that its first occurrence may still be
        # ahead. Only "no attempt at all" earns this: one that already fired and
        # was refused is judged by that outcome, however recently it was created.
        status = STATUS_PENDING
    elif category == notifications.CAT_BLOCKED:
        status = STATUS_BLOCKED
    elif category == notifications.CAT_SUPPRESSED:
        status = STATUS_SUPPRESSED
    elif category == notifications.CAT_BROKEN:
        status = STATUS_BROKEN
    else:
        # Silence with nothing on record to explain it: never fired at all, or
        # fired so long ago the ledger has forgotten. This is the shape the 14
        # mute notifications had.
        status = STATUS_SILENT

    def _hours(seconds: float | None) -> float | None:
        return None if seconds is None else round(seconds / 3600, 1)

    return {
        "key": notif.key,
        "title": notif.title,
        "schedule": notif.schedule,
        "enabled": notif.enabled,
        "provider": notif.provider,
        "status": status,
        "reason": _REASONS[status],
        "alarm": status in ALARM_STATUSES,
        # Which timestamp "silent_for_h" measures: the delivery for a normal
        # notification, the run for the watchdog (whose silence is the good news).
        "measured_on": "attempt" if attempt_based else "send",
        "expected_interval_h": _hours(expected_interval_sec(notif.schedule, now)),
        "silence_limit_h": _hours(limit),
        "silent_for_h": _hours(silent_for),
        "last_sent_at": rec.get("last_sent_at"),
        "last_attempt_at": rec.get("last_attempt_at"),
        "last_outcome": rec.get("last_outcome"),
        "last_category": category,
        "first_seen": rec.get("first_seen"),
        "totals": dict(rec.get("totals") or {}),
    }


def _watchdog_block(entries: list[dict], outcomes: dict, now: datetime) -> dict:
    """WATCHDOG SELF-CHECK — how the alert notices its own silence.

    The health alert is delivered by the very machinery it watches, so on its own
    it could go mute exactly like everything else. It cannot vouch for itself by
    sending, because it is *supposed* to stay quiet when all is well (an alert
    that arrives every day saying "fine" gets ignored, and then a real one does
    too). So its liveness is not measured by what it sends but by ``last_attempt_at``
    — the timestamp the scheduler writes on EVERY fire, including the quiet ones.
    That timestamp is read here, through a different path (the HTTP endpoint), so
    the watchdog's own death shows up as a named, alarming state instead of as
    more silence. Absent or disabled counts as failure, not as "nothing to say".
    """
    entry = next((e for e in entries if e["key"] == WATCHDOG_KEY), None)
    rec = outcomes.get(WATCHDOG_KEY) or {}
    last_check = _parse(rec.get("last_attempt_at"))
    block = {
        "key": WATCHDOG_KEY,
        "present": entry is not None,
        "enabled": bool(entry and entry["enabled"]),
        "last_check_at": rec.get("last_attempt_at"),
        "max_silence_h": entry["silence_limit_h"] if entry else None,
        "stale": True,
        "reason": "",
    }
    if entry is None:
        block["reason"] = (
            f"nessuna notifica '{WATCHDOG_KEY}': il controllo periodico non esiste, "
            "quindi nessuno si accorgerebbe di una notifica muta"
        )
        return block
    if not entry["enabled"]:
        block["reason"] = "il controllo periodico esiste ma è disabilitato"
        return block
    limit = silence_limit_sec(entry["schedule"], now)
    if last_check is None:
        block["reason"] = "il controllo periodico non è mai girato"
        return block
    if limit is not None and (now - last_check).total_seconds() <= limit:
        block["stale"] = False
        block["reason"] = "il controllo periodico ha girato entro la sua cadenza"
    else:
        block["reason"] = (
            f"il controllo periodico non gira da {(now - last_check).days} giorni: "
            "il guardiano stesso è muto"
        )
    return block


def build_report(now: datetime | None = None, *, mark_seen: bool = True) -> dict:
    """The health of every stored notification, plus the watchdog's own state.

    ``mark_seen`` writes a ``first_seen`` baseline for notifications that have
    never fired, so the FIRST observation of a new notification never alarms and
    the clock starts from that moment. Tests pass ``False`` to stay read-only.
    """
    now = now or datetime.now(_tzobj())
    notifs = notif_store.load()
    if mark_seen:
        notif_store.note_seen([n.key for n in notifs], now.isoformat())
    outcomes = notif_store.load_outcomes()
    entries = [_entry(n, outcomes.get(n.key) or {}, now) for n in notifs]

    watchdog = _watchdog_block(entries, outcomes, now)
    alarms = [e for e in entries if e["alarm"]]
    degraded = [e for e in entries if e["status"] in DEGRADED_STATUSES]
    if alarms or watchdog["stale"]:
        status = "critical"
    elif degraded:
        status = "degraded"
    else:
        status = "ok"

    counts: dict[str, int] = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "summary": {
            "total": len(entries),
            "enabled": sum(1 for e in entries if e["enabled"]),
            "alarms": len(alarms),
            "degraded": len(degraded),
            "by_status": counts,
        },
        "watchdog": watchdog,
        "alarms": alarms,
        "notifications": entries,
    }


# Where the periodic health alert is delivered, and how often. Both live in the
# (gitignored) .env because a chat id is personal data that must never enter a
# versioned file — Life is going public. Unset destination => the watchdog is
# not created, and the report then says so out loud (watchdog.present == false,
# status critical) instead of pretending everything is fine.
WATCHDOG_DEST_ENV = "NOTIF_HEALTH_DEST"
WATCHDOG_SCHEDULE = os.environ.get("NOTIF_HEALTH_SCHEDULE", "0 9 * * *")


def ensure_watchdog(env=None) -> str | None:
    """Create/refresh the health-alert notification. Returns its key, or None.

    Idempotent and non-destructive: an existing row keeps its ``enabled`` flag
    and destination if the env no longer names one, so a manual disable is not
    silently undone at every boot.
    """
    dest = ((os.environ if env is None else env).get(WATCHDOG_DEST_ENV) or "").strip()
    current = notif_store.get(WATCHDOG_KEY)
    if not dest and current is None:
        logger.warning(
            "%s not set: no periodic health alert exists — a mute notification "
            "would only be visible via GET /api/notifications/health",
            WATCHDOG_DEST_ENV,
        )
        return None
    notif = notifications.Notification(
        key=WATCHDOG_KEY,
        title="Salute notifiche",
        provider=WATCHDOG_PROVIDER,
        schedule=WATCHDOG_SCHEDULE,
        recipients=(),
        enabled=current.enabled if current is not None else True,
        params={},
        destination=dest or (current.destination if current else "dm"),
    )
    notif_store.upsert(notif)
    return notif.key


def format_alert(report: dict) -> str | None:
    """The Telegram text for a report, or None when there is nothing to say.

    Silence when healthy is deliberate — see the watchdog self-check above: the
    alert's own liveness is proven by ``last_attempt_at``, not by chatter. Only
    a CRITICAL report (a real alarm: something that should have sent did not, or
    the watchdog itself is stale) produces a message. ``degraded`` (blocked /
    suppressed notifications) is a by-design quiet state, not a malfunction: a
    daily message about notifications that were *supposed* to stay quiet is
    exactly the noise that gets ignored, and then a real one does too.
    """
    if report["status"] != "critical":
        return None
    lines = ["🚨 <b>Notifiche mute</b>"]
    if report["watchdog"]["stale"]:
        lines.append(f"• ⚠️ <b>guardiano</b>: {report['watchdog']['reason']}")
    for e in report["alarms"]:
        silent = f"{e['silent_for_h']:.0f}h" if e["silent_for_h"] else "mai"
        lines.append(
            f"• <b>{e['key']}</b> — {e['reason']}\n"
            f"  ultimo invio: {silent} fa · cadenza {e['schedule']} · "
            # Provider errors carry raw stderr / agent text: escape it for HTML.
            f"ultimo esito: {html.escape((e['last_outcome'] or 'nessuno')[:300], quote=False)}"
        )
    degraded = [e for e in report["notifications"] if e["status"] in DEGRADED_STATUSES]
    if degraded:
        lines.append(
            "ℹ️ non allarmi ma nemmeno sane: "
            + ", ".join(f"{e['key']} ({e['status']})" for e in degraded)
        )
    return "\n".join(lines)
