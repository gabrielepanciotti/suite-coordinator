"""The watchdog over mute notifications.

Every test here is a shape the original failure actually had: a notification that
stopped sending while looking healthy, and a would-be watchdog that either cried
wolf on brand-new rows or stayed quiet on dead ones.
"""

import asyncio
from datetime import datetime, timedelta

from apscheduler.util import astimezone

import notif_health
import notif_store
import notifications
import scheduler
from notifications import Notification

_TZ = astimezone("Europe/Rome")
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=_TZ)

DAILY = "0 8 * * *"
WEEKLY = "0 11 * * sun"


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(notif_store, "NOTIF_PATH", str(tmp_path / "n.json"))
    monkeypatch.setattr(notif_store, "OUTCOMES_PATH", str(tmp_path / "o.json"))
    monkeypatch.setattr(notif_store, "RUNS_PATH", str(tmp_path / "r.json"))


def _n(key, schedule=DAILY, enabled=True):
    return Notification(key, key, "static", schedule, ("owner",), enabled, {"text": "x"})


def _fired(key, *, ago_days, outcome=notifications.OUTCOME_SENT):
    """Record one fire of ``key`` that happened ``ago_days`` before NOW."""
    notif_store.record_outcome(key, outcome, (NOW - timedelta(days=ago_days)).isoformat())


def _entry(report, key):
    return next(e for e in report["notifications"] if e["key"] == key)


# --- cadence: N is derived from the schedule, not declared -----------------


def test_expected_interval_is_read_from_the_crontab():
    day = notif_health.expected_interval_sec(DAILY, NOW)
    week = notif_health.expected_interval_sec(WEEKLY, NOW)
    assert day == 24 * 3600
    assert week == 7 * 24 * 3600


def test_unparsable_schedule_has_no_interval():
    assert notif_health.expected_interval_sec("not a crontab", NOW) is None


# --- the four cases that matter -------------------------------------------


def test_daily_mute_for_three_days_alarms(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("giornaliera"))
    _fired("giornaliera", ago_days=3)

    report = notif_health.build_report(NOW)
    entry = _entry(report, "giornaliera")
    assert entry["status"] == notif_health.STATUS_SILENT
    assert entry["alarm"] is True
    assert entry["silent_for_h"] == 72.0
    assert report["status"] == "critical"
    assert [e["key"] for e in report["alarms"]] == ["giornaliera"]


def test_weekly_mute_for_two_days_does_not_alarm(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("settimanale", schedule=WEEKLY))
    _fired("settimanale", ago_days=2)

    entry = _entry(notif_health.build_report(NOW), "settimanale")
    assert entry["status"] == notif_health.STATUS_OK
    assert entry["alarm"] is False
    # 2 missed weeks + grace: two days of silence is nowhere near it.
    assert entry["silence_limit_h"] > 300


def test_weekly_mute_for_three_weeks_does_alarm(tmp_path, monkeypatch):
    """The mirror of the test above: the tolerance scales, it does not disappear."""
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("settimanale", schedule=WEEKLY))
    _fired("settimanale", ago_days=21)

    entry = _entry(notif_health.build_report(NOW), "settimanale")
    assert entry["status"] == notif_health.STATUS_SILENT
    assert entry["alarm"] is True


def test_disabled_notification_is_excluded_from_the_alarm(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("spenta", enabled=False))
    _fired("spenta", ago_days=90)

    report = notif_health.build_report(NOW)
    entry = _entry(report, "spenta")
    assert entry["status"] == notif_health.STATUS_DISABLED
    assert entry["alarm"] is False
    assert report["alarms"] == []
    assert report["summary"]["enabled"] == 0


def test_brand_new_notification_never_fired_does_not_alarm(tmp_path, monkeypatch):
    """First observation must never accuse: it starts the clock instead."""
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("appena-creata"))

    first = notif_health.build_report(NOW)          # marks first_seen == NOW
    entry = _entry(first, "appena-creata")
    assert entry["status"] == notif_health.STATUS_PENDING
    assert entry["alarm"] is False
    assert notif_store.load_outcomes()["appena-creata"]["first_seen"] == NOW.isoformat()

    # Still within the tolerated window a day later: still no alarm.
    entry = _entry(notif_health.build_report(NOW + timedelta(days=1)), "appena-creata")
    assert entry["status"] == notif_health.STATUS_PENDING


def test_a_notification_that_never_fires_stops_being_pending(tmp_path, monkeypatch):
    """Pending is a grace period, not an amnesty."""
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("mai-partita"))
    notif_health.build_report(NOW)                   # baseline

    entry = _entry(notif_health.build_report(NOW + timedelta(days=5)), "mai-partita")
    assert entry["status"] == notif_health.STATUS_SILENT
    assert entry["alarm"] is True


# --- "non doveva partire" vs "doveva partire e non è partita" --------------


def test_policy_blocked_is_named_not_counted_as_healthy(tmp_path, monkeypatch):
    """The exact state of the 14 mute notifications: never an error, never fine."""
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("vietata"))
    _fired("vietata", ago_days=1, outcome=notifications.OUTCOME_BANNED)

    report = notif_health.build_report(NOW)
    entry = _entry(report, "vietata")
    assert entry["status"] == notif_health.STATUS_BLOCKED
    assert entry["alarm"] is False           # not an alarm...
    assert report["summary"]["degraded"] == 1  # ...but not hidden either


def test_closed_gate_is_suppressed_not_broken(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("con-gate"))
    _fired("con-gate", ago_days=3, outcome="fermata dal gate: nessun viaggio")

    entry = _entry(notif_health.build_report(NOW), "con-gate")
    assert entry["status"] == notif_health.STATUS_SUPPRESSED
    assert entry["alarm"] is False


def test_a_failed_send_is_broken_and_alarms(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("rotta"))
    _fired("rotta", ago_days=3, outcome=notifications.OUTCOME_NO_TARGETS)

    entry = _entry(notif_health.build_report(NOW), "rotta")
    assert entry["status"] == notif_health.STATUS_BROKEN
    assert entry["alarm"] is True
    assert entry["last_outcome"] == notifications.OUTCOME_NO_TARGETS


def test_outcome_categories():
    c = notifications.classify_outcome
    assert c(notifications.OUTCOME_SENT) == notifications.CAT_SENT
    assert c(notifications.OUTCOME_BANNED) == notifications.CAT_BLOCKED
    assert c("fermata dal gate: piove") == notifications.CAT_SUPPRESSED
    assert c("progetto cosmos in pausa") == notifications.CAT_SUPPRESSED
    assert c(notifications.OUTCOME_NO_CONTENT) == notifications.CAT_SUPPRESSED
    assert c("invio fallito verso 1 destinatari su 2") == notifications.CAT_BROKEN
    assert c(notifications.OUTCOME_NO_CHANNEL) == notifications.CAT_BROKEN
    # An outcome nobody named must be loud, not quietly fine.
    assert c("qualcosa di nuovo") == notifications.CAT_BROKEN
    assert c(None) == notifications.CAT_BROKEN


# --- the watchdog watching itself -----------------------------------------


def test_missing_watchdog_is_itself_a_critical_finding(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("qualsiasi"))
    _fired("qualsiasi", ago_days=0)

    report = notif_health.build_report(NOW)
    assert report["watchdog"]["present"] is False
    assert report["watchdog"]["stale"] is True
    assert report["status"] == "critical"


def test_watchdog_liveness_is_the_attempt_not_the_send(tmp_path, monkeypatch):
    """A healthy watchdog sends nothing — so silence cannot be its health signal."""
    _store(tmp_path, monkeypatch)
    notif_store.upsert(Notification(
        notif_health.WATCHDOG_KEY, "Salute", "health", DAILY, (), True, {}, "chat:-1"
    ))
    # It ran and had nothing to say: no send, but an attempt on record.
    _fired(notif_health.WATCHDOG_KEY, ago_days=0,
           outcome=notifications.OUTCOME_NO_CONTENT)

    report = notif_health.build_report(NOW)
    assert report["watchdog"]["stale"] is False
    assert report["status"] == "ok"
    assert notif_health.format_alert(report) is None


def test_a_watchdog_that_stopped_running_is_reported_stale(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    notif_store.upsert(Notification(
        notif_health.WATCHDOG_KEY, "Salute", "health", DAILY, (), True, {}, "chat:-1"
    ))
    _fired(notif_health.WATCHDOG_KEY, ago_days=9,
           outcome=notifications.OUTCOME_NO_CONTENT)

    report = notif_health.build_report(NOW)
    assert report["watchdog"]["stale"] is True
    assert report["status"] == "critical"
    assert "guardiano" in notif_health.format_alert(report)


def test_ensure_watchdog_needs_a_destination(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    assert notif_health.ensure_watchdog(env={}) is None
    assert notif_store.get(notif_health.WATCHDOG_KEY) is None

    key = notif_health.ensure_watchdog(env={notif_health.WATCHDOG_DEST_ENV: "chat:-42"})
    assert key == notif_health.WATCHDOG_KEY
    assert notif_store.get(key).provider == "health"


def test_ensure_watchdog_does_not_silently_re_enable_a_disabled_one(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    notif_health.ensure_watchdog(env={notif_health.WATCHDOG_DEST_ENV: "chat:-42"})
    current = notif_store.get(notif_health.WATCHDOG_KEY)
    current.enabled = False
    notif_store.upsert(current)

    notif_health.ensure_watchdog(env={notif_health.WATCHDOG_DEST_ENV: "chat:-42"})
    assert notif_store.get(notif_health.WATCHDOG_KEY).enabled is False


# --- the ledger itself -----------------------------------------------------


def test_fire_persists_its_outcome(tmp_path, monkeypatch):
    """The whole point: the outcome no longer dies inside a logger.info."""
    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("k"))
    monkeypatch.setitem(scheduler._S, "send_target", None)  # -> no channel
    monkeypatch.setattr(scheduler.notifications, "is_banned", lambda n: False)

    outcome = asyncio.run(scheduler._fire("k"))
    assert outcome == notifications.OUTCOME_NO_CHANNEL

    rec = notif_store.load_outcomes()["k"]
    assert rec["last_outcome"] == notifications.OUTCOME_NO_CHANNEL
    assert rec["last_category"] == notifications.CAT_BROKEN
    assert rec["last_sent_at"] is None            # it did NOT reach anyone
    assert rec["last_attempt_at"] is not None     # but it did try
    assert rec["totals"] == {notifications.CAT_BROKEN: 1}


def test_record_outcome_tracks_last_sent_separately(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    _fired("k", ago_days=5, outcome=notifications.OUTCOME_SENT)
    _fired("k", ago_days=1, outcome=notifications.OUTCOME_NO_CONTENT)

    rec = notif_store.load_outcomes()["k"]
    assert rec["last_sent_at"] == (NOW - timedelta(days=5)).isoformat()
    assert rec["last_attempt_at"] == (NOW - timedelta(days=1)).isoformat()
    assert len(rec["recent"]) == 2


def test_recent_history_is_bounded(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    for i in range(notif_store.RECENT_MAX + 7):
        notif_store.record_outcome("k", notifications.OUTCOME_SENT, f"2026-08-0{i % 9 + 1}")
    assert len(notif_store.load_outcomes()["k"]["recent"]) == notif_store.RECENT_MAX


def test_manifest_sync_does_not_wipe_the_outcome_ledger(tmp_path, monkeypatch):
    """notif_sync upserts whole Notification objects from the nodes' coordinator.toml
    at EVERY boot. Anything kept inside a Notification would be silently reset;
    the ledger lives in its own file precisely so history survives that pass."""
    import manifest
    import notif_sync
    import projects
    from manifest import NotificationDecl

    _store(tmp_path, monkeypatch)
    notif_store.upsert(_n("nodo-report"))
    _fired("nodo-report", ago_days=1)
    before = notif_store.load_outcomes()["nodo-report"]

    proj = projects.Project(key="nodo", label="Nodo", emoji="🔧",
                            host_path=str(tmp_path), workspace=str(tmp_path),
                            kind="work")
    decl = NotificationDecl(key="nodo-report", title="Report", provider="static",
                            schedule=DAILY, to="chat:-1", enabled=True,
                            project="nodo", prompt="", text="ciao", gate="")
    monkeypatch.setattr(projects, "list_projects", lambda: [proj])
    monkeypatch.setattr(manifest, "notifications_of", lambda p: [decl])
    monkeypatch.setattr(notif_sync.scheduler, "apply", lambda n: None)

    assert notif_sync.sync_from_manifests() == 1
    assert notif_store.get("nodo-report").destination == "chat:-1"  # sync did run
    assert notif_store.load_outcomes()["nodo-report"] == before      # history intact


def test_record_outcome_survives_a_missing_data_dir(monkeypatch):
    monkeypatch.setattr(notif_store, "OUTCOMES_PATH", "/no_such_dir_zzz/o.json")
    notif_store.record_outcome("k", notifications.OUTCOME_SENT, NOW.isoformat())
    assert notif_store.load_outcomes() == {}
