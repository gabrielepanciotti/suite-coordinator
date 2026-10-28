# tests/test_a2a_webhook.py
"""A2A inbound webhook: event mapping, dispatch routing, registration, token."""
import asyncio

import a2a_webhook
import hub
import hub_config


def _cfg(**over):
    base = dict(
        hub_url="http://h:8030",
        api_keys={"personale": "k"},
        telegram_agent="owner-telegram",
        telegram_chat_id=-100,
        local_agents={"personalChef-local": "personalChef"},
        role="hub",
    )
    base.update(over)
    return hub_config.HubConfig(**base)


def _evt(**meta):
    m = {"busEvent": "new_message", "from": "personalChef-local", "needsHuman": True,
         "subject": "Serve OK", "priority": "high", "messageId": 7}
    m.update(meta)
    return {
        "taskId": "5", "contextId": "5", "kind": "status-update",
        "status": {"state": "working",
                   "message": {"role": "agent",
                               "parts": [{"kind": "text", "text": "confermi?"}]}},
        "final": False, "metadata": m,
    }


# --- mapping --------------------------------------------------------------
def test_webhook_to_bus_new_message():
    ev_type, data = hub._webhook_to_bus(_evt())
    assert ev_type == "new_message"
    assert data["conversation_id"] == 5
    assert data["from_agent"] == "personalChef-local"
    assert data["body"] == "confermi?"
    assert data["needs_human"] is True
    assert data["subject"] == "Serve OK"
    assert data["message_id"] == 7


def test_webhook_to_bus_status_changed():
    evt = {"taskId": "5", "final": True,
           "status": {"state": "completed"},
           "metadata": {"busEvent": "status_changed", "newStatus": "resolved"}}
    ev_type, data = hub._webhook_to_bus(evt)
    assert ev_type == "status_changed"
    assert data["new_status"] == "resolved"


def test_webhook_to_bus_undecodable():
    ev_type, _ = hub._webhook_to_bus({"taskId": "not-an-int"})
    assert ev_type is None


# --- dispatch routing -----------------------------------------------------
def test_dispatch_webhook_event_routes(monkeypatch):
    seen = {}

    async def fake_dispatch(tenant, agent_id, ev_type, data):
        seen.update(tenant=tenant, agent_id=agent_id, ev_type=ev_type, data=data)

    hub._S["config"] = _cfg()
    monkeypatch.setattr(hub, "_dispatch", fake_dispatch)
    asyncio.run(hub.dispatch_webhook_event("personale", "personalChef-local", _evt()))
    assert seen["agent_id"] == "personalChef-local"
    assert seen["ev_type"] == "new_message"
    assert seen["data"]["needs_human"] is True


def test_dispatch_webhook_event_skips_undecodable(monkeypatch):
    called = []
    monkeypatch.setattr(hub, "_dispatch", lambda *a: called.append(a))
    hub._S["config"] = _cfg()
    asyncio.run(hub.dispatch_webhook_event("personale", "x", {"taskId": "nope"}))
    assert called == []


# --- registration ---------------------------------------------------------
def test_register_push_webhooks(monkeypatch):
    registered = []

    class FakeA2A:
        async def set_push_config(self, target, url, *, token=None, config_id=None, task_id=None):
            registered.append({"target": target, "url": url, "config_id": config_id})
            return {}

    hub._S["config"] = _cfg(a2a_webhook_url="http://host:8018", a2a_webhook_token="tk")
    monkeypatch.setattr(hub, "_a2a_client", lambda tenant, agent: FakeA2A())
    n = asyncio.run(hub.register_push_webhooks())
    assert n == 2  # personalChef-local + owner-telegram
    urls = {r["url"] for r in registered}
    assert "http://host:8018/a2a/webhook/personale/personalChef-local" in urls
    assert "http://host:8018/a2a/webhook/personale/owner-telegram" in urls


def test_register_push_webhooks_disabled_when_no_url():
    hub._S["config"] = _cfg()  # no a2a_webhook_url
    assert asyncio.run(hub.register_push_webhooks()) == 0


def test_unregister_push_webhooks_drops_the_same_configs(monkeypatch):
    """Standing down must hand the push path back.

    Network delivers a push config ON TOP of the SSE fan-out, so a config left
    behind by a peer that stopped polling gives it a second copy of every
    escalation on the shared telegram identity — and makes the bus consider
    that agent reachable, so it stops warning about undelivered messages.
    """
    removed = []

    class FakeA2A:
        async def delete_push_config(self, target, config_id):
            removed.append((target, config_id))
            return {}

    hub._S["config"] = _cfg(a2a_webhook_url="http://host:8018")
    monkeypatch.setattr(hub, "_a2a_client", lambda tenant, agent: FakeA2A())
    n = asyncio.run(hub.unregister_push_webhooks())
    assert n == 2
    assert removed == [
        ("personalChef-local", "coordinator-personalChef-local"),
        ("owner-telegram", "coordinator-owner-telegram"),
    ]


def test_unregister_push_webhooks_is_quiet_when_nothing_is_registered(monkeypatch, caplog):
    """Nothing to drop is the ordinary case (every boot, every release)."""
    import a2a_client

    class FakeA2A:
        async def delete_push_config(self, target, config_id):
            raise a2a_client.A2AError(-32003, f"Config {config_id} not found")

    hub._S["config"] = _cfg(a2a_webhook_url="http://host:8018")
    monkeypatch.setattr(hub, "_a2a_client", lambda tenant, agent: FakeA2A())
    with caplog.at_level("ERROR"):
        assert asyncio.run(hub.unregister_push_webhooks()) == 0
    assert caplog.records == []


def test_unregister_push_webhooks_survives_a_bus_failure(monkeypatch):
    """A peer that cannot reach the bus must still stand down."""
    class FakeA2A:
        async def delete_push_config(self, target, config_id):
            raise RuntimeError("bus down")

    hub._S["config"] = _cfg(a2a_webhook_url="http://host:8018")
    monkeypatch.setattr(hub, "_a2a_client", lambda tenant, agent: FakeA2A())
    assert asyncio.run(hub.unregister_push_webhooks()) == 0


# --- token --------------------------------------------------------------
def test_token_ok():
    assert a2a_webhook._token_ok("", None) is True       # no token configured
    assert a2a_webhook._token_ok("s3k", "s3k") is True
    assert a2a_webhook._token_ok("s3k", "wrong") is False
    assert a2a_webhook._token_ok("s3k", None) is False
