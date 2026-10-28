"""Tests for the FastAPI notifications HTTP layer (api.py).

Uses TestClient + monkeypatch to stub notif_store/scheduler/manifest in-memory
so tests run with no filesystem I/O and no live scheduler.
"""
import notif_store
import providers
import scheduler
from notifications import Notification


def _n(key="a", enabled=True, schedule="0 8 * * *", provider="static",
       params=None, destination="dm", recipients=("owner",)):
    return Notification(key, "T", provider, schedule, recipients, enabled,
                        params or {"text": "ciao"}, destination)


def _client(monkeypatch, items):
    """A TestClient with notif_store/scheduler/manifest stubbed in-memory."""
    from fastapi.testclient import TestClient
    import api
    store = {n.key: n for n in items}
    monkeypatch.setattr(notif_store, "load", lambda: list(store.values()))
    monkeypatch.setattr(notif_store, "get", lambda k: store.get(k))
    monkeypatch.setattr(notif_store, "upsert", lambda n: store.__setitem__(n.key, n))
    monkeypatch.setattr(notif_store, "delete",
                        lambda k: (store.pop(k, None) is not None))
    monkeypatch.setattr(notif_store, "load_runs", lambda: {})
    monkeypatch.setattr(api, "_next_run", lambda k: None)
    monkeypatch.setattr(api, "_manifest_keys", lambda: set())
    # Operator contract (#78): writes need a registered instance. Pin one fake.
    import instances
    monkeypatch.setattr(instances, "known_ids", lambda env=None: ["personal"])
    monkeypatch.setattr(instances, "get",
                        lambda i, env=None: object() if i == "personal" else None)
    monkeypatch.setattr(instances, "describe_all",
                        lambda env=None: {"default": "personal", "instances": [], "warnings": []})
    applied, unscheduled = [], []
    monkeypatch.setattr(scheduler, "apply", lambda n: applied.append(n.key))
    monkeypatch.setattr(scheduler, "unschedule", lambda k: unscheduled.append(k))
    client = TestClient(api.app)
    return client, store, applied, unscheduled


# ---------------------------------------------------------------------------
# Task 1: GET /api/notifications
# ---------------------------------------------------------------------------

def test_list_notifications(monkeypatch):
    client, *_ = _client(monkeypatch, [_n("a"), _n("b", enabled=False)])
    r = client.get("/api/notifications")
    assert r.status_code == 200
    data = r.json()["notifications"]
    by_key = {d["key"]: d for d in data}
    assert set(by_key) == {"a", "b"}
    assert by_key["a"]["enabled"] is True
    assert by_key["b"]["enabled"] is False
    assert by_key["a"]["origin"] == "web"
    assert "last_run" in by_key["a"] and "next_run" in by_key["a"]


# ---------------------------------------------------------------------------
# Task 2: POST / PATCH / DELETE
# ---------------------------------------------------------------------------

def test_create_validates_and_applies(monkeypatch):
    client, store, applied, _ = _client(monkeypatch, [])
    payload = {"title": "Promemoria", "provider": "static", "schedule": "0 9 * * *",
               "recipients": ["owner"], "params": {"text": "ciao"}, "destination": "dm",
               "instance": "personal"}
    r = client.post("/api/notifications", json=payload)
    assert r.status_code == 201
    key = r.json()["key"]
    assert key in store and key in applied
    assert store[key].title == "Promemoria"


def test_create_rejects_invalid(monkeypatch):
    client, store, applied, _ = _client(monkeypatch, [])
    # static senza text -> errore di notifications.validate
    payload = {"title": "X", "provider": "static", "schedule": "0 9 * * *",
               "recipients": ["owner"], "params": {}, "destination": "dm",
               "instance": "personal"}
    r = client.post("/api/notifications", json=payload)
    assert r.status_code == 422
    assert r.json()["errors"]                 # lista non vuota
    assert not store and not applied


def test_patch_updates_and_reapplies(monkeypatch):
    client, store, applied, unsched = _client(monkeypatch, [_n("a", enabled=True)])
    r = client.patch("/api/notifications/a", json={"enabled": False, "instance": "personal"})
    assert r.status_code == 200
    assert store["a"].enabled is False
    assert "a" in applied                      # apply() gestisce anche il disable (rimuove il job)


def test_patch_unknown_is_404(monkeypatch):
    client, *_ = _client(monkeypatch, [])
    assert client.patch("/api/notifications/ghost", json={"enabled": False, "instance": "personal"}).status_code == 404


def test_delete_removes_and_unschedules(monkeypatch):
    client, store, _, unsched = _client(monkeypatch, [_n("a")])
    r = client.delete("/api/notifications/a")
    assert r.status_code == 200
    assert "a" not in store and "a" in unsched


# ---------------------------------------------------------------------------
# Task 3: GET /api/meta
# ---------------------------------------------------------------------------

def test_meta_lists_options(monkeypatch):
    client, *_ = _client(monkeypatch, [])
    r = client.get("/api/meta")
    assert r.status_code == 200
    meta = r.json()
    assert {"profiles", "projects", "groups", "presets", "providers"} <= set(meta)
    assert isinstance(meta["providers"], list) and meta["providers"]
    prof_keys = {p["key"] for p in meta["profiles"]}
    assert "owner" in prof_keys                 # dal catalogo profiles.PROFILES
    assert any(p["key"] for p in meta["projects"]) # registry progetti non vuoto
    assert "m8" in meta["presets"]                 # preset notifications._PRESETS


# ---------------------------------------------------------------------------
# Task 4: POST /{key}/test + POST /preview
# ---------------------------------------------------------------------------

def test_test_now_fires(monkeypatch):
    client, *_ = _client(monkeypatch, [_n("a")])
    fired = []

    async def fake_fire(key):
        fired.append(key)
    monkeypatch.setattr(scheduler, "_fire", fake_fire)
    r = client.post("/api/notifications/a/test")
    assert r.status_code == 200
    assert fired == ["a"]


def test_test_now_reports_a_notification_that_was_not_sent(monkeypatch):
    # The endpoint used to answer {"ok": true} whatever happened, so a
    # notification dropped by the policy gate looked delivered. That is how 14
    # dead notifications went unnoticed: the only way to check them said "ok".
    client, *_ = _client(monkeypatch, [_n("a")])

    async def refuse(key):
        return "vietata dalla policy (progetto personal/work)"
    monkeypatch.setattr(scheduler, "_fire", refuse)
    body = client.post("/api/notifications/a/test").json()
    assert body["ok"] is False
    assert "policy" in body["outcome"]


def test_test_now_says_ok_only_when_actually_sent(monkeypatch):
    client, *_ = _client(monkeypatch, [_n("a")])

    async def sent(key):
        return "inviata"
    monkeypatch.setattr(scheduler, "_fire", sent)
    assert client.post("/api/notifications/a/test").json()["ok"] is True


def test_test_now_unknown_is_404(monkeypatch):
    client, *_ = _client(monkeypatch, [])
    assert client.post("/api/notifications/ghost/test").status_code == 404


def test_preview_returns_text_without_sending(monkeypatch):
    client, *_ = _client(monkeypatch, [])
    monkeypatch.setitem(scheduler._S, "run_claude", None)

    async def fake_run_provider(notif, *, run_claude):
        return "ANTEPRIMA"
    monkeypatch.setattr(providers, "run_provider", fake_run_provider)
    payload = {"title": "X", "provider": "static", "schedule": "0 9 * * *",
               "recipients": ["owner"], "params": {"text": "ciao"}, "destination": "dm"}
    r = client.post("/api/notifications/preview", json=payload)
    assert r.status_code == 200
    assert r.json()["text"] == "ANTEPRIMA"


def test_preview_ignores_no_recipients(monkeypatch):
    client, *_ = _client(monkeypatch, [])
    monkeypatch.setitem(scheduler._S, "run_claude", None)

    async def fake_run_provider(notif, *, run_claude):
        return "ANTEPRIMA"
    monkeypatch.setattr(providers, "run_provider", fake_run_provider)
    payload = {"title": "X", "provider": "static", "schedule": "0 9 * * *",
               "recipients": [], "params": {"text": "ciao"}, "destination": "dm"}
    r = client.post("/api/notifications/preview", json=payload)
    assert r.status_code == 200


def test_create_duplicate_key_is_409(monkeypatch):
    client, *_ = _client(monkeypatch, [_n("a")])
    payload = {"key": "a", "title": "X", "provider": "static",
               "schedule": "0 9 * * *", "recipients": ["owner"],
               "params": {"text": "ciao"}, "destination": "dm",
               "instance": "personal"}
    assert client.post("/api/notifications", json=payload).status_code == 409


# ---------------------------------------------------------------------------
# GET /api/notifications/health — the watchdog over mute notifications
# ---------------------------------------------------------------------------


def test_health_endpoint_names_a_mute_notification(monkeypatch):
    from datetime import timedelta

    import notif_health

    client, *_ = _client(monkeypatch, [_n("giornaliera")])
    monkeypatch.setattr(notif_store, "note_seen", lambda keys, when: [])
    old = (notif_health.datetime.now(notif_health._tzobj())
           - timedelta(days=4)).isoformat()
    monkeypatch.setattr(notif_store, "load_outcomes", lambda: {
        "giornaliera": {"first_seen": old, "last_attempt_at": old,
                        "last_outcome": "inviata", "last_category": "sent",
                        "last_sent_at": old, "totals": {}, "recent": []},
    })

    r = client.get("/api/notifications/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "critical"
    assert [a["key"] for a in body["alarms"]] == ["giornaliera"]
    assert body["notifications"][0]["status"] == "silent"
    # The watchdog reports on itself: absent means nobody would notice.
    assert body["watchdog"]["present"] is False


def test_health_route_is_not_swallowed_by_the_key_routes(monkeypatch):
    """'health' must not be matched as a notification key."""
    client, *_ = _client(monkeypatch, [])
    monkeypatch.setattr(notif_store, "note_seen", lambda keys, when: [])
    monkeypatch.setattr(notif_store, "load_outcomes", lambda: {})
    assert client.get("/api/notifications/health").json()["summary"]["total"] == 0


# ---------------------------------------------------------------------------
# POST /api/cards (Coordinator#45)
# ---------------------------------------------------------------------------

def test_post_cards_forwards_payload_token_and_status(monkeypatch):
    from fastapi.testclient import TestClient
    import api
    import remote_cards

    captured = {}

    async def _fake_show(payload, token):
        captured["payload"] = payload
        captured["token"] = token
        return 200, {"ok": True, "message_id": 7}

    monkeypatch.setattr(remote_cards, "show", _fake_show)
    client = TestClient(api.app)
    body = {"source": "tm", "card_id": "rm-1", "target": {"project_key": ""},
            "text": "hi", "buttons": []}
    r = client.post("/api/cards", json=body, headers={"X-Card-Token": "secret"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message_id": 7}
    assert captured["payload"] == body
    assert captured["token"] == "secret"


def test_post_cards_propagates_error_status(monkeypatch):
    from fastapi.testclient import TestClient
    import api
    import remote_cards

    async def _fake_show(payload, token):
        return 403, {"error": "unknown source"}

    monkeypatch.setattr(remote_cards, "show", _fake_show)
    client = TestClient(api.app)
    r = client.post("/api/cards", json={"source": "ghost"})
    assert r.status_code == 403
    assert r.json() == {"error": "unknown source"}


def test_post_cards_invalid_json_body_is_400():
    from fastapi.testclient import TestClient
    import api

    client = TestClient(api.app)
    r = client.post("/api/cards", content="not json",
                     headers={"Content-Type": "application/json"})
    assert r.status_code == 400
