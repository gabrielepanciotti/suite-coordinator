# notifications.py
"""Notification domain for M4 — pure logic, no Telegram/IO/bot imports.

A *Notification* is a scheduled message routed to one or more M3 *profiles*.
Content is produced by a *provider* (see providers.py). Instances are persisted
by notif_store.py and scheduled by scheduler.py. Default deny: recipients are
explicit profile keys; a profile with no mapped user ids reaches nobody.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

import local_defaults
import profiles

logger = logging.getLogger("coordinator.notifications")

VALID_PROVIDERS: tuple[str, ...] = (
    "agent", "command", "deepseek-balance", "growbox", "health", "regime", "report",
    "sentinella", "static")

# Providers that run code: creatable ONLY from a versioned coordinator.toml
# (notif_sync), never from chat or the web API (they would be a remote shell).
MANIFEST_ONLY_PROVIDERS: frozenset[str] = frozenset({"command"})

NO_RECIPIENTS_MSG = "nessun destinatario"

# --- Fire outcomes ---------------------------------------------------------
# Every return path of scheduler._fire produces one of these strings. They used
# to exist only as literals inside _fire, travel to a logger.info and die there;
# nothing aggregated them, so a notification could stop sending for a month and
# still look healthy. They are constants here (and classified below) because the
# health report has to tell three different things apart:
#
#   sent       -> it went out
#   suppressed -> it correctly had nothing to send (gate closed, no content,
#                 project paused, disabled). "non doveva partire".
#   blocked    -> policy refused it. Not an error, but NOT healthy either: this
#                 is exactly the state the 14 mute notifications were in.
#   broken     -> it should have gone out and did not. "doveva partire e non è
#                 partita" — the only category that deserves an alarm together
#                 with pure silence.
#
# An outcome nobody recognises is classified BROKEN on purpose: an unnamed
# degradation must be loud, not silently counted as fine.
OUTCOME_SENT = "inviata"
OUTCOME_UNKNOWN_KEY = "sconosciuta"
OUTCOME_DISABLED = "disabilitata"
OUTCOME_BANNED = "vietata dalla policy (progetto personal/work)"
OUTCOME_NO_CONTENT = "nessun contenuto da inviare"
OUTCOME_NO_TARGETS = "nessun destinatario risolto"
OUTCOME_NO_CHANNEL = "nessun canale di invio configurato"
OUTCOME_GATE_PREFIX = "fermata dal gate"
OUTCOME_PAUSED_SUFFIX = "in pausa"
# A provider that failed (job exit != 0, queue exhausted, agent blocked): the
# reason goes to the outcome ledger -> frontend + daily health digest, never to
# the chat. Classified BROKEN by the fallback below.
OUTCOME_ERROR_PREFIX = "errore"
OUTCOME_SEND_FAILED_PREFIX = "invio fallito"

CAT_SENT = "sent"
CAT_SUPPRESSED = "suppressed"
CAT_BLOCKED = "blocked"
CAT_BROKEN = "broken"


def classify_outcome(outcome: str | None) -> str:
    """Bucket a ``_fire`` outcome string into one of the CAT_* categories."""
    if not outcome:
        return CAT_BROKEN
    text = outcome.strip()
    if text == OUTCOME_SENT:
        return CAT_SENT
    if text == OUTCOME_BANNED:
        return CAT_BLOCKED
    if text in (OUTCOME_DISABLED, OUTCOME_NO_CONTENT):
        return CAT_SUPPRESSED
    if text.startswith(OUTCOME_GATE_PREFIX):
        return CAT_SUPPRESSED
    if text.startswith("progetto ") and text.endswith(OUTCOME_PAUSED_SUFFIX):
        return CAT_SUPPRESSED
    if text in (OUTCOME_UNKNOWN_KEY, OUTCOME_NO_TARGETS, OUTCOME_NO_CHANNEL):
        return CAT_BROKEN
    if text.startswith(OUTCOME_SEND_FAILED_PREFIX):
        return CAT_BROKEN
    return CAT_BROKEN

# UI presets (label shown elsewhere) -> 5-field crontab.
_PRESETS: dict[str, str] = {
    "hourly": "0 * * * *",
    "m8": "0 8 * * *",
    "sun11": "0 11 * * sun",
}


@dataclass
class Notification:
    key: str  # slug = APScheduler job id
    title: str
    provider: str  # one of VALID_PROVIDERS
    schedule: str  # 5-field crontab
    recipients: tuple[
        str, ...
    ]  # profile keys (default deny) — used when destination="dm"
    enabled: bool = True
    params: dict = field(default_factory=dict)
    destination: str = "dm"  # dm | group | chat:<id>[:<thread>]
    # Life instance that produced this record (Coordinator#78, contract §4.2).
    # None on rows written before the multi-instance migration: notif_store
    # backfills them with the device's default instance ("the instance that
    # produced them today") on read, so the bump stays additive.
    instance_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["recipients"] = list(self.recipients)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Notification:
        return cls(
            key=d["key"],
            title=d.get("title", d["key"]),
            provider=d["provider"],
            schedule=d["schedule"],
            recipients=tuple(d.get("recipients", ())),
            enabled=bool(d.get("enabled", True)),
            params=dict(d.get("params", {})),
            destination=str(d.get("destination", "dm")) or "dm",
            instance_id=(str(d["instance_id"]) if d.get("instance_id") else None),
        )


_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _dow_token(tok: str) -> str:
    """One comma-separated day-of-week token, standard cron -> APScheduler names.

    Numeric values, ranges and steps are expanded to explicit names, so the
    week never has to wrap (``5-7`` = fri,sat,sun). Tokens with names are kept.
    """
    base, _, step_s = tok.partition("/")
    if base == "*" and not step_s:
        return tok
    if base == "*":
        lo, hi = 0, 6
    elif "-" in base:
        a, _, b = base.partition("-")
        if not (a.isdigit() and b.isdigit()):
            return tok
        lo, hi = int(a), int(b)
    elif base.isdigit():
        lo = hi = int(base)
        if step_s:
            hi = 6
    else:
        return tok
    step = int(step_s) if step_s.isdigit() else 1
    if not (0 <= lo <= 7 and 0 <= hi <= 7 and lo <= hi and step > 0):
        raise ValueError(f"giorno della settimana non valido: {tok!r}")
    days = [_DOW_NAMES[d % 7] for d in range(lo, hi + 1, step)]
    return ",".join(dict.fromkeys(days))


def to_apscheduler_crontab(schedule: str) -> str:
    """Translate the day-of-week field from standard cron (0/7 = sunday).

    ``CronTrigger.from_crontab`` reads 0 as MONDAY: ``0 18 * * 0`` fired on
    monday and ``30 9 * * 1`` on tuesday (#85). Only the 5th field changes.
    """
    fields = schedule.split()
    if len(fields) != 5:
        return schedule
    fields[4] = ",".join(_dow_token(t) for t in fields[4].split(","))
    return " ".join(fields)


def cron_trigger(schedule: str, timezone=None):
    """``CronTrigger`` for a standard 5-field crontab (the only entry point)."""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger.from_crontab(to_apscheduler_crontab(schedule), timezone=timezone)


def validate_schedule(schedule: str) -> bool:
    """True if ``schedule`` is a valid 5-field crontab APScheduler accepts."""
    try:
        cron_trigger(schedule)
        return True
    except (ValueError, TypeError):
        return False


def validate(notif: Notification) -> list[str]:
    """Return a list of human-readable errors ([] when valid)."""
    errors: list[str] = []
    slug = notif.key.replace("-", "")
    if not notif.key or not slug.isalnum():
        errors.append("key non valida (slug alfanumerico con trattini)")
    if notif.provider not in VALID_PROVIDERS:
        errors.append(f"provider sconosciuto: {notif.provider}")
    if not validate_schedule(notif.schedule):
        errors.append(f"crontab non valido: {notif.schedule!r}")
    known = {p.key for p in profiles.PROFILES}
    bad = [r for r in notif.recipients if r not in known]
    if bad:
        errors.append(f"profili destinatari sconosciuti: {bad}")
    # Recipients (M3 profiles) only matter for DM: group/chat destinations are
    # resolved from params["project"] / the explicit chat id instead.
    if (notif.destination or "dm").strip().lower() == "dm" and not notif.recipients:
        errors.append(NO_RECIPIENTS_MSG)
    if notif.provider == "agent":
        import projects  # local import: avoids circular dependency

        proj = notif.params.get("project")
        if not proj or projects.resolve_project(proj) is None:
            errors.append("agent: 'project' mancante o sconosciuto")
        if not (notif.params.get("prompt") or "").strip():
            errors.append("agent: 'prompt' mancante")
    if notif.provider == "command":
        import projects  # local import: avoids circular dependency

        proj = notif.params.get("project")
        if not proj or projects.resolve_project(proj) is None:
            errors.append("command: 'project' mancante o sconosciuto")
        if not str(notif.params.get("command") or "").strip():
            errors.append("command: 'command' mancante")
    if notif.provider in ("command", "agent"):
        t = notif.params.get("timeout_sec")
        if t is not None and (
            isinstance(t, bool) or not isinstance(t, (int, float)) or not 1 <= t <= 3600
        ):
            errors.append(f"{notif.provider}: 'timeout_sec' deve essere un numero tra 1 e 3600")
    if notif.provider == "static" and not (notif.params.get("text") or "").strip():
        errors.append("static: 'text' mancante")
    if notif.provider == "report":
        if not (notif.params.get("path") or "").strip():
            errors.append("report: 'path' mancante (relativo ai workspace)")
        elif ".." in Path(str(notif.params["path"])).parts:
            # Caught here as well as in the provider: a notification is created
            # from a chat, so the escape must be refused before it is persisted.
            errors.append("report: 'path' non può risalire con '..'")
    if notif.provider == "regime":
        person = str(notif.params.get("person") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", person):
            errors.append("regime: 'person' mancante o non è uno slug di Health")
        slot = str(notif.params.get("slot") or "").strip()
        if slot and not re.fullmatch(r"[a-z_]+", slot):
            errors.append(f"regime: 'slot' non valido: {slot!r}")
    return errors


def resolve_recipients(recipients: tuple[str, ...]) -> list[int]:
    """Profile keys -> deduped user ids (insertion order). Default deny."""
    seen: dict[int, None] = {}
    for pkey in recipients:
        for uid in profiles.user_ids_for(pkey):
            seen.setdefault(uid, None)
    return list(seen)


def preset_to_cron(preset: str) -> str | None:
    """UI schedule preset -> crontab, or None for an unknown preset."""
    return _PRESETS.get(preset)


def gen_key(base: str, existing: set[str]) -> str:
    """A unique slug from ``base`` (base, base-2, base-3, …)."""
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def project_of(notif: Notification) -> str:
    """Best-effort project key for a notification.

    Manifest/agent notifications carry ``params['project']``; seed/UI ones may
    not, so fall back to the slug before the first '-' (e.g. "personalchef-buongiorno"
    -> "personalchef"). Mirrors overview_snapshot._project_of (kept here as the canonical
    domain helper so scheduler/sync can gate without importing overview_snapshot).
    """
    proj = (notif.params or {}).get("project", "")
    if proj:
        return proj
    return notif.key.split("-", 1)[0]


def is_personal(notif: Notification) -> bool:
    """True when the notification belongs to a personal-kind project."""
    import projects

    proj = projects.resolve_project(project_of(notif))
    return proj is not None and proj.kind == "personal"


def is_work(notif: Notification) -> bool:
    """True when the notification belongs to a work-kind project (work instance)."""
    import projects

    proj = projects.resolve_project(project_of(notif))
    return proj is not None and proj.kind == "work"


# Explicit derogation to the 2026-06-15 personal-project ban: these notification
# keys are re-enabled. Allowlisted personal notifications are synced, scheduled
# and fired normally; every other personal notification stays banned. Keep this
# the single source of truth — scheduler + notif_sync gate on is_banned(), not
# is_personal(), so adding a key here is enough.
#
# WHY IT IS AN ENV VAR AND NOT A LIST OF LITERALS (2026-08-09).
# This module ships to BOTH devices. On 2026-07-31 the list was emptied with the
# rationale "this instance is life-work" — a *per-device* judgement written into
# *shared* code. The personal instance (the mini-pc) inherited it and went silent:
# 15 notifications stored, 14 banned, 1 alive, for weeks. Nobody noticed, because
# a notification that is dropped by the policy gate looks exactly like a
# notification that had nothing to say. Which keys a device wants is a device
# decision, so it lives in that device's (gitignored) .env:
#
#   NOTIFICATION_ALLOWLIST=key-a,key-b        # exactly these personal keys
#   NOTIFICATION_ALLOWLIST=*                  # every personal key (mini-pc style)
#   NOTIFICATION_ALLOWLIST=                   # set-but-empty: ban every personal key
#   (unset)                                   # fall back to _DEFAULT_ALLOWLIST below
#
# The default below is deliberately NOT empty: an unconfigured device must not
# degrade to silence again. The first 14 keys are the personal-node reports the owner
# asked for on 2026-08-09 (13 re-enabled + the network drift report). They are
# node/report names, never personal data — the destination chat id is not here,
# it lives only in the runtime store / .env.
# `growbox-orario` stays OUT: hourly cadence, pure noise.
_DEFAULT_ALLOWLIST: frozenset[str] = frozenset({
    "life-deriva-rete",             # network drift report (the only survivor pre-2026-08-09)
    "personalchef-buongiorno",
    "personalchef-menu-domenica",
    "personalchef-feedback-sera",
    "personalchef-proposta-ordine",
    "mycorise-scadenze-batch",
    "mycorise-digest-kb",
    "homeresearcher-riepilogo",
    "scheduler-piano-giorno",
    "scheduler-revisione-domenica",
    "cosmos-report-lunedi",
    "academicassistant-scadenze",
    "academicassistant-prelezione",   # a member's lesson reminders (2026-10-08):
    "academicassistant-postlezione",  # before/after each lesson, and the
    "academicassistant-sentinella",   # sentinel that says when they stop
    "academicassistant-preparazione",  # knowledge map for courses about to start
    "travel-briefing-partenza",     # ex travelers-*: re-keyed after the
    "travel-digest-domenica",       # Travelers -> Travel/ExperienceFinder rename
}) | frozenset(
    # The installation's own additions (locale/defaults.json). There the "work"
    # keys admitted on purpose: not reports but the prod sentinel's alarm and
    # heartbeat. Keeping them out would rebuild, on purpose, the silence of the
    # 8-13 August 2026 outage.
    local_defaults.get_list("NOTIFICATION_ALLOWLIST_EXTRA")
)

ALLOWLIST_ENV = "NOTIFICATION_ALLOWLIST"
ALLOW_ALL = "*"


def load_allowlist(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The effective allowlist: ``$NOTIFICATION_ALLOWLIST`` if set, else the default.

    Accepts comma- and/or whitespace-separated keys. An env var that is set but
    empty means "no personal key is allowed" — distinct from unset, which means
    "use the shipped default".
    """
    raw = (os.environ if env is None else env).get(ALLOWLIST_ENV)
    if raw is None:
        return _DEFAULT_ALLOWLIST
    return frozenset(part for part in re.split(r"[,\s]+", raw.strip()) if part)


NOTIFICATION_ALLOWLIST: frozenset[str] = load_allowlist()


def is_allowlisted(key: str) -> bool:
    """True when ``key`` escapes the personal-project ban.

    Reads the module-level NOTIFICATION_ALLOWLIST on every call (tests and the
    /notifiche UI patch it), and honours the "*" wildcard.
    """
    return ALLOW_ALL in NOTIFICATION_ALLOWLIST or key in NOTIFICATION_ALLOWLIST


def is_banned(notif: Notification) -> bool:
    """True when policy forbids producing/sending this notification.

    Banned when:
    - personal project AND key not allowlisted (policy 2026-06-15). The allowlist
      is NOTIFICATION_ALLOWLIST; the single entry "*" allows every personal key.
    - work project (work instance) AND key not allowlisted — policy 2026-07-06: work
      notifications are useless. Amended 2026-08-17: the ban stays the default,
      but an EXPLICIT allowlist entry escapes it, exactly as for personal keys.
      The amendment exists because one class of work notification is not useless
      at all — the outage alarm. The prod stayed down five days in August 2026
      and nobody was told; a policy that bans that alarm along with the weekly
      reports would rebuild the same silence on purpose.
    """
    if is_work(notif):
        return not is_allowlisted(notif.key)
    return is_personal(notif) and not is_allowlisted(notif.key)


TOPIC_DIRECTIVE = "@argomento:"


def split_topic_directive(text: str) -> tuple[str | None, str]:
    """(topic name, text without it) when the text opens with ``@argomento: <name>``.

    A provider uses it to aim a group notification at one forum topic chosen at
    run time (the lesson's subject), which a static ``to`` cannot express. The
    line is always stripped, so it never reaches a chat even when unresolved.
    """
    head, _, rest = (text or "").lstrip().partition("\n")
    if not head.lower().startswith(TOPIC_DIRECTIVE):
        return None, text
    name = head[len(TOPIC_DIRECTIVE):].strip()
    return (name or None), rest.strip()


def resolve_targets(notif: Notification) -> list[tuple[int, int | None]]:
    """Resolve a notification's destination into concrete (chat_id, thread_id) pairs.

    - "dm"            -> one (user_id, None) per resolved recipient
    - "group"         -> the [telegram].group_chat_id of params["project"]
    - "chat:<id>[:<t>]" -> the explicit chat (and optional forum topic)
    Unknown/unresolvable destinations yield [] (logged by callers as 'no recipients').
    """
    dest = (notif.destination or "dm").strip().lower()
    if dest == "dm":
        return [(uid, None) for uid in resolve_recipients(notif.recipients)]
    if dest == "group":
        import manifest  # local import: avoids circular dependency
        import projects  # local import: avoids circular dependency

        proj = projects.resolve_project(notif.params.get("project", ""))
        if proj is None:
            return []
        tg = manifest.telegram_of(proj)
        if tg is None or tg.group_chat_id is None:
            return []
        return [(tg.group_chat_id, None)]
    if dest.startswith("chat:"):
        parts = dest[len("chat:") :].split(":")
        try:
            chat_id = int(parts[0])
        except (ValueError, IndexError):
            return []
        thread = None
        if len(parts) > 1 and parts[1].strip():
            try:
                thread = int(parts[1])
            except ValueError:
                thread = None
        return [(chat_id, thread)]
    return []
