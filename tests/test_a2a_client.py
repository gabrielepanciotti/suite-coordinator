# tests/test_a2a_client.py
import asyncio

import pytest

import a2a_client


def _run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    last = {}
    post_payload = {"jsonrpc": "2.0", "id": "co-1", "result": {"id": "5", "kind": "task"}}
    get_payload = {"name": "personalChef-local", "url": "http://h/a2a/agents/personalChef-local"}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.last = {"method": "POST", "url": url, "headers": headers, "json": json}
        return _FakeResp(_FakeClient.post_payload)

    async def get(self, url, headers=None):
        _FakeClient.last = {"method": "GET", "url": url, "headers": headers}
        return _FakeResp(_FakeClient.get_payload)


def _patch(monkeypatch):
    monkeypatch.setattr(a2a_client.httpx, "AsyncClient", _FakeClient)
    return a2a_client.A2AClient("http://h:8030", "k", "owner-telegram")


# --- message/send ---------------------------------------------------------
def test_send_starts_new_task(monkeypatch):
    c = _patch(monkeypatch)
    out = _run(c.send_message("personalChef-local", "ciao", subject="hi", priority="high"))
    assert out == {"id": "5", "kind": "task"}
    body = _FakeClient.last["json"]
    assert _FakeClient.last["url"].endswith("/a2a/agents/personalChef-local")
    assert _FakeClient.last["headers"]["X-Api-Key"] == "k"
    assert _FakeClient.last["headers"]["X-Agent-Id"] == "owner-telegram"
    assert body["jsonrpc"] == "2.0" and body["method"] == "message/send"
    msg = body["params"]["message"]
    assert msg["role"] == "user"
    assert msg["parts"][0] == {"kind": "text", "text": "ciao"}
    assert "taskId" not in msg  # new task
    assert msg["metadata"] == {"subject": "hi", "priority": "high"}


def test_send_appends_with_task_id(monkeypatch):
    c = _patch(monkeypatch)
    _run(c.send_message("personalChef-local", "altro", task_id=42))
    msg = _FakeClient.last["json"]["params"]["message"]
    assert msg["taskId"] == "42" and msg["contextId"] == "42"


def test_send_with_data_adds_datapart(monkeypatch):
    c = _patch(monkeypatch)
    _run(c.send_message("personalChef-local", "ctx", data={"k": "v"}))
    parts = _FakeClient.last["json"]["params"]["message"]["parts"]
    kinds = {p["kind"] for p in parts}
    assert kinds == {"text", "data"}


# --- tasks/get + tasks/cancel --------------------------------------------
def test_get_task(monkeypatch):
    c = _patch(monkeypatch)
    _run(c.get_task("personalChef-local", 7))
    body = _FakeClient.last["json"]
    assert body["method"] == "tasks/get" and body["params"] == {"id": "7"}


def test_cancel_task(monkeypatch):
    c = _patch(monkeypatch)
    _run(c.cancel_task("personalChef-local", 7))
    assert _FakeClient.last["json"]["method"] == "tasks/cancel"


# --- error envelope -------------------------------------------------------
def test_rpc_error_raises(monkeypatch):
    _FakeClient.post_payload = {
        "jsonrpc": "2.0", "id": "co-1",
        "error": {"code": -32602, "message": "Agent not found"},
    }
    try:
        c = _patch(monkeypatch)
        with pytest.raises(a2a_client.A2AError) as ei:
            _run(c.send_message("ghost", "hi"))
        assert ei.value.code == -32602
    finally:
        _FakeClient.post_payload = {
            "jsonrpc": "2.0", "id": "co-1", "result": {"id": "5", "kind": "task"}
        }


# --- discovery ------------------------------------------------------------
def test_get_agent_card(monkeypatch):
    c = _patch(monkeypatch)
    out = _run(c.get_agent_card("personalChef-local"))
    assert out["name"] == "personalChef-local"
    assert _FakeClient.last["url"].endswith(
        "/a2a/agents/personalChef-local/.well-known/agent-card.json"
    )


def test_list_agent_cards(monkeypatch):
    _FakeClient.get_payload = {"agents": [{"name": "a"}, {"name": "b"}]}
    try:
        c = _patch(monkeypatch)
        out = _run(c.list_agent_cards())
        assert [a["name"] for a in out] == ["a", "b"]
        assert _FakeClient.last["url"].endswith("/a2a/agents")
    finally:
        _FakeClient.get_payload = {
            "name": "personalChef-local", "url": "http://h/a2a/agents/personalChef-local"
        }
