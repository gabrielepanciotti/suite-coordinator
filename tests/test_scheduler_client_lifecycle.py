# tests/test_scheduler_client_lifecycle.py
import asyncio

import pytest

import scheduler_client as hw
from scheduler_client import SchedulerClient, client_from_env


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
    """Monkeypatches httpx.AsyncClient; returns a preset response and records the call."""

    resp = _FakeResp(200, {"id": 7, "notification_state": "completed"})
    last: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.last = {"url": url, "headers": headers, "json": json}
        return _FakeClient.resp


def _patch(monkeypatch, resp):
    _FakeClient.resp = resp
    _FakeClient.last = {}
    monkeypatch.setattr(hw.httpx, "AsyncClient", _FakeClient)


# --- start_task ---


def test_start_task_posts_expected(monkeypatch):
    _patch(monkeypatch, _FakeResp(200, {"id": 7, "notification_state": "started"}))
    c = SchedulerClient("http://hw", "sek", origin="user")
    out = _run(c.start_task(7, telegram_chat_id=542))
    call = _FakeClient.last
    assert call["url"].endswith("/api/agent/scheduled-tasks/7/start")
    assert call["json"] == {"telegram_chat_id": 542}
    assert call["headers"]["X-Agent-Origin"] == "user"
    assert out["notification_state"] == "started"


# --- complete_task ---


def test_complete_task_posts_expected(monkeypatch):
    _patch(monkeypatch, _FakeResp(200, {"id": 7, "notification_state": "completed"}))
    c = SchedulerClient("http://hw", "sek", origin="user")
    out = _run(c.complete_task(7, telegram_chat_id=542))
    call = _FakeClient.last
    assert call["url"].endswith("/api/agent/scheduled-tasks/7/complete")
    assert call["json"] == {"telegram_chat_id": 542}
    assert call["headers"]["X-Agent-Origin"] == "user"
    assert out["notification_state"] == "completed"


# --- postpone_task ---


def test_postpone_task_without_minutes(monkeypatch):
    _patch(
        monkeypatch,
        _FakeResp(
            200,
            {
                "id": 7,
                "notification_state": "postponed",
                "start_at": "2026-07-05T10:00:00",
            },
        ),
    )
    c = SchedulerClient("http://hw", "sek", origin="user")
    out = _run(c.postpone_task(7, telegram_chat_id=542))
    call = _FakeClient.last
    assert call["url"].endswith("/api/agent/scheduled-tasks/7/postpone")
    assert call["json"] == {"telegram_chat_id": 542}
    assert call["headers"]["X-Agent-Origin"] == "user"
    assert out["notification_state"] == "postponed"


def test_postpone_task_with_minutes(monkeypatch):
    _patch(monkeypatch, _FakeResp(200, {"id": 7, "notification_state": "postponed"}))
    c = SchedulerClient("http://hw", "sek", origin="user")
    _run(c.postpone_task(7, telegram_chat_id=542, minutes=15))
    call = _FakeClient.last
    assert call["json"] == {"telegram_chat_id": 542, "minutes": 15}


# --- status -> typed errors reuse ---


@pytest.mark.parametrize(
    "status,exc",
    [
        (401, hw.SchedulerAuthError),
        (404, hw.SchedulerNotFound),
        (409, hw.SchedulerConflict),
    ],
)
def test_lifecycle_status_maps_to_typed_error(monkeypatch, status, exc):
    _patch(monkeypatch, _FakeResp(status, {"detail": "boom"}))
    c = SchedulerClient("http://hw", "sek", origin="user")
    with pytest.raises(exc):
        _run(c.complete_task(7, telegram_chat_id=542))


# --- client_from_env(origin=...) ---


def test_client_from_env_origin(monkeypatch):
    monkeypatch.setenv("SCHEDULER_API_URL", "http://hw")
    monkeypatch.setenv("AGENT_API_SECRET", "s")
    monkeypatch.delenv("SCHEDULER_AGENT_ORIGIN", raising=False)
    assert client_from_env(origin="user").origin == "user"


def test_client_from_env_default_origin_unchanged(monkeypatch):
    monkeypatch.setenv("SCHEDULER_API_URL", "http://hw")
    monkeypatch.setenv("AGENT_API_SECRET", "s")
    monkeypatch.delenv("SCHEDULER_AGENT_ORIGIN", raising=False)
    assert client_from_env().origin == "node:coordinator"


def test_client_from_env_explicit_origin_wins_over_env(monkeypatch):
    """Regression: SCHEDULER_AGENT_ORIGIN (set for #17 proposals) must NOT
    override an explicitly-passed origin (lifecycle callers pass origin="user"
    and need it to actually reach Scheduler as X-Agent-Origin: user)."""
    monkeypatch.setenv("SCHEDULER_API_URL", "http://hw")
    monkeypatch.setenv("AGENT_API_SECRET", "s")
    monkeypatch.setenv("SCHEDULER_AGENT_ORIGIN", "node:coordinator")
    assert client_from_env(origin="user").origin == "user"


def test_client_from_env_falls_back_to_env_when_origin_omitted(monkeypatch):
    """No explicit origin (#17 proposal-creation callers) -> env var still applies."""
    monkeypatch.setenv("SCHEDULER_API_URL", "http://hw")
    monkeypatch.setenv("AGENT_API_SECRET", "s")
    monkeypatch.setenv("SCHEDULER_AGENT_ORIGIN", "node:coordinator")
    assert client_from_env().origin == "node:coordinator"
