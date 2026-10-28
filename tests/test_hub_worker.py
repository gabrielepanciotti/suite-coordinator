"""Worker-role hub.start opens SSE only for local_agents, never owner-telegram."""

import asyncio

import pytest
import hub
import hub_config


def _cfg(role):
    return hub_config.HubConfig(
        hub_url="http://h:8031",
        api_keys={"acme": "k"},
        telegram_agent="owner-telegram",
        telegram_chat_id=0,
        local_agents={"offerte-prod": "offerte", "db-prod": "db"},
        role=role,
    )


def test_worker_listens_only_local_agents(monkeypatch):
    started = []

    async def fake_listen(tenant, agent_id):
        started.append((tenant, agent_id))

    monkeypatch.setattr(hub, "_listen", fake_listen)

    async def go():
        hub._S["local_tasks"] = []
        hub._S["owner_tasks"] = []
        hub._S["clients"] = {}
        hub.start(bot=None, run_claude=None, send_to=None, config=_cfg("worker"))
        await asyncio.sleep(0)  # let the created tasks run the fake

    asyncio.run(go())

    agents = {a for _, a in started}
    assert "owner-telegram" not in agents
    assert agents == {"offerte-prod", "db-prod"}


def test_multi_tenant_listens_agents_only_on_their_tenant(monkeypatch):
    started = []

    async def fake_listen(tenant, agent_id):
        started.append((tenant, agent_id))

    monkeypatch.setattr(hub, "_listen", fake_listen)

    cfg = hub_config.HubConfig(
        hub_url="http://h:8031",
        api_keys={"acme": "k1", "personale": "k2"},
        telegram_agent="owner-telegram",
        telegram_chat_id=-100,
        local_agents={"offerte-dev-linux": "offerte", "personalChef-local": "personalChef"},
        agent_tenants={"offerte-dev-linux": "acme", "personalChef-local": "personale"},
        role="hub",
    )

    async def go():
        hub._S["local_tasks"] = []
        hub._S["owner_tasks"] = []
        hub._S["clients"] = {}
        hub.start(bot=object(), run_claude=None, send_to=None, config=cfg)
        await asyncio.sleep(0)

    asyncio.run(go())

    assert ("acme", "offerte-dev-linux") in started
    assert ("personale", "personalChef-local") in started
    # never the wrong tenant
    assert ("personale", "offerte-dev-linux") not in started
    assert ("acme", "personalChef-local") not in started
    # human face on both tenants
    assert ("acme", "owner-telegram") in started
    assert ("personale", "owner-telegram") in started


def test_hub_role_listens_telegram(monkeypatch):
    started = []

    async def fake_listen(tenant, agent_id):
        started.append((tenant, agent_id))

    monkeypatch.setattr(hub, "_listen", fake_listen)

    async def go():
        hub._S["local_tasks"] = []
        hub._S["owner_tasks"] = []
        hub._S["clients"] = {}
        cfg = _cfg("hub")
        cfg = hub_config.HubConfig(**{**cfg.__dict__, "telegram_chat_id": -100})
        hub.start(bot=object(), run_claude=None, send_to=None, config=cfg)
        await asyncio.sleep(0)

    asyncio.run(go())

    assert ("acme", "owner-telegram") in started


def test_dispatch_to_agent_creates_conversation(monkeypatch):
    sent = {}

    class FakeClient:
        async def create_conversation(
            self, to, subject, body, priority="normal", context=None
        ):
            sent.update(to=to, subject=subject, body=body)
            return {"id": 99}

    cfg = _cfg("hub")
    cfg = hub_config.HubConfig(**{**cfg.__dict__, "telegram_chat_id": -100})
    hub._S["config"] = cfg
    monkeypatch.setattr(hub, "_client", lambda tenant, agent: FakeClient())

    async def go():
        return await hub.dispatch_to_agent(
            "acme", "offerte-prod", "allinea e deploya"
        )

    conv = asyncio.run(go())

    assert conv["id"] == 99
    assert sent["to"] == ["offerte-prod"]
    assert "allinea e deploya" in sent["body"]


# ---------------------------------------------------------------------------
# cmd_dispatch tests
# ---------------------------------------------------------------------------


class _FakeMsg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self):
        self.effective_message = _FakeMsg()


class _FakeCtx:
    def __init__(self, args):
        self.args = args


def _enabled_hub_cfg():
    cfg = _cfg("hub")
    return hub_config.HubConfig(**{**cfg.__dict__, "telegram_chat_id": -100})


def test_cmd_dispatch_usage_when_too_few_args():
    hub._S["config"] = _enabled_hub_cfg()
    upd, ctx = _FakeUpdate(), _FakeCtx(["offerte-prod"])  # only 1 arg
    asyncio.run(hub.cmd_dispatch(upd, ctx))
    assert any("Uso:" in r for r in upd.effective_message.replies)


def test_cmd_dispatch_not_configured():
    hub._S["config"] = None
    upd, ctx = _FakeUpdate(), _FakeCtx(["offerte-prod", "task"])
    asyncio.run(hub.cmd_dispatch(upd, ctx))
    assert any("non configurat" in r.lower() for r in upd.effective_message.replies)


def test_cmd_dispatch_happy_path(monkeypatch):
    hub._S["config"] = _enabled_hub_cfg()

    class FakeClient:
        async def create_conversation(
            self, to, subject, body, priority="normal", context=None
        ):
            return {"id": 42}

    monkeypatch.setattr(hub, "_client", lambda tenant, agent: FakeClient())
    upd, ctx = _FakeUpdate(), _FakeCtx(["offerte-prod", "allinea", "e", "deploya"])
    asyncio.run(hub.cmd_dispatch(upd, ctx))
    assert any("conv #42" in r for r in upd.effective_message.replies)


def test_prefix_dispatch_resolves_prod_agent(monkeypatch):
    sent = {}

    class FakeClient:
        async def create_conversation(
            self, to, subject, body, priority="normal", context=None
        ):
            sent.update(to=to, body=body)
            return {"id": 7}

    cfg = _cfg("hub")
    cfg = hub_config.HubConfig(
        **{
            **cfg.__dict__,
            "telegram_chat_id": -100,
            "prod_dispatch": {"offerte": "offerte-prod"},
        }
    )
    hub._S["config"] = cfg
    monkeypatch.setattr(hub, "_client", lambda tenant, agent: FakeClient())

    async def go():
        return await hub.maybe_dispatch_prefix("offerte", "prod: allinea e deploya")

    handled, conv = asyncio.run(go())

    assert handled is True
    assert sent["to"] == ["offerte-prod"]
    assert sent["body"] == "allinea e deploya"


def test_prefix_dispatch_ignores_plain_text():
    async def go():
        return await hub.maybe_dispatch_prefix("offerte", "ciao come stai")

    handled, conv = asyncio.run(go())
    assert handled is False and conv is None


def test_prefix_dispatch_unmapped_project(monkeypatch):
    cfg = _cfg("hub")
    cfg = hub_config.HubConfig(
        **{**cfg.__dict__, "telegram_chat_id": -100, "prod_dispatch": {}}
    )
    hub._S["config"] = cfg

    async def go():
        return await hub.maybe_dispatch_prefix("offerte", "prod: do something")

    handled, conv = asyncio.run(go())
    assert handled is False and conv is None


# ---------------------------------------------------------------------------
# A2A routing (cfg.use_a2a) — outbound sends go through the A2A client
# ---------------------------------------------------------------------------


def _a2a_cfg():
    cfg = _cfg("hub")
    return hub_config.HubConfig(
        **{**cfg.__dict__, "telegram_chat_id": -100, "use_a2a": True}
    )


def test_dispatch_to_agent_uses_a2a_when_enabled(monkeypatch):
    sent = {}

    class FakeA2A:
        async def send_message(self, target, text, *, task_id=None, data=None,
                               subject=None, priority=None):
            sent.update(target=target, text=text, task_id=task_id)
            return {"id": "77", "kind": "task"}

    hub._S["config"] = _a2a_cfg()
    monkeypatch.setattr(hub, "_a2a_client", lambda tenant, agent: FakeA2A())
    # REST client must NOT be used in A2A mode.
    monkeypatch.setattr(hub, "_client", lambda *a: 1 / 0)

    async def go():
        return await hub.dispatch_to_agent("acme", "offerte-prod", "deploya")

    conv = asyncio.run(go())
    assert conv["id"] == 77  # normalised str -> int
    assert sent == {"target": "offerte-prod", "text": "deploya", "task_id": None}


def test_handle_topic_reply_appends_via_a2a(monkeypatch):
    import hub_store

    sent = {}

    class FakeA2A:
        async def send_message(self, target, text, *, task_id=None, **kw):
            sent.update(target=target, text=text, task_id=task_id)
            return {"id": str(task_id), "kind": "task"}

    hub._S["config"] = _a2a_cfg()
    monkeypatch.setattr(hub_store, "conv_id_for_topic", lambda c, t: 5)
    monkeypatch.setattr(
        hub_store, "get_conv",
        lambda cid: {"tenant": "acme", "counterpart": "offerte-prod"},
    )
    monkeypatch.setattr(hub, "_a2a_client", lambda tenant, agent: FakeA2A())
    monkeypatch.setattr(hub, "_client", lambda *a: 1 / 0)  # REST must not be used

    async def go():
        return await hub.handle_topic_reply(-100, 9, "ok procedi")

    assert asyncio.run(go()) is True
    assert sent == {"target": "offerte-prod", "text": "ok procedi", "task_id": 5}


def test_handle_topic_reply_falls_back_to_rest_without_counterpart(monkeypatch):
    import hub_store

    sent = {}

    class FakeRest:
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            sent.update(conv_id=conv_id, body=body)
            return {"id": conv_id}

    hub._S["config"] = _a2a_cfg()  # A2A on, but counterpart unknown
    monkeypatch.setattr(hub_store, "conv_id_for_topic", lambda c, t: 5)
    monkeypatch.setattr(hub_store, "get_conv", lambda cid: {"tenant": "acme"})
    monkeypatch.setattr(hub, "_client", lambda tenant, agent: FakeRest())
    monkeypatch.setattr(hub, "_a2a_client", lambda *a: 1 / 0)  # A2A must not be used

    async def go():
        return await hub.handle_topic_reply(-100, 9, "ciao")

    assert asyncio.run(go()) is True
    assert sent == {"conv_id": 5, "body": "ciao"}


# ---------------------------------------------------------------------------
# _wake_local_agent: is_watched / is_claimed guard tests (issue #26)
# ---------------------------------------------------------------------------


def test_wake_local_agent_skips_when_watched(monkeypatch):
    import hub_store
    import projects

    called = []

    async def fake_run_claude(*a, **k):
        called.append(a)
        return ("out", "sess-1")

    async def fake_verify(*a, **k):
        return True

    async def fake_surface(*a, **k):
        return None

    hub._S["config"] = _cfg("worker")
    hub._S["run_claude"] = fake_run_claude
    monkeypatch.setattr(projects, "resolve_project",
                        lambda key: type("P", (), {"key": "cosmos", "workspace": "/ws"})())
    monkeypatch.setattr(hub_store, "is_watched", lambda tenant, agent: True)
    monkeypatch.setattr(hub_store, "is_claimed", lambda tenant, agent: True)
    # Stub the whole post-guard spawn path too: if the is_watched guard were
    # removed, control would reach run_claude (growing `called`) and the test
    # would fail cleanly on `called == []` on ANY filesystem -- instead of
    # masking the regression behind a PermissionError (hub_store disk write)
    # or a real network call (_verify_outcome) further down the path.
    monkeypatch.setattr(hub_store, "get_conv", lambda c: {})
    monkeypatch.setattr(hub_store, "incr_turn", lambda c: 1)
    monkeypatch.setattr(hub_store, "set_session", lambda c, s, **kw: None)
    monkeypatch.setattr(hub, "_verify_outcome", fake_verify)
    monkeypatch.setattr(hub, "_maybe_surface_escalation", fake_surface)

    data = {"conversation_id": 1, "subject": "s", "from_agent": "x",
            "body": "b", "message_id": 1}
    asyncio.run(hub._wake_local_agent("acme", "offerte-prod", "new_message", data))

    assert called == []  # a live watcher is listening -> no headless spawn


def test_wake_local_agent_spawns_when_claimed_but_not_watched(monkeypatch):
    """Ghost claim (issue #26): an interactive session looks present (the
    hub-auto-claim.sh hook keeps the claim file fresh) but no real hub_watch.py
    is listening. Previously this silently dropped the wake; now the spawn must
    go through, with the caution paragraph injected into the prompt so the
    woken worker does not confidently close a conversation it lacks context
    for."""
    import hub_store
    import projects

    called = []

    async def fake_run_claude(prompt, **k):
        called.append(prompt)
        return ("out", "sess-1")

    async def fake_verify(*a, **k):
        return True

    async def fake_surface(*a, **k):
        return None

    hub._S["config"] = _cfg("worker")
    hub._S["run_claude"] = fake_run_claude
    monkeypatch.setattr(projects, "resolve_project",
                        lambda key: type("P", (), {"key": "cosmos", "workspace": "/ws"})())
    monkeypatch.setattr(hub_store, "is_watched", lambda tenant, agent: False)
    monkeypatch.setattr(hub_store, "is_claimed", lambda tenant, agent: True)
    monkeypatch.setattr(hub_store, "get_conv", lambda c: {})
    monkeypatch.setattr(hub_store, "incr_turn", lambda c: 1)
    monkeypatch.setattr(hub_store, "set_session", lambda c, s, **kw: None)
    monkeypatch.setattr(hub, "_verify_outcome", fake_verify)
    monkeypatch.setattr(hub, "_maybe_surface_escalation", fake_surface)

    data = {"conversation_id": 1, "subject": "s", "from_agent": "x",
            "body": "b", "message_id": 1}
    asyncio.run(hub._wake_local_agent("acme", "offerte-prod", "new_message", data))

    assert len(called) == 1  # ghost claim -> spawn happens (new behavior)
    assert hub._WAKE_CAUTION in called[0]


def test_wake_local_agent_spawns_when_unclaimed(monkeypatch):
    import hub_store
    import projects

    called = []

    async def fake_run_claude(prompt, **k):
        called.append(prompt)
        return ("out", "sess-1")

    async def fake_verify(*a, **k):
        return True

    async def fake_surface(*a, **k):
        return None

    hub._S["config"] = _cfg("worker")
    hub._S["run_claude"] = fake_run_claude
    monkeypatch.setattr(projects, "resolve_project",
                        lambda key: type("P", (), {"key": "cosmos", "workspace": "/ws"})())
    monkeypatch.setattr(hub_store, "is_watched", lambda tenant, agent: False)
    monkeypatch.setattr(hub_store, "is_claimed", lambda tenant, agent: False)
    monkeypatch.setattr(hub_store, "get_conv", lambda c: {})
    monkeypatch.setattr(hub_store, "incr_turn", lambda c: 1)
    monkeypatch.setattr(hub_store, "set_session", lambda c, s, **kw: None)
    monkeypatch.setattr(hub, "_verify_outcome", fake_verify)
    monkeypatch.setattr(hub, "_maybe_surface_escalation", fake_surface)

    data = {"conversation_id": 1, "subject": "s", "from_agent": "x",
            "body": "b", "message_id": 1}
    asyncio.run(hub._wake_local_agent("acme", "offerte-prod", "new_message", data))

    assert len(called) == 1  # unclaimed and unwatched -> exactly one headless spawn
    assert hub._WAKE_CAUTION not in called[0]  # no ghost -> no caution text


def test_wake_local_agent_skips_second_concurrent_spawn(monkeypatch):
    """Issue #38: the same event can reach _wake_local_agent twice (SSE
    redelivery, the A2A push webhook, ...) before the first headless run has
    finished. Without a guard, two claude sessions spawn concurrently on the
    same workspace -- neither aware of the other -- and whichever finishes
    (or fails) first can escalate a conversation the other is still
    genuinely working on. The second concurrent call must skip the spawn
    entirely rather than race it."""
    import hub_store
    import projects

    called = []
    first_may_finish = asyncio.Event()
    second_entered = asyncio.Event()

    async def fake_run_claude(prompt, **k):
        called.append(prompt)
        second_entered.set()
        await first_may_finish.wait()
        return ("out", "sess-1")

    async def fake_verify(*a, **k):
        return True

    async def fake_surface(*a, **k):
        return None

    hub._S["config"] = _cfg("worker")
    hub._S["run_claude"] = fake_run_claude
    monkeypatch.setattr(projects, "resolve_project",
                        lambda key: type("P", (), {"key": "cosmos", "workspace": "/ws"})())
    monkeypatch.setattr(hub_store, "is_watched", lambda tenant, agent: False)
    monkeypatch.setattr(hub_store, "is_claimed", lambda tenant, agent: False)
    monkeypatch.setattr(hub_store, "get_conv", lambda c: {})
    monkeypatch.setattr(hub_store, "incr_turn", lambda c: 1)
    monkeypatch.setattr(hub_store, "set_session", lambda c, s, **kw: None)
    monkeypatch.setattr(hub, "_verify_outcome", fake_verify)
    monkeypatch.setattr(hub, "_maybe_surface_escalation", fake_surface)

    data = {"conversation_id": 1, "subject": "s", "from_agent": "x",
            "body": "b", "message_id": 1}

    async def go():
        first = asyncio.create_task(
            hub._wake_local_agent("acme", "offerte-prod", "new_message", data)
        )
        await second_entered.wait()  # first is now inside run_claude
        await hub._wake_local_agent("acme", "offerte-prod", "new_message", data)
        first_may_finish.set()
        await first

    asyncio.run(go())

    assert len(called) == 1  # the concurrent second call never spawned
    assert hub._S["inflight_wakes"] == {}  # the slot is released afterwards


@pytest.fixture(autouse=True)
def _no_human_replied_write(monkeypatch):
    """These tests stub hub_store reads without a store path: the #66 marker
    write would hit the container default (/app). Its behaviour is covered in
    test_hub_forward_replies.py."""
    import hub_store

    monkeypatch.setattr(hub_store, "mark_human_replied", lambda cid: None)
