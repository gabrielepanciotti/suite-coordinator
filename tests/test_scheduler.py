import asyncio
from datetime import datetime

from apscheduler.util import astimezone

import notif_store
import scheduler
from notifications import Notification

_TZ = astimezone("Europe/Rome")


def _run(coro):
    return asyncio.run(coro)


def _n(key="a", enabled=True, schedule="0 8 * * *"):
    return Notification(
        key, "T", "static", schedule, ("owner",), enabled, {"text": "ciao"}
    )


def _py(code: str) -> str:
    """A shell command running ``code`` with this interpreter.

    Double quotes on purpose: the gate runs through the platform shell, and
    cmd.exe does not understand the POSIX single-quoting shlex.quote emits.
    ``code`` must therefore contain no double quote.
    """
    import sys

    return f'"{sys.executable}" -c "{code}"'


def _fresh_sched(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler(timezone="Europe/Rome")  # not started
    monkeypatch.setitem(scheduler._S, "sched", sched)
    monkeypatch.setitem(scheduler._S, "tz", "Europe/Rome")
    return sched


def test_apply_adds_and_removes_job(monkeypatch):
    sched = _fresh_sched(monkeypatch)
    scheduler.apply(_n("a", enabled=True))
    assert sched.get_job("a") is not None
    scheduler.apply(_n("a", enabled=False))  # disable -> removed
    assert sched.get_job("a") is None


def test_fire_outcome_names_why_the_gate_closed(monkeypatch):
    # A closed gate must say WHY in the outcome: "not today" and "the gate script
    # is gone" both stop the send, and /test answered the same for both.
    closed = _py("import sys; sys.stderr.write('nessun viaggio'); sys.exit(1)")
    notif = Notification("brief", "T", "static", "0 8 * * *", ("owner",), True,
                         {"text": "ciao", "gate": closed})
    monkeypatch.setattr(notif_store, "get", lambda k: notif)
    monkeypatch.setattr(notif_store, "record_run", lambda k, w: None)
    monkeypatch.setattr(scheduler.notifications, "is_banned", lambda n: False)
    monkeypatch.setattr(scheduler.overview_store, "is_paused", lambda p: False)
    outcome = _run(scheduler._fire("brief"))
    assert outcome.startswith("fermata dal gate")
    assert "nessun viaggio" in outcome


def test_fire_runs_provider_and_sends_to_targets(monkeypatch):
    sent = []
    monkeypatch.setattr(notif_store, "get", lambda k: _n("a"))

    async def fake_run_provider(notif, *, run_claude):
        return "TESTO"

    monkeypatch.setattr(scheduler.providers, "run_provider", fake_run_provider)
    monkeypatch.setattr(
        scheduler.notifications,
        "resolve_targets",
        lambda n: [(111, None), (-100999, 7)],
    )

    async def fake_send_target(chat_id, thread_id, text):
        sent.append((chat_id, thread_id, text))

    monkeypatch.setitem(scheduler._S, "send_target", fake_send_target)
    monkeypatch.setitem(scheduler._S, "run_claude", None)

    _run(scheduler._fire("a"))
    assert sent == [(111, None, "TESTO"), (-100999, 7, "TESTO")]


def test_fire_skips_disabled(monkeypatch):
    monkeypatch.setattr(notif_store, "get", lambda k: _n("a", enabled=False))
    called = []
    monkeypatch.setitem(scheduler._S, "send_target", lambda *a: called.append(a))
    _run(scheduler._fire("a"))
    assert called == []


def test_fire_no_targets_no_send(monkeypatch):
    monkeypatch.setattr(notif_store, "get", lambda k: _n("a"))

    async def fake_run_provider(notif, *, run_claude):
        return "TESTO"

    monkeypatch.setattr(scheduler.providers, "run_provider", fake_run_provider)
    monkeypatch.setattr(scheduler.notifications, "resolve_targets", lambda n: [])
    sent = []

    async def fake_send_target(chat_id, thread_id, text):
        sent.append(chat_id)

    monkeypatch.setitem(scheduler._S, "send_target", fake_send_target)
    _run(scheduler._fire("a"))
    assert sent == []


# --- catch-up logic ----------------------------------------------------------


def test_last_fire_in_window_finds_recent_trigger():
    now = datetime(2026, 6, 14, 9, 30, tzinfo=_TZ)
    last = scheduler._last_fire_in_window(
        "0 8 * * *", now, 2 * 3600
    )  # window [07:30, 09:30]
    assert last is not None and last.hour == 8 and last.minute == 0


def test_last_fire_in_window_none_when_outside():
    now = datetime(2026, 6, 14, 11, 0, tzinfo=_TZ)
    assert (
        scheduler._last_fire_in_window("0 8 * * *", now, 2 * 3600) is None
    )  # 08:00 not in [09:00,11:00]


def _fake_runs(monkeypatch, initial):
    runs = dict(initial)
    monkeypatch.setattr(notif_store, "load_runs", lambda: dict(runs))
    monkeypatch.setattr(
        notif_store, "record_run", lambda k, ts: runs.__setitem__(k, ts)
    )
    return runs


def test_catchup_fires_when_trigger_missed_during_downtime(monkeypatch):
    sched = _fresh_sched(monkeypatch)
    _fake_runs(monkeypatch, {"a": "2000-01-01T00:00:00+01:00"})  # ancient -> missed
    scheduler._schedule_catchup([_n("a", schedule="* * * * *")])  # fires every minute
    job = sched.get_job("catchup:a")
    assert job is not None


def test_catchup_skips_first_seen_and_sets_baseline(monkeypatch):
    sched = _fresh_sched(monkeypatch)
    runs = _fake_runs(monkeypatch, {})  # no history
    scheduler._schedule_catchup([_n("a", schedule="* * * * *")])
    assert sched.get_job("catchup:a") is None  # no surprise send on fresh deploy
    assert "a" in runs  # baseline established


def test_catchup_skips_disabled(monkeypatch):
    sched = _fresh_sched(monkeypatch)
    _fake_runs(monkeypatch, {"a": "2000-01-01T00:00:00+01:00"})
    scheduler._schedule_catchup([_n("a", enabled=False, schedule="* * * * *")])
    assert sched.get_job("catchup:a") is None


def test_fire_skips_personal_project(monkeypatch):
    # Policy gate: a banned (personal, non-allowlisted) notification must neither
    # produce (provider) nor send. The gate returns before run_provider runs.
    monkeypatch.setattr(notif_store, "get", lambda k: _n("a"))
    monkeypatch.setattr(scheduler.notifications, "is_banned", lambda n: True)
    produced = []

    async def fake_run_provider(notif, *, run_claude):
        produced.append(notif.key)
        return "TESTO"

    monkeypatch.setattr(scheduler.providers, "run_provider", fake_run_provider)
    sent = []

    async def fake_send_target(chat_id, thread_id, text):
        sent.append(chat_id)

    monkeypatch.setitem(scheduler._S, "send_target", fake_send_target)
    monkeypatch.setitem(scheduler._S, "run_claude", None)
    _run(scheduler._fire("a"))
    assert produced == [] and sent == []


def test_fire_runs_allowlisted_personal(monkeypatch):
    # Allowlist derogation: a personal notification whose key is allowlisted is
    # NOT banned, so it produces and sends like a normal one. The shipped
    # allowlist is empty (life-work bans every personal project), so pin a key
    # into it here: what is under test is the derogation mechanism, not which
    # keys currently enjoy it.
    notif = _n("personalchef-feedback-sera")
    monkeypatch.setattr(notif_store, "get", lambda k: notif)
    monkeypatch.setattr(scheduler.notifications, "is_personal", lambda n: True)
    monkeypatch.setattr(
        scheduler.notifications,
        "NOTIFICATION_ALLOWLIST",
        frozenset({"personalchef-feedback-sera"}),
    )
    sent = []

    async def fake_run_provider(n, *, run_claude):
        return "TESTO"

    monkeypatch.setattr(scheduler.providers, "run_provider", fake_run_provider)
    monkeypatch.setattr(
        scheduler.notifications, "resolve_targets", lambda n: [(111, None)]
    )

    async def fake_send_target(chat_id, thread_id, text):
        sent.append((chat_id, text))

    monkeypatch.setitem(scheduler._S, "send_target", fake_send_target)
    monkeypatch.setitem(scheduler._S, "run_claude", None)
    _run(scheduler._fire("personalchef-feedback-sera"))
    assert sent == [(111, "TESTO")]
