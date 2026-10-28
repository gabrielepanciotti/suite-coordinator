# tests/test_deployer.py
import asyncio

import deployer
import deployer_config as dc
import deployer_ops as ops


def _run(coro):
    return asyncio.run(coro)


def _cfg():
    return dc.DeployerConfig(
        hub_url="http://h:8031", tenant="acme", api_key="k",
        agent_id="deployer-test",
        allowed_senders=frozenset({"owner-telegram"}),
        env_keys=frozenset({"HUB_TURN_CAP"}),
        compose_file="docker-compose.prod.yml", project_dir=".",
        worker_service="coordinator-worker", env_file=".env", branch="master",
    )


class _FakeClient:
    """Stand-in for hub_client.HubClient: canned conv + records posts."""
    def __init__(self, conv):
        self._conv = conv
        self.posts = []
        self.fetch_count = 0

    async def get_conversation(self, conv_id):
        self.fetch_count += 1
        return self._conv

    async def send_message(self, conv_id, body, needs_human=False, context=None):
        self.posts.append((conv_id, body, context))
        return {"id": 999}


def _conv(msg_id, sender, ctx):
    return {
        "id": 7, "status": "open",
        "messages": [{"id": msg_id, "from_agent": sender, "context": ctx}],
    }


def test_ignores_unlisted_sender_without_fetch():
    deployer._processed.clear()
    client = _FakeClient(_conv(5, "intruder", {"deployer": {"verb": "deploy"}}))
    ev = {"conversation_id": 7, "message_id": 5, "from_agent": "intruder"}
    _run(deployer.handle_event(_cfg(), client, "new_message", ev))
    assert client.posts == []  # nothing executed, nothing posted
    assert client.fetch_count == 0  # cheap sender gate blocked the fetch


def test_executes_allowed_command_and_posts_outcome(monkeypatch):
    deployer._processed.clear()
    captured = {}
    async def fake_execute(cfg, verb, args):
        captured["verb"] = verb
        return ops.OpResult(True, "set HUB_TURN_CAP")
    monkeypatch.setattr(deployer, "_execute", fake_execute)
    ctx = {"deployer": {"verb": "set-env", "args": {"key": "HUB_TURN_CAP", "value": "20"}}}
    client = _FakeClient(_conv(5, "owner-telegram", ctx))
    ev = {"conversation_id": 7, "message_id": 5, "from_agent": "owner-telegram"}
    _run(deployer.handle_event(_cfg(), client, "new_message", ev))
    assert captured["verb"] == "set-env"
    assert len(client.posts) == 1
    assert "set HUB_TURN_CAP" in client.posts[0][1]


def test_dedup_skips_already_processed_message(monkeypatch):
    deployer._processed.clear()
    calls = []
    async def fake_execute(cfg, verb, args):
        calls.append(verb)
        return ops.OpResult(True, "ok")
    monkeypatch.setattr(deployer, "_execute", fake_execute)
    ctx = {"deployer": {"verb": "pull", "args": {}}}
    client = _FakeClient(_conv(5, "owner-telegram", ctx))
    ev = {"conversation_id": 7, "message_id": 5, "from_agent": "owner-telegram"}
    _run(deployer.handle_event(_cfg(), client, "new_message", ev))
    _run(deployer.handle_event(_cfg(), client, "new_message", ev))  # redelivery
    assert calls == ["pull"]  # executed once


def test_unknown_verb_posts_rejection(monkeypatch):
    deployer._processed.clear()
    ctx = {"deployer": {"verb": "rm-rf", "args": {}}}
    client = _FakeClient(_conv(5, "owner-telegram", ctx))
    ev = {"conversation_id": 7, "message_id": 5, "from_agent": "owner-telegram"}
    _run(deployer.handle_event(_cfg(), client, "new_message", ev))
    assert len(client.posts) == 1
    assert "unknown verb" in client.posts[0][1].lower()


def test_non_deployer_message_is_noop():
    deployer._processed.clear()
    client = _FakeClient(_conv(5, "owner-telegram", {"other": 1}))
    ev = {"conversation_id": 7, "message_id": 5, "from_agent": "owner-telegram"}
    _run(deployer.handle_event(_cfg(), client, "new_message", ev))
    assert client.posts == []


def test_execute_routes_rollback_verb_with_to_sha(monkeypatch):
    seen = {}

    async def fake_op_rollback(cfg, to_sha):
        seen["to_sha"] = to_sha
        return ops.OpResult(True, "ok")

    monkeypatch.setattr(deployer.deployer_ops, "op_rollback", fake_op_rollback)
    res = _run(deployer._execute(_cfg(), "rollback", {"to_sha": "abc1234"}))
    assert res.ok is True
    assert seen["to_sha"] == "abc1234"


def test_execute_routes_verify_verb(monkeypatch):
    called = {}

    async def fake_op_verify(cfg):
        called["yes"] = True
        return ops.OpResult(True, "worker coordinator-worker running")

    monkeypatch.setattr(deployer.deployer_ops, "op_verify", fake_op_verify)
    res = _run(deployer._execute(_cfg(), "verify", {}))
    assert called.get("yes") is True
    assert res.ok is True


def test_handle_event_echoes_pipeline_verdict_when_correlated(monkeypatch):
    deployer._processed.clear()

    async def fake_execute(cfg, verb, args):
        return ops.OpResult(True, "deploy: pulled origin/master; recreated worker")
    monkeypatch.setattr(deployer, "_execute", fake_execute)

    async def fake_head_sha(cfg):
        return "abc1234"
    monkeypatch.setattr(deployer.deployer_ops, "head_sha", fake_head_sha)

    ctx = {
        "deployer": {"verb": "deploy", "args": {}},
        "pipeline": {"id": 42, "stage": "deploy", "type": "deploy", "task": "t", "args": {}},
    }
    client = _FakeClient(_conv(5, "owner-telegram", ctx))
    ev = {"conversation_id": 7, "message_id": 5, "from_agent": "owner-telegram"}
    _run(deployer.handle_event(_cfg(), client, "new_message", ev))
    assert len(client.posts) == 1
    posted_context = client.posts[0][2]
    assert posted_context == {"pipeline_verdict": {
        "id": 42, "stage": "deploy", "ok": True,
        "summary": "deploy: pulled origin/master; recreated worker", "sha": "abc1234",
    }}


def test_handle_event_posts_text_only_when_no_correlation(monkeypatch):
    deployer._processed.clear()

    async def fake_execute(cfg, verb, args):
        return ops.OpResult(True, "set HUB_TURN_CAP")
    monkeypatch.setattr(deployer, "_execute", fake_execute)
    ctx = {"deployer": {"verb": "set-env", "args": {"key": "HUB_TURN_CAP", "value": "20"}}}
    client = _FakeClient(_conv(5, "owner-telegram", ctx))
    ev = {"conversation_id": 7, "message_id": 5, "from_agent": "owner-telegram"}
    _run(deployer.handle_event(_cfg(), client, "new_message", ev))
    assert len(client.posts) == 1
    assert client.posts[0][2] is None


def test_listen_declares_the_owner_before_streaming(monkeypatch):
    # #37: the deployer must say who holds its identity, like hub._listen does.
    calls = []

    async def fake_declare(url, api_key, agent_id, agent_key=""):
        calls.append(("declare", agent_id))
        return True

    def fake_stream(*a, **k):
        calls.append(("stream",))
        raise asyncio.CancelledError

    monkeypatch.setattr(deployer.hub_client, "dichiara_proprietario", fake_declare)
    monkeypatch.setattr(deployer.hub_client, "stream_events", fake_stream)
    try:
        _run(deployer._listen(_cfg()))
    except asyncio.CancelledError:
        pass
    assert calls == [("declare", "deployer-test"), ("stream",)]
