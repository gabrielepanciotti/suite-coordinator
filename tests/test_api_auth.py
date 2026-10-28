"""COORDINATOR_API_TOKEN on the notifications API (Coordinator#88).

Deny by default: every route wants the token except the public node card and
the routes that check a secret of their own.
"""
import pytest
from fastapi.testclient import TestClient

import api
import notif_store
from conftest import API_TEST_TOKEN

# Routes that write, cost, or expose configuration: all closed without token.
PROTECTED = [
    ("GET", "/api/notifications"),
    ("GET", "/api/instances"),
    ("GET", "/api/meta"),
    ("GET", "/api/dm-replies"),
    ("GET", "/api/notifications/health"),
    ("GET", "/api/conversations/1/sessions"),
    ("POST", "/api/notifications"),
    ("PATCH", "/api/notifications/x"),
    ("DELETE", "/api/notifications/x"),
    ("POST", "/api/notifications/x/test"),
    ("POST", "/api/notifications/preview"),
    ("POST", "/api/route/preview"),
]


@pytest.fixture
def anon(monkeypatch):
    """A client without the token the suite gives every TestClient."""
    monkeypatch.setattr(notif_store, "get", lambda k: None)
    monkeypatch.setattr(notif_store, "delete", lambda k: False)
    client = TestClient(api.app)
    client.headers.pop("X-Api-Token")
    return client


@pytest.mark.parametrize("method,path", PROTECTED)
def test_protected_routes_are_401_without_token(anon, method, path):
    r = anon.request(method, path, json={})
    assert r.status_code == 401, (method, path, r.text)


@pytest.mark.parametrize("method,path", PROTECTED)
def test_wrong_token_is_401(anon, method, path):
    r = anon.request(method, path, json={}, headers={"X-Api-Token": "wrong"})
    assert r.status_code == 401


def test_right_token_passes_the_gate(anon):
    # 404 from the route itself, not 401 from the gate.
    r = anon.delete("/api/notifications/x", headers={"X-Api-Token": API_TEST_TOKEN})
    assert r.status_code == 404
    r = anon.delete("/api/notifications/x",
                    headers={"Authorization": f"Bearer {API_TEST_TOKEN}"})
    assert r.status_code == 404


def test_unset_token_closes_the_api(anon, monkeypatch):
    """No token configured on the device = closed (503), never open."""
    monkeypatch.delenv("COORDINATOR_API_TOKEN")
    assert anon.get("/api/meta").status_code == 503
    assert anon.get("/api/meta", headers={"X-Api-Token": ""}).status_code == 503


def test_public_node_card_stays_open(anon):
    assert anon.get("/api/health").status_code == 200


def test_routes_with_their_own_secret_skip_the_gate(anon, monkeypatch):
    monkeypatch.delenv("VISION_TOKEN", raising=False)
    # vision: its own VISION_TOKEN check answers (503 unset), not the API gate.
    r = anon.post("/api/vision/receipt", content=b"x",
                  headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 503 and "VISION_TOKEN" in r.json()["detail"]
    # cards: remote_cards checks X-Card-Token; a bad body is its 400.
    assert anon.post("/api/cards", content=b"nope").status_code == 400
    # A2A webhook: answered by its own token/enablement check, not by the gate.
    assert anon.post("/a2a/webhook/t/a", json={}).status_code != 401


def test_schema_and_docs_are_not_served(anon):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert anon.get(path).status_code == 404
