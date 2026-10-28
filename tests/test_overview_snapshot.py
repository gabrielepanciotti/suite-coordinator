# tests/test_overview_snapshot.py
"""Tests for overview_snapshot (build_snapshot) and overview_store (pause gate)."""
from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_notif(key, title, enabled=True, project="", provider="static"):
    from notifications import Notification
    params = {}
    if project:
        params["project"] = project
    if provider == "static":
        params["text"] = "hello"
    return Notification(
        key=key,
        title=title,
        provider=provider,
        schedule="0 8 * * *",
        recipients=("owner",),
        enabled=enabled,
        params=params,
    )


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERVIEW_PAUSES_PATH", str(tmp_path / "overview_pauses.json"))
    import overview_store
    importlib.reload(overview_store)
    return overview_store


# ---------------------------------------------------------------------------
# overview_store — unit tests
# ---------------------------------------------------------------------------

def test_set_and_get_pause(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.set_pause("personalChef", "2099-01-01T00:00:00Z")
    assert s.get_pause("personalChef") == "2099-01-01T00:00:00Z"


def test_is_paused_future(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    s.set_pause("personalChef", future)
    assert s.is_paused("personalChef") is True


def test_is_paused_past(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    past = "2000-01-01T00:00:00Z"
    s.set_pause("personalChef", past)
    assert s.is_paused("personalChef") is False


def test_is_paused_none(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    assert s.is_paused("personalChef") is False


def test_clear_pause(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.set_pause("personalChef", "2099-01-01T00:00:00Z")
    s.set_pause("personalChef", None)
    assert s.is_paused("personalChef") is False
    assert s.get_pause("personalChef") is None


def test_apply_pauses_sets_and_clears(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    payload = [
        {"project": "personalChef", "notifications_paused_until": "2099-06-01T08:00:00Z"},
        {"project": "travelers", "notifications_paused_until": None},
    ]
    s.apply_pauses(payload)
    assert s.is_paused("personalChef") is True
    assert s.is_paused("travelers") is False


def test_apply_pauses_ignores_missing_project(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    # Should not raise even if "project" key is absent
    s.apply_pauses([{"notifications_paused_until": "2099-01-01T00:00:00Z"}])


def test_persists_across_reload(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.set_pause("mycorise", "2099-01-01T00:00:00Z")
    s2 = _store(tmp_path, monkeypatch)
    assert s2.get_pause("mycorise") == "2099-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# overview_snapshot.build_snapshot — unit tests
# ---------------------------------------------------------------------------

def test_build_snapshot_groups_by_project(monkeypatch):
    import overview_snapshot

    notifs = [
        _make_notif("personalchef-buongiorno", "Buongiorno", project="personalChef"),
        _make_notif("personalchef-menu", "Menu settimana", project="personalChef"),
        _make_notif("gonzalo-check", "Check Gonzalo", project="travelers"),
    ]
    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: notifs)

    snap = overview_snapshot.build_snapshot(sched=None)
    assert "projects" in snap
    projects = {p["project"]: p for p in snap["projects"]}
    assert set(projects) == {"personalChef", "travelers"}
    assert len(projects["personalChef"]["notifications"]) == 2
    assert len(projects["travelers"]["notifications"]) == 1


def test_build_snapshot_uses_key_prefix_when_no_params_project(monkeypatch):
    import overview_snapshot

    notifs = [_make_notif("growbox-orario", "Growbox orario")]
    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: notifs)

    snap = overview_snapshot.build_snapshot(sched=None)
    projects = {p["project"] for p in snap["projects"]}
    assert "growbox" in projects


def test_build_snapshot_paused_flag(monkeypatch):
    import overview_snapshot

    notifs = [
        _make_notif("personalchef-buongiorno", "Buongiorno", enabled=False, project="personalChef"),
    ]
    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: notifs)

    snap = overview_snapshot.build_snapshot(sched=None)
    n = snap["projects"][0]["notifications"][0]
    assert n["paused"] is True


def test_build_snapshot_next_run_at_from_scheduler(monkeypatch):
    import overview_snapshot

    dt = datetime(2099, 6, 10, 20, 30, 0, tzinfo=timezone.utc)
    fake_job = MagicMock()
    fake_job.next_run_time = dt

    fake_sched = MagicMock()
    fake_sched.get_job.return_value = fake_job

    notifs = [_make_notif("personalchef-buongiorno", "Buongiorno", project="personalChef")]
    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: notifs)

    snap = overview_snapshot.build_snapshot(sched=fake_sched)
    n = snap["projects"][0]["notifications"][0]
    assert n["next_run_at"] == "2099-06-10T20:30:00Z"


def test_build_snapshot_next_run_at_none_when_job_missing(monkeypatch):
    import overview_snapshot

    fake_sched = MagicMock()
    fake_sched.get_job.return_value = None

    notifs = [_make_notif("personalchef-buongiorno", "Buongiorno", project="personalChef")]
    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: notifs)

    snap = overview_snapshot.build_snapshot(sched=fake_sched)
    n = snap["projects"][0]["notifications"][0]
    assert n["next_run_at"] is None


def test_build_snapshot_empty_when_no_notifications(monkeypatch):
    import overview_snapshot

    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: [])
    snap = overview_snapshot.build_snapshot()
    assert snap == {"projects": []}


# ---------------------------------------------------------------------------
# overview_snapshot.push_snapshot — mock httpx tests
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_push_snapshot_sends_correct_request(monkeypatch, tmp_path):
    monkeypatch.setenv("OVERVIEW_PAUSES_PATH", str(tmp_path / "pauses.json"))
    monkeypatch.setenv("OVERVIEW_SNAPSHOT_ENABLED", "1")
    import overview_snapshot
    importlib.reload(overview_snapshot)

    notifs = [_make_notif("personalchef-buongiorno", "Buongiorno", project="personalChef")]
    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: notifs)

    posted = {}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"projects": []}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            posted.update(url=url, headers=headers, json=json)
            return FakeResp()

    monkeypatch.setattr(overview_snapshot.httpx, "AsyncClient", FakeClient)

    ok = _run(overview_snapshot.push_snapshot("http://h:8030", "k", "owner-telegram"))
    assert ok is True
    assert posted["url"] == "http://h:8030/api/overview/schedule-snapshot"
    assert posted["headers"]["X-Api-Key"] == "k"
    assert posted["headers"]["X-Agent-Id"] == "owner-telegram"
    assert "projects" in posted["json"]
    assert posted["json"]["projects"][0]["project"] == "personalChef"


def test_push_snapshot_applies_pauses_from_response(monkeypatch, tmp_path):
    monkeypatch.setenv("OVERVIEW_PAUSES_PATH", str(tmp_path / "pauses.json"))
    monkeypatch.setenv("OVERVIEW_SNAPSHOT_ENABLED", "1")
    import overview_snapshot
    import overview_store
    importlib.reload(overview_snapshot)
    importlib.reload(overview_store)

    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: [])

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"projects": [
                {"project": "personalChef", "notifications_paused_until": "2099-06-10T08:00:00Z"}
            ]}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return FakeResp()

    monkeypatch.setattr(overview_snapshot.httpx, "AsyncClient", FakeClient)

    _run(overview_snapshot.push_snapshot("http://h:8030", "k", "owner-telegram"))
    assert overview_store.is_paused("personalChef") is True


def test_push_snapshot_returns_false_on_http_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OVERVIEW_SNAPSHOT_ENABLED", "1")
    import overview_snapshot
    importlib.reload(overview_snapshot)

    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: [])

    class FakeResp:
        status_code = 503
        def raise_for_status(self):
            raise overview_snapshot.httpx.HTTPStatusError(
                "503", request=MagicMock(), response=self
            )
        def json(self): return {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return FakeResp()

    monkeypatch.setattr(overview_snapshot.httpx, "AsyncClient", FakeClient)

    ok = _run(overview_snapshot.push_snapshot("http://h:8030", "k", "owner-telegram"))
    assert ok is False


def test_push_snapshot_returns_false_on_connection_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OVERVIEW_SNAPSHOT_ENABLED", "1")
    import overview_snapshot
    importlib.reload(overview_snapshot)

    monkeypatch.setattr(overview_snapshot.notif_store, "load", lambda: [])

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            raise ConnectionError("timeout")

    monkeypatch.setattr(overview_snapshot.httpx, "AsyncClient", FakeClient)

    ok = _run(overview_snapshot.push_snapshot("http://h:8030", "k", "owner-telegram"))
    assert ok is False


def test_push_snapshot_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("OVERVIEW_SNAPSHOT_ENABLED", "0")
    import overview_snapshot
    importlib.reload(overview_snapshot)

    called = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            called.append(1)
            raise AssertionError("should not be called")

    monkeypatch.setattr(overview_snapshot.httpx, "AsyncClient", FakeClient)
    ok = _run(overview_snapshot.push_snapshot("http://h:8030", "k", "owner-telegram"))
    assert ok is True
    assert called == []


# ---------------------------------------------------------------------------
# scheduler._fire — pause gate tests
# ---------------------------------------------------------------------------

def test_fire_skips_when_project_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERVIEW_PAUSES_PATH", str(tmp_path / "pauses.json"))
    import overview_store as os_mod
    importlib.reload(os_mod)

    # Set a future pause for a work project. NB: since the 2026-07-06 policy the
    # ban gate covers work projects too and runs BEFORE the pause check, so this
    # test would go green without ever exercising the pause. `is_banned` is
    # neutralised below to keep the assertion about the pause, and only the pause.
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    os_mod.set_pause("offerte", future)

    import scheduler
    notif = _make_notif("offerte-buongiorno", "Buongiorno", project="offerte")
    monkeypatch.setattr(scheduler.notif_store, "get", lambda k: notif)
    monkeypatch.setattr(scheduler, "overview_store", os_mod)
    monkeypatch.setattr(scheduler.notifications, "is_banned", lambda n: False)

    provider_called = []

    async def fake_run_provider(notif, *, run_claude):
        provider_called.append(notif.key)
        return "text"

    monkeypatch.setattr(scheduler.providers, "run_provider", fake_run_provider)
    monkeypatch.setitem(scheduler._S, "run_claude", None)
    monkeypatch.setitem(scheduler._S, "send_target", None)

    asyncio.run(scheduler._fire("offerte-buongiorno"))
    assert provider_called == []  # skipped due to pause


def test_fire_delivers_when_not_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERVIEW_PAUSES_PATH", str(tmp_path / "pauses2.json"))
    import overview_store as os_mod
    importlib.reload(os_mod)
    # No pause set

    import scheduler
    notif = _make_notif("offerte-buongiorno", "Buongiorno", project="offerte",
                        provider="static")
    monkeypatch.setattr(scheduler.notif_store, "get", lambda k: notif)
    monkeypatch.setattr(scheduler, "overview_store", os_mod)
    # See the note in test_fire_skips_when_project_paused: the policy ban would
    # short-circuit before delivery. This test is about the delivery path.
    monkeypatch.setattr(scheduler.notifications, "is_banned", lambda n: False)

    provider_called = []

    async def fake_run_provider(n, *, run_claude):
        provider_called.append(n.key)
        return "text!"

    targets_called = []

    async def fake_send_target(chat_id, thread_id, text):
        targets_called.append((chat_id, thread_id))

    monkeypatch.setattr(scheduler.providers, "run_provider", fake_run_provider)
    monkeypatch.setattr(scheduler.notifications, "resolve_targets",
                        lambda notif: [(123, None)])
    monkeypatch.setitem(scheduler._S, "run_claude", None)
    monkeypatch.setitem(scheduler._S, "send_target", fake_send_target)

    asyncio.run(scheduler._fire("offerte-buongiorno"))
    assert provider_called == ["offerte-buongiorno"]
    assert targets_called == [(123, None)]
