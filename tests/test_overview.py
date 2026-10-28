"""Node overview endpoint: GET /api/overview returns the scheda-nodo payload.

Uses TestClient on api.app (same pattern as test_api.py). The live DB section
is stubbed via monkeypatch so tests never require a running Postgres.
"""
from pathlib import Path

import overview


def _client():
    from fastapi.testclient import TestClient
    import api
    return TestClient(api.app)


def test_health_ok():
    r = _client().get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "node": "coordinator"}


def test_overview_ok(monkeypatch):
    monkeypatch.setattr(
        overview, "_query_database_section",
        lambda specs: {"tables": [{"name": "notifications", "rows": 3}],
                       "highlights": [{"label": "Notifiche schedulate", "value": 3}]},
    )
    r = _client().get("/api/overview")
    assert r.status_code == 200
    body = r.json()

    assert body["live"] is True

    identity = body["identity"]
    assert identity["slug"] == "coordinator"
    assert identity["name"] == "Coordinator"
    assert identity["type"] == "bridge"

    assert isinstance(body["functions"], list)
    assert len(body["functions"]) > 0

    assert "collaborations" in body
    assert "brain" in body

    database = body["database"]
    assert database["tables"] == [{"name": "notifications", "rows": 3}]
    assert database["highlights"][0]["label"] == "Notifiche schedulate"
    assert "error" not in database


def test_overview_db_down_degrades_gracefully(monkeypatch):
    """DB unreachable -> database.error + live false, rest of payload intact."""
    monkeypatch.setattr(
        overview, "_query_database_section",
        lambda specs: {"tables": [], "highlights": [], "error": "database query failed"},
    )
    r = _client().get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is False
    assert body["database"]["error"] == "database query failed"
    assert "identity" in body and "functions" in body


def test_overview_manifest_missing(monkeypatch, tmp_path):
    """Manifest absent -> minimal identity + live false + warning."""
    monkeypatch.setattr(overview, "_MANIFEST_PATH", Path(tmp_path) / "nope.json")
    r = _client().get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is False
    assert body["identity"]["slug"] == "coordinator"
    assert "warning" in body


def test_overview_public_no_auth():
    """Public like /api/health: no header/cookie/token required (the whole
    notifications API is LAN no-auth; the overview must never regress that)."""
    r = _client().get("/api/overview")
    assert r.status_code == 200
