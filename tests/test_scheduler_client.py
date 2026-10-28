# tests/test_scheduler_client.py
import asyncio

import pytest

import scheduler_client as hw


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
    resp = _FakeResp(201, {"id": 7, "status": "proposed"})
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


# --- client construction / config ---

def test_not_configured_raises():
    with pytest.raises(hw.SchedulerNotConfigured):
        hw.SchedulerClient("", "secret")
    with pytest.raises(hw.SchedulerNotConfigured):
        hw.SchedulerClient("http://h", "")


def test_client_from_env(monkeypatch):
    monkeypatch.setenv("SCHEDULER_API_URL", "http://scheduler:8011/")
    monkeypatch.setenv("AGENT_API_SECRET", "s3cr3t")
    monkeypatch.delenv("SCHEDULER_AGENT_ORIGIN", raising=False)
    c = hw.client_from_env()
    assert c.base_url == "http://scheduler:8011"          # trailing slash stripped
    assert c.origin == "node:coordinator"                  # default
    assert c.headers["X-Agent-Secret"] == "s3cr3t"


def test_client_from_env_missing_raises(monkeypatch):
    monkeypatch.delenv("SCHEDULER_API_URL", raising=False)
    monkeypatch.delenv("AGENT_API_SECRET", raising=False)
    with pytest.raises(hw.SchedulerNotConfigured):
        hw.client_from_env()


def test_invalid_origin_rejected():
    with pytest.raises(hw.SchedulerValidationError):
        hw.SchedulerClient("http://h", "s", origin="Node:Coordinator")


# --- create_proposal: happy path + headers/body ---

def test_create_proposal_posts_with_auth_headers(monkeypatch):
    _patch(monkeypatch, _FakeResp(201, {"id": 7, "status": "proposed"}))
    c = hw.SchedulerClient("http://scheduler:8011", "s3cr3t")
    out = _run(c.create_proposal(
        kind="preference", summary="Riposo 23:00–07:00",
        payload={"target": "rest_window", "rest_start": "23:00", "rest_end": "07:00"},
        telegram_chat_id=42,
    ))
    assert out == {"id": 7, "status": "proposed"}
    call = _FakeClient.last
    assert call["url"] == "http://scheduler:8011/api/agent/proposals"
    assert call["headers"]["X-Agent-Secret"] == "s3cr3t"
    assert call["headers"]["X-Agent-Origin"] == "node:coordinator"
    assert call["json"]["kind"] == "preference"
    assert call["json"]["telegram_chat_id"] == 42
    assert "user_id" not in call["json"]


def test_create_proposal_accepts_200(monkeypatch):
    _patch(monkeypatch, _FakeResp(200, {"id": 1}))
    c = hw.SchedulerClient("http://h", "s")
    assert _run(c.create_proposal(
        kind="preference", summary="x", payload={}, user_id=5)) == {"id": 1}


# --- create_proposal: target / arg validation ---

def test_requires_exactly_one_target(monkeypatch):
    _patch(monkeypatch, _FakeResp(201, {"id": 1}))
    c = hw.SchedulerClient("http://h", "s")
    with pytest.raises(hw.SchedulerValidationError):
        _run(c.create_proposal(kind="preference", summary="x", payload={}))
    with pytest.raises(hw.SchedulerValidationError):
        _run(c.create_proposal(kind="preference", summary="x", payload={},
                               telegram_chat_id=1, user_id=2))


def test_bad_kind_and_summary(monkeypatch):
    _patch(monkeypatch, _FakeResp(201, {"id": 1}))
    c = hw.SchedulerClient("http://h", "s")
    with pytest.raises(hw.SchedulerValidationError):
        _run(c.create_proposal(kind="nope", summary="x", payload={}, user_id=1))
    with pytest.raises(hw.SchedulerValidationError):
        _run(c.create_proposal(kind="preference", summary="", payload={}, user_id=1))
    with pytest.raises(hw.SchedulerValidationError):
        _run(c.create_proposal(kind="preference", summary="a" * 201, payload={}, user_id=1))


# --- create_proposal: HTTP status -> typed errors ---

@pytest.mark.parametrize("status,exc", [
    (401, hw.SchedulerAuthError),
    (403, hw.SchedulerForbidden),
    (404, hw.SchedulerNotFound),
    (409, hw.SchedulerConflict),
    (422, hw.SchedulerValidationError),
    (500, hw.SchedulerError),
])
def test_status_maps_to_typed_error(monkeypatch, status, exc):
    _patch(monkeypatch, _FakeResp(status, {"detail": "boom"}))
    c = hw.SchedulerClient("http://h", "s")
    with pytest.raises(exc) as ei:
        _run(c.create_proposal(kind="preference", summary="x", payload={}, user_id=1))
    assert "boom" in str(ei.value)


def test_transport_error_wrapped(monkeypatch):
    class _Boom(_FakeClient):
        async def post(self, *a, **k):
            raise hw.httpx.ConnectError("refused")
    monkeypatch.setattr(hw.httpx, "AsyncClient", _Boom)
    c = hw.SchedulerClient("http://h", "s")
    with pytest.raises(hw.SchedulerError) as ei:
        _run(c.create_proposal(kind="preference", summary="x", payload={}, user_id=1))
    assert "unreachable" in str(ei.value)


# --- build_proposal (pure) ---

def test_build_preference_window():
    summary, payload = hw.build_proposal("preference", ["23:00", "07:00"])
    assert payload == {"target": "rest_window", "rest_start": "23:00", "rest_end": "07:00"}
    assert "23:00" in summary and "07:00" in summary


def test_build_preference_reset():
    summary, payload = hw.build_proposal("preference", [])
    assert payload == {"target": "rest_window"}
    assert "zzera" in summary.lower() or "reset" in summary.lower()


def test_build_preference_bad_time():
    with pytest.raises(ValueError):
        hw.build_proposal("preference", ["25:00", "07:00"])
    with pytest.raises(ValueError):
        hw.build_proposal("preference", ["23:00"])          # wrong arg count


def test_build_fixed_event_all_day():
    summary, payload = hw.build_proposal("fixed_event", ["2026-07-05"])
    assert payload == {"date": "2026-07-05", "type": "busy"}


def test_build_fixed_event_window():
    summary, payload = hw.build_proposal("fixed_event", ["2026-07-05", "18:00", "20:00"])
    assert payload["start_time"] == "18:00" and payload["end_time"] == "20:00"
    assert payload["date"] == "2026-07-05"


def test_build_fixed_event_bad():
    with pytest.raises(ValueError):
        hw.build_proposal("fixed_event", ["05-07-2026"])     # wrong date shape
    with pytest.raises(ValueError):
        hw.build_proposal("fixed_event", [])                 # missing date


def test_build_schedulable_not_via_command():
    with pytest.raises(ValueError):
        hw.build_proposal("schedulable", ["Cena"])
