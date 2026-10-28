"""#61: a message whose context declares no_reply_needed wakes nobody."""
import asyncio

import hub


class _Client:
    conv: dict = {}
    fail = False
    calls = 0

    def __init__(self, *a):
        pass

    async def get_conversation(self, conv_id):
        _Client.calls += 1
        if _Client.fail:
            raise RuntimeError("bus down")
        return _Client.conv


def _setup(monkeypatch, conv, fail=False):
    _Client.conv, _Client.fail, _Client.calls = conv, fail, 0
    monkeypatch.setattr(hub.hub_client, "HubClient", _Client)
    monkeypatch.setitem(hub._S, "clients", {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"acme": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"a-dev": "a"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)


def _check(data):
    return asyncio.run(hub._no_reply_needed("acme", "a-dev", 290, data))


def test_reads_the_flag_from_the_bus_message(monkeypatch):
    _setup(monkeypatch, {"messages": [
        {"id": 29208, "context": None},
        {"id": 29209, "context": {"no_reply_needed": True}},
    ]})
    assert _check({"conversation_id": 290, "message_id": 29209}) is True


def test_other_messages_flag_does_not_count(monkeypatch):
    _setup(monkeypatch, {"messages": [
        {"id": 29208, "context": {"no_reply_needed": True}},
        {"id": 29209, "context": {"escalation": {"blocked": True}}},
    ]})
    assert _check({"conversation_id": 290, "message_id": 29209}) is False


def test_context_in_the_event_spares_the_fetch(monkeypatch):
    _setup(monkeypatch, {"messages": []})
    data = {"conversation_id": 290, "message_id": 1, "context": {"no_reply_needed": True}}
    assert _check(data) is True
    assert _Client.calls == 0


def test_only_a_literal_true_counts(monkeypatch):
    _setup(monkeypatch, {"messages": [{"id": 1, "context": {"no_reply_needed": "yes"}}]})
    assert _check({"conversation_id": 290, "message_id": 1}) is False


def test_fails_open(monkeypatch):
    _setup(monkeypatch, {}, fail=True)
    assert _check({"conversation_id": 290, "message_id": 1}) is False   # bus down
    _setup(monkeypatch, {"messages": []})
    assert _check({"conversation_id": 290, "message_id": 1}) is False   # not found
    assert _check({"conversation_id": 290}) is False                    # no id


def test_wake_skips_the_spawn(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "nr.json"))
    _setup(monkeypatch, {"messages": [{"id": 7, "context": {"no_reply_needed": True}}]})
    monkeypatch.setattr(hub.projects, "resolve_project", lambda k: object())
    monkeypatch.setattr(hub.hub_store, "is_watched", lambda *a: False)
    spawned = []

    async def _run_local_wake(*a, **k):
        spawned.append(a)

    monkeypatch.setattr(hub, "_run_local_wake", _run_local_wake)
    data = {"conversation_id": 290, "message_id": 7, "from_agent": "b-dev", "body": "ok, chiuso"}
    asyncio.run(hub._wake_local_agent("acme", "a-dev", "new_message", data))
    assert spawned == []
