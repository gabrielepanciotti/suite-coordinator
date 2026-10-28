# tests/test_scheduler_client_taskgen.py
"""taskgen (Slice B) additions to SchedulerClient: one-off scheduled tasks +
node-scoped proposals. Mirrors the monkeypatch-httpx.AsyncClient pattern used
by tests/test_scheduler_client_lifecycle.py (no respx in this project)."""
import asyncio

import pytest

import scheduler_client as hw
from scheduler_client import SchedulerClient


def _run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """Monkeypatches httpx.AsyncClient; records the last call (POST or DELETE)."""

    resp = _FakeResp(200, {})
    last: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.last = {"method": "POST", "url": url, "headers": headers, "json": json}
        return _FakeClient.resp

    async def delete(self, url, headers=None, params=None):
        _FakeClient.last = {
            "method": "DELETE",
            "url": url,
            "headers": headers,
            "params": params,
        }
        return _FakeClient.resp


def _patch(monkeypatch, resp):
    _FakeClient.resp = resp
    _FakeClient.last = {}
    monkeypatch.setattr(hw.httpx, "AsyncClient", _FakeClient)


# --- create_scheduled_task ---


def test_create_scheduled_task_sends_user_origin(monkeypatch):
    _patch(
        monkeypatch,
        _FakeResp(
            201,
            {"id": 7, "notification_state": "pending", "start_at": "2026-07-07T20:00:00+02:00"},
        ),
    )
    c = SchedulerClient(base_url="http://hw.test", secret="s")
    out = _run(
        c.create_scheduled_task(
            telegram_chat_id=542,
            name="Cena",
            category_id=3,
            start_at="2026-07-07T20:00:00+02:00",
            duration_min=45,
            description=None,
        )
    )
    call = _FakeClient.last
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/agent/scheduled-tasks")
    assert call["headers"]["X-Agent-Origin"] == "user"
    assert call["headers"]["X-Agent-Secret"] == "s"
    assert call["json"] == {
        "telegram_chat_id": 542,
        "name": "Cena",
        "category_id": 3,
        "start_at": "2026-07-07T20:00:00+02:00",
        "duration_min": 45,
        "description": None,
    }
    assert out["id"] == 7


def test_create_scheduled_task_raises_typed_error_on_403(monkeypatch):
    _patch(monkeypatch, _FakeResp(403, {"detail": "nope"}))
    c = SchedulerClient(base_url="http://hw.test", secret="s")
    with pytest.raises(hw.SchedulerForbidden):
        _run(
            c.create_scheduled_task(
                telegram_chat_id=542,
                name="Cena",
                category_id=None,
                start_at="2026-07-07T20:00:00+02:00",
                duration_min=45,
                description=None,
            )
        )


# --- delete_scheduled_task ---


def test_delete_scheduled_task_sends_user_origin_and_query(monkeypatch):
    _patch(monkeypatch, _FakeResp(204))
    c = SchedulerClient(base_url="http://hw.test", secret="s")
    out = _run(c.delete_scheduled_task(st_id=7, telegram_chat_id=542))
    call = _FakeClient.last
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/api/agent/scheduled-tasks/7")
    assert call["headers"]["X-Agent-Origin"] == "user"
    assert call["params"] == {"telegram_chat_id": 542}
    assert out is None


def test_delete_scheduled_task_raises_typed_error_on_404(monkeypatch):
    _patch(monkeypatch, _FakeResp(404, {"detail": "gone"}))
    c = SchedulerClient(base_url="http://hw.test", secret="s")
    with pytest.raises(hw.SchedulerNotFound):
        _run(c.delete_scheduled_task(st_id=7, telegram_chat_id=542))


# --- create_proposal(node_slug=...) ---


def test_create_proposal_sends_node_origin(monkeypatch):
    _patch(monkeypatch, _FakeResp(201, {"id": 1}))
    c = SchedulerClient(base_url="http://hw.test", secret="s")
    out = _run(
        c.create_proposal(
            telegram_chat_id=542,
            node_slug="cheftoni",
            kind="schedulable",
            summary="Cuciniamo",
            payload={"name": "Cena"},
        )
    )
    call = _FakeClient.last
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/agent/proposals")
    assert call["headers"]["X-Agent-Origin"] == "node:cheftoni"
    assert call["headers"]["X-Agent-Secret"] == "s"
    assert call["json"] == {
        "kind": "schedulable",
        "summary": "Cuciniamo",
        "payload": {"name": "Cena"},
        "telegram_chat_id": 542,
    }
    assert out["id"] == 1


def test_create_proposal_rejects_uppercase_node_slug(monkeypatch):
    """Server gate (_ORIGIN_RE) only accepts node:[a-z0-9-]+ — camelCase must
    be rejected client-side rather than sent and 422'd by Scheduler."""
    _patch(monkeypatch, _FakeResp(201, {"id": 1}))
    c = SchedulerClient(base_url="http://hw.test", secret="s")
    with pytest.raises(hw.SchedulerValidationError):
        _run(
            c.create_proposal(
                telegram_chat_id=542,
                node_slug="chefToni",
                kind="schedulable",
                summary="Cuciniamo",
                payload={"name": "Cena"},
            )
        )


def test_list_categories_parses(monkeypatch):
    c = SchedulerClient(base_url="http://hw.test", secret="s")

    async def fake_get(path, headers=None):
        assert path == "/api/agent/categories"
        return [{"id": 1, "name": "cucina", "display_name": "Cucina"}]

    monkeypatch.setattr(c, "_get", fake_get)
    out = _run(c.list_categories())
    assert out == [{"id": 1, "name": "cucina", "display_name": "Cucina"}]


def test_create_scheduled_task_rejects_nonpositive_duration():
    c = SchedulerClient(base_url="http://hw.test", secret="s")
    with pytest.raises(hw.SchedulerValidationError):
        _run(
            c.create_scheduled_task(
                telegram_chat_id=1,
                name="X",
                category_id=None,
                start_at="2026-07-10T20:00:00+02:00",
                duration_min=0,
            )
        )


def test_create_proposal_without_node_slug_keeps_instance_origin(monkeypatch):
    """Regression: existing #17 callers (no node_slug) must keep using
    self.origin unchanged — the per-call override is opt-in."""
    _patch(monkeypatch, _FakeResp(201, {"id": 2}))
    c = SchedulerClient(base_url="http://hw.test", secret="s", origin="node:coordinator")
    _run(
        c.create_proposal(
            telegram_chat_id=542,
            kind="preference",
            summary="Finestra riposo",
            payload={"target": "rest_window"},
        )
    )
    call = _FakeClient.last
    assert call["headers"]["X-Agent-Origin"] == "node:coordinator"
