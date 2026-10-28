# tests/test_hub_client.py
import asyncio
import hub_client


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
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def post(self, url, headers=None, json=None):
        _FakeClient.last = {"method": "POST", "url": url, "headers": headers, "json": json}
        return _FakeResp({"id": 99})
    async def get(self, url, headers=None, params=None):
        _FakeClient.last = {"method": "GET", "url": url, "headers": headers,
                            "params": params}
        return _FakeResp([{"id": 1}])
    async def patch(self, url, headers=None, json=None):
        _FakeClient.last = {"method": "PATCH", "url": url, "headers": headers, "json": json}
        return _FakeResp({"id": 1, "status": "resolved"})


def test_send_message_posts_with_auth(monkeypatch):
    monkeypatch.setattr(hub_client.httpx, "AsyncClient", _FakeClient)
    c = hub_client.HubClient("http://h:8030", "k", "owner-telegram")
    out = _run(c.send_message(5, "ciao", needs_human=False))
    assert out == {"id": 99}
    assert _FakeClient.last["url"].endswith("/api/conversations/5/messages")
    assert _FakeClient.last["headers"]["X-Api-Key"] == "k"
    assert _FakeClient.last["headers"]["X-Agent-Id"] == "owner-telegram"
    assert _FakeClient.last["json"] == {"body": "ciao", "needs_human": False, "context": None}


def test_create_conversation(monkeypatch):
    monkeypatch.setattr(hub_client.httpx, "AsyncClient", _FakeClient)
    c = hub_client.HubClient("http://h:8030", "k", "owner-telegram")
    out = _run(c.create_conversation(["personalChef-local"], "subj", "body", priority="high"))
    assert out == {"id": 99}
    assert _FakeClient.last["json"]["to"] == ["personalChef-local"]
    assert _FakeClient.last["json"]["priority"] == "high"


def test_get_inbox(monkeypatch):
    monkeypatch.setattr(hub_client.httpx, "AsyncClient", _FakeClient)
    c = hub_client.HubClient("http://h:8030", "k", "owner-telegram")
    out = _run(c.get_inbox())
    assert out == [{"id": 1}]
    assert _FakeClient.last["url"].endswith("/api/inbox")


def test_list_agents_is_tenant_scoped(monkeypatch):
    monkeypatch.setattr(hub_client.httpx, "AsyncClient", _FakeClient)
    c = hub_client.HubClient("http://h:8030", "k", "cosmos")
    out = _run(c.list_agents("personale"))
    assert out == [{"id": 1}]
    assert _FakeClient.last["url"].endswith("/api/agents")
    assert _FakeClient.last["params"] == {"tenant": "personale"}


def test_resolve(monkeypatch):
    monkeypatch.setattr(hub_client.httpx, "AsyncClient", _FakeClient)
    c = hub_client.HubClient("http://h:8030", "k", "owner-telegram")
    out = _run(c.resolve_conversation(1))
    assert out["status"] == "resolved"
    assert _FakeClient.last["json"] == {"status": "resolved"}


def test_parse_sse_yields_events():
    lines = [
        "event: new_message",
        'data: {"conversation_id": 5, "from_agent": "etl-local", "needs_human": true}',
        "id: 17",
        "",
        ":keepalive",
        "event: status_changed",
        'data: {"conversation_id": 5, "new_status": "resolved"}',
        "id: 18",
        "",
    ]

    async def _aiter():
        for ln in lines:
            yield ln

    async def _collect():
        out = []
        async for ev in hub_client.parse_sse(_aiter()):
            out.append(ev)
        return out

    events = asyncio.run(_collect())
    assert events[0] == ("new_message", {"conversation_id": 5, "from_agent": "etl-local", "needs_human": True}, 17)
    assert events[1] == ("status_changed", {"conversation_id": 5, "new_status": "resolved"}, 18)
