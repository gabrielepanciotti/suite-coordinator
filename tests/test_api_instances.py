"""API instance selector: ?instance= reads, required writes, 422, degradation.

Coordinator#78 / Life §4.3. Two fake instances plus one registered-but-missing
(``comunity``) exercise the read filter, the required-on-write rule, the named
422 and the additive backfill of rows written before the migration.
"""

from __future__ import annotations

import json

import api
import notif_store


def _setup(tmp_path, monkeypatch):
    home = tmp_path / "life-home"
    home.mkdir()
    personal = tmp_path / "life-personal"
    work = tmp_path / "life-work"
    personal.mkdir()
    work.mkdir()
    (home / "instances.json").write_text(json.dumps({
        "version": 1,
        "instances": {
            "personal": {"path": str(personal), "profile": "personal", "band": 0},
            "work": {"path": str(work), "profile": "work", "band": 1},
            "comunity": {"path": str(tmp_path / "life-comunity"),
                         "profile": "comunity", "band": 2},
        },
    }), encoding="utf-8")
    (home / "state.json").write_text(
        json.dumps({"version": 1, "default": "personal", "nextBand": 3}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LIFE_HOME", str(home))
    monkeypatch.delenv("LIFE_INSTANCE", raising=False)
    monkeypatch.setattr(notif_store, "NOTIF_PATH", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(notif_store, "RUNS_PATH", str(tmp_path / "runs.json"))
    monkeypatch.setattr(notif_store, "OUTCOMES_PATH", str(tmp_path / "outcomes.json"))
    monkeypatch.setattr(api, "_manifest_keys", lambda: set())
    monkeypatch.setattr(api, "_next_run", lambda k: None)
    import scheduler
    monkeypatch.setattr(scheduler, "apply", lambda n: None)
    monkeypatch.setattr(scheduler, "unschedule", lambda k: None)
    from fastapi.testclient import TestClient
    return TestClient(api.app), tmp_path


def _payload(**over):
    base = {"title": "Promemoria", "provider": "static", "schedule": "0 9 * * *",
            "recipients": ["owner"], "params": {"text": "ciao"}, "destination": "dm"}
    base.update(over)
    return base


def test_instances_endpoint_names_degradation(tmp_path, monkeypatch):
    client, _ = _setup(tmp_path, monkeypatch)
    r = client.get("/api/instances")
    assert r.status_code == 200
    body = r.json()
    assert body["default"] == "personal"
    by_id = {row["id"]: row for row in body["instances"]}
    assert by_id["personal"]["status"] == "ok"
    assert by_id["comunity"]["status"] == "unreachable"
    assert by_id["comunity"]["reason"]


def test_write_without_instance_uses_device_default(tmp_path, monkeypatch, caplog):
    """A create omitting `instance` is NOT a 422: it lands on the device default.

    Legacy writers predate multi-instance (manifest seed, wizard, ComUnity's
    announce scripts) — a hard 422 would break them silently. The default of
    this device is `personal` (state.json)."""
    client, _ = _setup(tmp_path, monkeypatch)
    with caplog.at_level("WARNING", logger="coordinator.api"):
        r = client.post("/api/notifications", json=_payload())
    assert r.status_code == 201
    rows = client.get("/api/notifications").json()["notifications"]
    assert rows[0]["instance"] == "personal"
    assert any("default del device" in m for m in caplog.messages)


def test_write_rejects_unknown_instance(tmp_path, monkeypatch):
    client, _ = _setup(tmp_path, monkeypatch)
    r = client.post("/api/notifications", json=_payload(instance="ghost"))
    assert r.status_code == 422
    assert "ghost" in r.json()["detail"]


def test_read_filter_and_badge(tmp_path, monkeypatch):
    client, _ = _setup(tmp_path, monkeypatch)
    assert client.post("/api/notifications",
                       json=_payload(instance="work")).status_code == 201
    assert client.post("/api/notifications",
                       json=_payload(title="Altra", instance="personal")).status_code == 201

    work_only = client.get("/api/notifications?instance=work").json()
    assert [n["instance"] for n in work_only["notifications"]] == ["work"]

    every = client.get("/api/notifications").json()["notifications"]
    assert {n["instance"] for n in every} == {"work", "personal"}


def test_read_unknown_instance_is_422(tmp_path, monkeypatch):
    client, _ = _setup(tmp_path, monkeypatch)
    r = client.get("/api/notifications?instance=ghost")
    assert r.status_code == 422
    assert "ghost" in r.json()["detail"]


def test_existing_rows_backfilled_with_default_instance(tmp_path, monkeypatch):
    client, tmp_path = _setup(tmp_path, monkeypatch)
    (tmp_path / "notifications.json").write_text(json.dumps({
        "notifications": [{
            "key": "legacy", "title": "Vecchia", "provider": "static",
            "schedule": "0 9 * * *", "recipients": [], "params": {"text": "x"},
            "destination": "dm",
        }],
    }), encoding="utf-8")
    rows = client.get("/api/notifications").json()["notifications"]
    assert rows[0]["instance"] == "personal"  # the instance that produced it
    assert client.get("/api/notifications?instance=personal").json()["notifications"]


def test_patch_without_instance_keeps_row_instance(tmp_path, monkeypatch):
    """A PATCH omitting `instance` (the ComUnity announce scripts) keeps the
    row's own instance instead of being rejected or relabelled to the default."""
    client, _ = _setup(tmp_path, monkeypatch)
    assert client.post("/api/notifications",
                       json=_payload(instance="work")).status_code == 201
    key = client.get("/api/notifications").json()["notifications"][0]["key"]
    ok = client.patch(f"/api/notifications/{key}", json={"enabled": False})
    assert ok.status_code == 200
    rows = client.get("/api/notifications").json()["notifications"]
    assert rows[0]["instance"] == "work"
    changed = client.patch(f"/api/notifications/{key}",
                           json={"enabled": True, "instance": "personal"})
    assert changed.status_code == 200
    rows = client.get("/api/notifications").json()["notifications"]
    assert rows[0]["instance"] == "personal"


def test_route_preview_reads_the_requested_instance(tmp_path, monkeypatch):
    """#78: a dry run in an instance's room must use THAT instance's graph."""
    import bus_agents
    import life_registry_client
    import node_routing
    import reparto_routing

    client, _ = _setup(tmp_path, monkeypatch)
    seen: list[str | None] = []

    def _graph(*, instance=None, **kw):
        seen.append(instance)
        return {"travel": {"id": "travel", "children": []}}

    async def _route(text, root, nodes, *a, **kw):
        return node_routing.Decision("root", root)

    monkeypatch.setattr(life_registry_client, "load_graph_nodes", _graph)
    monkeypatch.setattr(reparto_routing, "route", _route)
    monkeypatch.setattr(bus_agents, "instance_for_node", lambda node, env=None: "work")

    body = client.post("/api/route/preview",
                       json={"text": "ciao", "root": "travel", "instance": "work"})
    assert body.status_code == 200
    assert body.json()["instance"] == "work"
    assert seen == ["work"]

    body = client.post("/api/route/preview", json={"text": "ciao", "root": "travel"})
    assert body.status_code == 200
    assert body.json()["instance"] == "work"
    assert seen == ["work", "work"]

    bad = client.post("/api/route/preview",
                      json={"text": "ciao", "root": "travel", "instance": "ghost"})
    assert bad.status_code == 422
