# tests/test_hub.py
import asyncio

import pytest

import hub


@pytest.fixture(autouse=True)
def _wake_without_no_reply_check(monkeypatch):
    """The wake tests here script the bus reads of the verify step; the
    no_reply_needed pre-check (#61) would consume one. It is tested on its own
    in test_hub_no_reply.py."""

    async def _never(*_a, **_k):
        return False

    monkeypatch.setattr(hub, "_no_reply_needed", _never)

    # Same for the context lookup of max_minutes (#70): event-only here.
    async def _ctx(_t, _a, _c, data):
        return data.get("context")

    monkeypatch.setattr(hub, "_message_context", _ctx)


async def _noop_sleep(*_a, **_k):
    return None


def _run(coro):
    return asyncio.run(coro)


def test_start_stores_deps(monkeypatch):
    monkeypatch.setitem(hub._S, "tasks", [])
    sent = []

    async def fake_run_claude(prompt, *, workspace, persona, resume_id=None, extra_env=None, on_session=None):
        return "ok", "sid"

    async def fake_send_to(uid, text):
        sent.append((uid, text))

    class FakeBot:
        pass

    cfg = hub.hub_config.HubConfig(
        hub_url="http://h:8030", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={"personalChef-local": "personalChef"}, turn_cap=12,
    )
    # start with listeners disabled (we only check wiring here)
    hub.start(bot=FakeBot(), run_claude=fake_run_claude, send_to=fake_send_to,
              config=cfg, spawn_listeners=False)
    assert hub._S["config"].hub_url == "http://h:8030"
    assert hub._S["run_claude"] is fake_run_claude
    assert hub._S["bot"] is not None


def test_human_new_message_creates_topic_and_posts(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    created = {}
    posted = []

    class FakeBot:
        async def create_forum_topic(self, chat_id, name):
            created.update(chat_id=chat_id, name=name)
            class T:  # noqa
                message_thread_id = 77
            return T()

        async def send_message(self, chat_id, text, message_thread_id=None, **kw):
            posted.append((chat_id, message_thread_id, text))
            return type("M", (), {"message_id": 900})()

    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={}, turn_cap=12,
    )
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "bot", FakeBot())

    data = {"conversation_id": 5, "from_agent": "personalChef-local",
            "subject": "Serve una scelta", "body": "Quale fornitore?",
            "needs_human": True, "priority": "high"}
    asyncio.run(hub._handle_human_event("personale", "new_message", data))

    assert created["chat_id"] == -100
    assert hub_store.conv_id_for_topic(-100, 77) == 5
    assert posted and posted[0][1] == 77 and "Quale fornitore" in posted[0][2]
    assert hub_store.get_conv(5)["card_msg_id"] == 900


def test_human_new_message_without_needs_human_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h2.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    posted = []
    monkeypatch.setitem(hub._S, "config", hub.hub_config.load_config())
    monkeypatch.setitem(hub._S, "send_thread", lambda *a: posted.append(a))
    data = {"conversation_id": 9, "needs_human": False, "body": "x"}
    asyncio.run(hub._handle_human_event("personale", "new_message", data))
    assert posted == []  # default-deny: only needs_human surfaces


def test_human_needs_human_empty_body_opens_no_topic(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h2b.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    created = []
    posted = []

    class FakeBot:
        async def create_forum_topic(self, chat_id, name):
            created.append((chat_id, name))
            raise AssertionError("must not create a topic for an empty-body ping")

    async def fake_send_thread(bot, chat_id, thread_id, text):
        posted.append((chat_id, thread_id, text))

    monkeypatch.setitem(hub._S, "config", hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={}, turn_cap=12))
    monkeypatch.setitem(hub._S, "bot", FakeBot())
    monkeypatch.setitem(hub._S, "send_thread", fake_send_thread)

    # needs_human but no message body and no existing topic -> informational
    # noise, must NOT spawn a topic.
    data = {"conversation_id": 9, "from_agent": "etl-local",
            "subject": "ping", "body": "   ", "needs_human": True}
    asyncio.run(hub._handle_human_event("personale", "new_message", data))
    assert created == [] and posted == []


def test_topic_reply_posts_to_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h3.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    hub_store.upsert_conv(5, tenant="personale", chat_id=-100, topic_id=77)

    sent = {}

    class FakeClient:
        def __init__(self, *a):
            pass
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            sent.update(conv_id=conv_id, body=body)
            return {"id": 1}

    monkeypatch.setattr(hub.hub_client, "HubClient", FakeClient)
    monkeypatch.setitem(hub._S, "config", hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={}, turn_cap=12))
    monkeypatch.setitem(hub._S, "clients", {})

    handled = asyncio.run(hub.handle_topic_reply(-100, 77, "usa il fornitore B"))
    assert handled is True
    assert sent == {"conv_id": 5, "body": "usa il fornitore B"}


def test_topic_reply_unknown_topic_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h4.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    monkeypatch.setitem(hub._S, "config", hub.hub_config.load_config())
    handled = asyncio.run(hub.handle_topic_reply(-100, 999, "ciao"))
    assert handled is False  # not a hub topic -> bot handles it normally


def test_wake_local_agent_runs_claude_with_workspace_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h5.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    calls = {}

    async def fake_run_claude(prompt, *, workspace, persona, resume_id=None, extra_env=None, on_session=None):
        calls.update(prompt=prompt, workspace=workspace, resume_id=resume_id)
        return "done", "sid-9"

    # resolve_project("personalChef") must return a real project from the registry
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    class _OkClient:
        def __init__(self, *a):
            pass
        async def get_conversation(self, conv_id):
            return {"status": "open",
                    "messages": [{"id": 999, "from_agent": "personalChef-local"}]}
    monkeypatch.setattr(hub.hub_client, "HubClient", _OkClient)
    monkeypatch.setitem(hub._S, "clients", {})

    data = {"conversation_id": 5, "from_agent": "mycorise-local",
            "subject": "s", "body": "riprocessa", "priority": "normal"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert calls["workspace"].endswith("personalChef")
    assert "riprocessa" in calls["prompt"]
    assert calls["resume_id"] is None              # first turn
    assert (
        hub_store.get_session(5, "personalChef-local", calls["workspace"]) == "sid-9"
    )
    # second turn resumes the saved session
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))
    assert calls["resume_id"] == "sid-9"


def test_wake_loop_guard_escalates(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h6.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    hub_store.upsert_conv(5, tenant="personale", chat_id=-100, topic_id=1)
    for _ in range(2):
        hub_store.incr_turn(5)

    ran = []

    async def fake_run_claude(*a, **k):
        ran.append(1)
        return "x", "sid"

    escalated = []

    class FakeClient:
        def __init__(self, *a):
            pass
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            escalated.append((conv_id, needs_human))
            return {"id": 1}

    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={"personalChef-local": "personalChef"}, turn_cap=2)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)
    monkeypatch.setattr(hub.hub_client, "HubClient", FakeClient)
    monkeypatch.setitem(hub._S, "clients", {})

    data = {"conversation_id": 5, "from_agent": "x", "body": "loop"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))
    assert ran == []                       # capped: did not run claude
    assert escalated == [(5, True)]        # escalated to human instead


def test_wake_skips_self_originated_event_without_spending_turn(tmp_path, monkeypatch):
    # An agent's own escalation repost (from_agent == agent_id) must fast-skip
    # BEFORE incr_turn: otherwise it burns a turn_cap slot and re-runs claude as
    # a no-op before idempotency stops it (#13).
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h_self.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    ran = []

    async def fake_run_claude(*a, **k):
        ran.append(1)
        return "x", "sid"

    turned = []
    real_incr = hub_store.incr_turn
    monkeypatch.setattr(hub_store, "incr_turn",
                        lambda cid: (turned.append(cid), real_incr(cid))[1])

    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)
    monkeypatch.setitem(hub._S, "clients", {})

    data = {"conversation_id": 5, "from_agent": "personalChef-local", "body": "self"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))
    assert ran == []           # did not re-run claude
    assert turned == []        # did not consume a turn_cap slot


def test_inbox_text_formats_items(monkeypatch):
    items = [{"id": 5, "subject": "Margini", "from_agent": "etl-local",
              "priority": "high", "unread_count": 2,
              "last_message_preview": "verifica i batch"}]
    txt = hub._inbox_text({"personale": items})
    assert "#5" in txt and "Margini" in txt and "personale" in txt


def test_inbox_text_empty():
    assert "nessuna" in hub._inbox_text({"personale": []}).lower()


def test_status_resolved_archives_topic(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h7.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    hub_store.upsert_conv(5, tenant="personale", chat_id=-100, topic_id=77)
    hub_store.set_conv_fields(5, card={"ask_from": "personalChef-local", "ask": "Quale?"},
                              card_msg_id=900)

    closed = []
    edited = []

    class FakeBot:
        async def close_forum_topic(self, chat_id, message_thread_id):
            closed.append((chat_id, message_thread_id))

        async def edit_message_text(self, text, chat_id, message_id, **kw):
            edited.append((chat_id, message_id, text))

    monkeypatch.setitem(hub._S, "config", hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={}, turn_cap=12))
    monkeypatch.setitem(hub._S, "bot", FakeBot())

    data = {"conversation_id": 5, "new_status": "resolved", "changed_by": "personalChef-local"}
    asyncio.run(hub._handle_status("personale", data))
    # Who closed it goes into the conversation's card (edited, no new
    # message), then the topic is archived.
    assert closed == [(-100, 77)]
    assert len(edited) == 1 and edited[0][:2] == (-100, 900)
    assert "chiusa da <b>personalChef-local</b>" in edited[0][2]
    assert "Quale?" in edited[0][2]


def test_status_open_change_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h8.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    posted = []
    monkeypatch.setitem(hub._S, "send_thread", lambda *a: posted.append(a))
    monkeypatch.setitem(hub._S, "config", hub.hub_config.load_config())
    asyncio.run(hub._handle_status("personale", {"conversation_id": 5, "new_status": "open"}))
    assert posted == []


def test_start_stop_listeners_idempotent(monkeypatch):
    async def scenario():
        async def fake_listen(tenant, agent_id):
            await asyncio.sleep(3600)

        monkeypatch.setattr(hub, "_listen", fake_listen)
        cfg = hub.hub_config.HubConfig(
            hub_url="http://h:8030", api_keys={"personale": "k"},
            telegram_agent="owner-telegram", telegram_chat_id=-100,
            local_agents={"personalChef-local": "personalChef"},
        )
        monkeypatch.setitem(hub._S, "config", cfg)
        monkeypatch.setitem(hub._S, "local_tasks", [])
        monkeypatch.setitem(hub._S, "owner_tasks", [])
        n = hub.start_listeners()
        assert n == 2  # telegram agent + 1 local agent
        assert hub.start_listeners() == 2  # idempotent: doesn't double up
        captured = hub._all_listener_tasks()
        await hub.stop_listeners()
        assert hub._all_listener_tasks() == []
        assert all(t.done() for t in captured)

    asyncio.run(scenario())


class _VerifyClient:
    """Fake HubClient: get_conversation returns a queued dict or raises."""
    _queue = []
    def __init__(self, *a):
        pass
    async def get_conversation(self, conv_id):
        item = _VerifyClient._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _set_verify_client(monkeypatch, queue):
    _VerifyClient._queue = list(queue)
    monkeypatch.setattr(hub.hub_client, "HubClient", _VerifyClient)
    monkeypatch.setitem(hub._S, "clients", {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="g", telegram_chat_id=-100,
        local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)


def test_verify_outcome_true_when_agent_posted_after_since(monkeypatch):
    _set_verify_client(monkeypatch, [
        {"status": "open", "messages": [
            {"id": 10, "from_agent": "other"},
            {"id": 11, "from_agent": "personalChef-local"}]}])
    ok = asyncio.run(hub._verify_outcome("personale", "personalChef-local", 5, since=10))
    assert ok is True


def test_verify_outcome_true_when_resolved(monkeypatch):
    _set_verify_client(monkeypatch, [{"status": "resolved", "messages": []}])
    ok = asyncio.run(hub._verify_outcome("personale", "personalChef-local", 5, since=0))
    assert ok is True


def test_verify_outcome_false_when_no_agent_message(monkeypatch):
    _set_verify_client(monkeypatch, [
        {"status": "open", "messages": [{"id": 11, "from_agent": "other"}]}])
    ok = asyncio.run(hub._verify_outcome("personale", "personalChef-local", 5, since=10))
    assert ok is False


def test_verify_outcome_retries_once_then_raises(monkeypatch):
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    _set_verify_client(monkeypatch, [RuntimeError("net"), RuntimeError("net")])
    import pytest
    with pytest.raises(hub.OutcomeUnknown):
        asyncio.run(hub._verify_outcome("personale", "personalChef-local", 5, since=0))


def test_verify_outcome_recovers_on_second_fetch(monkeypatch):
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    _set_verify_client(monkeypatch, [
        RuntimeError("net"),
        {"status": "open", "messages": [{"id": 12, "from_agent": "personalChef-local"}]}])
    ok = asyncio.run(hub._verify_outcome("personale", "personalChef-local", 5, since=0))
    assert ok is True


def test_wake_retries_fresh_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "wr.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    # A prior session exists for THIS agent. workspace="" means "birthplace
    # unknown", which is not a mismatch — the workspace guard is exercised on
    # its own in test_wake_ignores_session_born_in_another_workspace.
    hub_store.set_session(5, "sid-prev", agent_id="personalChef-local", workspace="")

    resumes = []
    async def fake_run_claude(prompt, *, workspace, persona, resume_id=None, extra_env=None, on_session=None):
        resumes.append(resume_id)
        return "x", "sid-A"

    # attempt 1 verify -> no agent msg; attempt 2 verify -> agent posted
    _set_verify_client(monkeypatch, [
        {"status": "open", "messages": [{"id": 1, "from_agent": "other"}]},
        {"status": "open", "messages": [{"id": 2, "from_agent": "personalChef-local"}]}])
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "message_id": 1, "from_agent": "other", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    # attempt 1 resumes the saved session; attempt 2 is a clean restart (no resume)
    assert resumes == ["sid-prev", None]
    assert not (hub_store.get_conv(5) or {}).get("wake_escalated")


def test_wake_ignores_session_born_in_another_workspace(tmp_path, monkeypatch):
    """A session created in another node's workspace is never resumed (#30).

    That is the shape that blocked: the session keeps the project it was born
    with, so the woken node's own folder is *external* to it and the headless
    run hangs on a permission question. `--dir` pins the process, not the
    session — the only way out is a fresh one.
    """
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "ws.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    hub_store.set_session(
        5, "sid-elsewhere", agent_id="personalChef-local", workspace="/ws/another-node"
    )

    seen = []
    async def fake_run_claude(prompt, *, workspace, persona, resume_id=None, extra_env=None, on_session=None):
        seen.append((workspace, resume_id))
        return "x", "sid-fresh"

    _set_verify_client(monkeypatch, [
        {"status": "open", "messages": [{"id": 2, "from_agent": "personalChef-local"}]}])
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "message_id": 1, "from_agent": "other", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert len(seen) == 1
    run_workspace, resume = seen[0]
    assert run_workspace != "/ws/another-node"   # the seeded birthplace is foreign
    assert resume is None                        # ...so the run starts clean
    # ...and the fresh session is filed under this agent AND this workspace.
    assert hub_store.get_session(5, "personalChef-local", run_workspace) == "sid-fresh"


def test_wake_escalates_after_two_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "we.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    async def fake_run_claude(*a, **k):
        return "x", "sid-A"

    escalated = []
    class _FailClient:
        def __init__(self, *a):
            pass
        async def get_conversation(self, conv_id):
            return {"status": "open", "messages": [{"id": 1, "from_agent": "other"}]}
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            escalated.append((conv_id, needs_human))
            return {"id": 1}
    monkeypatch.setattr(hub.hub_client, "HubClient", _FailClient)
    monkeypatch.setitem(hub._S, "clients", {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "message_id": 1, "from_agent": "other", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert escalated == [(5, True)]                       # one needs_human posted
    assert hub_store.get_conv(5)["wake_escalated"] is True


def test_wake_escalated_conv_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "wn.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    hub_store.set_wake_escalated(5, at_message_id=7)

    ran = []
    async def fake_run_claude(*a, **k):
        ran.append(1)
        return "x", "s"
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    # the SAME message that was escalated on: an echo, still suppressed
    data = {"conversation_id": 5, "message_id": 7, "from_agent": "other", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))
    assert ran == []                                       # short-circuited


def test_newer_message_lifts_the_escalation_gag(tmp_path, monkeypatch):
    """#29: three messages over four hours all hit `201 Created` and woke nobody,
    because the flag had no way out. A message NEWER than the escalated one is
    fresh content — it re-opens the conversation and clears the flag."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "wu.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    hub_store.set_wake_escalated(33, at_message_id=90)

    ran = []
    async def fake_run_claude(*a, **k):
        ran.append(1)
        return "risposta", "sid-A"
    _set_verify_client(monkeypatch, [
        {"status": "open", "messages": [{"id": 92, "from_agent": "homeResearcher-minipc"}]}])
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"homeResearcher-minipc": "homeResearcher"},
        turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 33, "message_id": 91, "from_agent": "life", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "homeResearcher-minipc", "new_message", data))

    assert ran == [1]
    assert hub_store.wake_escalated_at(33) is None         # gag lifted


def test_un_gagging_also_restores_the_turn_budget(tmp_path, monkeypatch):
    """A conversation that hit BOTH guards (escalated and over the turn cap) must
    really come back: fresh content clears the gag and the turn counter with it,
    otherwise the cap keeps swallowing every event exactly as the flag did (#29)."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "wt2.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    for _ in range(12):
        hub_store.incr_turn(33)                            # at the cap
    hub_store.set_wake_escalated(33, at_message_id=90)

    ran = []
    async def fake_run_claude(*a, **k):
        ran.append(1)
        return "risposta", "sid-A"
    _set_verify_client(monkeypatch, [
        {"status": "open", "messages": [{"id": 92, "from_agent": "homeResearcher-minipc"}]}])
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"homeResearcher-minipc": "homeResearcher"},
        turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 33, "message_id": 91, "from_agent": "life", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "homeResearcher-minipc", "new_message", data))

    assert ran == [1]                                      # not eaten by the cap
    assert hub_store.get_conv(33)["turns"] == 1            # this event is turn 1


def test_legacy_escalated_record_is_lifted_by_any_message(tmp_path, monkeypatch):
    """Conversations already stuck in prod under the old flag heal by themselves."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "wl.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    rec = hub_store._load()
    rec["conversations"]["33"] = {"session_id": None, "turns": 0, "wake_escalated": True}
    hub_store._save(rec)
    monkeypatch.setattr(hub, "hub_store", hub_store)

    ran = []
    async def fake_run_claude(*a, **k):
        ran.append(1)
        return "risposta", "sid-A"
    _set_verify_client(monkeypatch, [
        {"status": "open", "messages": [{"id": 5, "from_agent": "homeResearcher-minipc"}]}])
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"homeResearcher-minipc": "homeResearcher"},
        turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 33, "message_id": 4, "from_agent": "life", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "homeResearcher-minipc", "new_message", data))

    assert ran == [1]


def test_transport_failure_is_retried_and_does_not_consume_an_attempt(tmp_path, monkeypatch):
    """#29: the bus was unreachable, both wake attempts died in 12 seconds without
    ever reaching a model, and the conversation was condemned. A transport fault
    is retried with backoff; only real non-answers count toward the 2 attempts."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "wt.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    replies = ["❌ claude exit=1\nbus down", "❌ claude exit=1\nbus down", "risposta"]
    runs = []
    async def fake_run_claude(prompt, *, workspace, persona, resume_id=None, extra_env=None, on_session=None):
        runs.append(resume_id)
        return replies.pop(0), "sid-A"

    # ONE verify call: the two transport failures never get that far.
    _set_verify_client(monkeypatch, [
        {"status": "open", "messages": [{"id": 2, "from_agent": "personalChef-local"}]}])
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "message_id": 1, "from_agent": "other", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert len(runs) == 3                                  # 2 retried + 1 that answered
    assert not (hub_store.get_conv(5) or {}).get("wake_escalated")


def test_transport_marker_survives_the_html_wrapper():
    """bot.py injects run_claude_html (Markdown -> Telegram HTML) as the wake's
    runner, so the transport marker the hub keys on must survive that conversion."""
    import bot
    import claude_runner

    assert claude_runner.is_transport_error(
        bot._md_to_html("❌ claude exit=1\nsession not found & gone"))
    assert not claude_runner.is_transport_error(
        bot._md_to_html("**Fatto**: ho aggiornato il digest."))


def test_durably_broken_transport_still_escalates(tmp_path, monkeypatch):
    """The retries are bounded: a transport that never recovers reaches a human."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "wtb.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(hub, "WAKE_TRANSPORT_RETRIES", 2)
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    runs = []
    async def fake_run_claude(*a, **k):
        runs.append(1)
        return "❌ claude exit=1\nbus down", None

    escalated = []
    class _FailClient:
        def __init__(self, *a):
            pass
        async def get_conversation(self, conv_id):
            return {"status": "open", "messages": [{"id": 1, "from_agent": "other"}]}
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            escalated.append((conv_id, needs_human))
            return {"id": 1}
    monkeypatch.setattr(hub.hub_client, "HubClient", _FailClient)
    monkeypatch.setitem(hub._S, "clients", {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "message_id": 7, "from_agent": "other", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert len(runs) == 3                                  # 1 + 2 retries, then stop
    assert escalated == [(5, True)]
    # gagged at the message we gave up on, NOT forever
    assert hub_store.wake_escalated_at(5) == 7


def test_listen_does_not_advance_cursor_on_outcome_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "lc.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    hub_store.set_cursor("personale", "personalChef-local", 100)

    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)

    calls = {"dispatch": 0}

    async def boom(*a, **k):
        calls["dispatch"] += 1
        raise hub.OutcomeUnknown(5)
    monkeypatch.setattr(hub, "_dispatch", boom)

    # 1st connection yields one event (id=200); the reconnect after the break
    # raises CancelledError to terminate _listen deterministically. The listener
    # REALLY runs (sleep is a no-op, not a barrier), so this discriminates: with
    # the OutcomeUnknown->break path the cursor stays 100; without it the generic
    # `except Exception` would advance it to 200.
    conn = {"n": 0}

    async def fake_stream(*a, **k):
        conn["n"] += 1
        if conn["n"] == 1:
            yield "new_message", {"conversation_id": 5, "message_id": 200}, 200
            return
        raise asyncio.CancelledError
    monkeypatch.setattr(hub.hub_client, "stream_events", fake_stream)

    try:
        asyncio.run(hub._listen("personale", "personalChef-local"))
    except asyncio.CancelledError:
        pass

    assert calls["dispatch"] == 1                       # the event was processed
    # cursor NOT advanced past the unverified event -> still 100
    assert hub_store.get_cursor("personale", "personalChef-local") == 100


def test_recover_backlog_rewakes_only_unanswered_recent_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "rb.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    # conv 4 escalated ON its last message (9): nothing new since -> still skipped.
    # conv 6 escalated on an OLDER message (8) and has since received #9: fresh
    # content, so the restart sweep re-wakes it (#29).
    hub_store.set_wake_escalated(4, at_message_id=9)
    hub_store.set_wake_escalated(6, at_message_id=8)

    woke = []
    async def fake_wake(tenant, agent_id, ev_type, data):
        woke.append(data["conversation_id"])
    monkeypatch.setattr(hub, "_wake_local_agent", fake_wake)

    recent = "2026-06-12T06:00:00+00:00"
    old = "2026-01-01T00:00:00+00:00"

    class _RecClient:
        def __init__(self, *a):
            pass
        async def get_inbox(self):
            return [
                {"id": 1, "last_message_at": recent},   # unanswered, recent -> wake
                {"id": 2, "last_message_at": recent},   # already answered -> skip
                {"id": 3, "last_message_at": old},      # too old -> skip
                {"id": 4, "last_message_at": recent},   # escalated -> skip
                {"id": 5, "last_message_at": recent},   # resolved -> skip
                {"id": 6, "last_message_at": recent},   # escalated, but newer msg -> wake
            ]
        async def get_conversation(self, conv_id):
            detail = {
                1: {"status": "open", "subject": "s", "priority": "normal",
                    "messages": [{"id": 9, "from_agent": "other", "body": "b"}]},
                2: {"status": "open", "messages": [{"id": 9, "from_agent": "personalChef-local"}]},
                4: {"status": "open", "messages": [{"id": 9, "from_agent": "other"}]},
                5: {"status": "resolved", "messages": [{"id": 9, "from_agent": "other"}]},
                6: {"status": "open", "subject": "s", "priority": "normal",
                    "messages": [{"id": 9, "from_agent": "other", "body": "b"}]},
            }
            return detail[conv_id]

    monkeypatch.setattr(hub.hub_client, "HubClient", _RecClient)
    monkeypatch.setitem(hub._S, "clients", {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"personalChef-local": "personalChef"},
        turn_cap=12, recovery_window_h=24, recovery_max=20)
    monkeypatch.setitem(hub._S, "config", cfg)

    # freeze "now" near recent so the 24h window includes `recent`, excludes `old`
    import datetime as _dt
    class _FrozenDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 6, 12, 12, 0, 0, tzinfo=tz or _dt.timezone.utc)
    monkeypatch.setattr(hub._dt, "datetime", _FrozenDT)

    asyncio.run(hub._recover_backlog("personale", "personalChef-local"))
    assert woke == [1, 6]


import pipeline as _pipeline_pl


def test_human_event_routes_pipeline_verdict(monkeypatch):
    seen = []

    async def fake_on_verdict(conv_id, message_id):
        seen.append((conv_id, message_id))
        return True

    monkeypatch.setattr(_pipeline_pl, "on_verdict", fake_on_verdict)
    data = {"conversation_id": 77, "message_id": 5, "from_agent": "demo-dev",
            "needs_human": False}
    asyncio.run(hub._handle_human_event("acme", "new_message", data))
    assert seen == [(77, 5)]


import migrate as _migrate_mod
import pipeline as _pipeline_mod


def test_human_event_routes_migrate_verdict(monkeypatch):
    seen = []

    async def fake_pipeline_verdict(conv_id, message_id):
        return False                       # not a pipeline conv

    async def fake_migrate_verdict(conv_id, message_id):
        seen.append((conv_id, message_id))
        return True

    monkeypatch.setattr(hub.pipeline, "on_verdict", fake_pipeline_verdict)
    monkeypatch.setattr(_migrate_mod, "on_verdict", fake_migrate_verdict)
    data = {"conversation_id": 88, "message_id": 6, "from_agent": "db-prod",
            "needs_human": False}
    asyncio.run(hub._handle_human_event("acme", "new_message", data))
    assert seen == [(88, 6)]


# --- wake-path escalation safety net ---------------------------------------

class _EscClient:
    """Fake HubClient: get_conversation returns a fixed conv dict; send_message
    appends to a shared `posts` list so the test can assert what the worker posted."""
    conv: dict = {}
    posts: list = []

    def __init__(self, *a):
        pass

    async def get_conversation(self, conv_id):
        return _EscClient.conv

    async def send_message(self, conv_id, body, needs_human=False, context=None):
        _EscClient.posts.append((conv_id, body, needs_human))
        return {"id": 999}


def _set_esc_client(monkeypatch, conv):
    _EscClient.conv = conv
    _EscClient.posts = []
    monkeypatch.setattr(hub.hub_client, "HubClient", _EscClient)
    monkeypatch.setitem(hub._S, "clients", {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)


def test_surface_escalation_posts_needs_human_then_idempotent(monkeypatch):
    # Agent posted a typed escalation but did NOT set needs_human.
    conv = {"status": "open", "messages": [
        {"id": 11, "from_agent": "personalChef-local", "needs_human": False,
         "context": {"escalation": {"blocked": True, "reason": "GDPR",
                                    "decision_needed": "Autorizzi la copia?"}}}]}
    _set_esc_client(monkeypatch, conv)

    asyncio.run(hub._maybe_surface_escalation("personale", "personalChef-local", 5, since=10))
    assert len(_EscClient.posts) == 1
    conv_id, body, needs_human = _EscClient.posts[0]
    assert conv_id == 5 and needs_human is True
    assert "GDPR" in body and "Autorizzi la copia?" in body

    # Idempotency: a needs_human message now exists after `since` -> no second post.
    _EscClient.conv = {"status": "open", "messages": conv["messages"] + [
        {"id": 12, "from_agent": "personalChef-local", "needs_human": True,
         "context": None}]}
    asyncio.run(hub._maybe_surface_escalation("personale", "personalChef-local", 5, since=10))
    assert len(_EscClient.posts) == 1   # nothing more posted


def test_surface_escalation_skips_resolved(monkeypatch):
    conv = {"status": "resolved", "messages": [
        {"id": 11, "from_agent": "personalChef-local", "needs_human": False,
         "context": {"escalation": {"blocked": True, "reason": "x",
                                    "decision_needed": "y"}}}]}
    _set_esc_client(monkeypatch, conv)
    asyncio.run(hub._maybe_surface_escalation("personale", "personalChef-local", 5, since=10))
    assert _EscClient.posts == []


def test_surface_escalation_skips_when_needs_human_already_raised(monkeypatch):
    conv = {"status": "open", "messages": [
        {"id": 11, "from_agent": "personalChef-local", "needs_human": True,
         "context": {"escalation": {"blocked": True, "reason": "x",
                                    "decision_needed": "y"}}}]}
    _set_esc_client(monkeypatch, conv)
    asyncio.run(hub._maybe_surface_escalation("personale", "personalChef-local", 5, since=10))
    assert _EscClient.posts == []


def test_surface_escalation_no_typed_escalation_no_post(monkeypatch):
    # A plain agent reply (no escalation context) must NOT be pushed to the human.
    conv = {"status": "open", "messages": [
        {"id": 11, "from_agent": "personalChef-local", "needs_human": False,
         "context": None, "body": "fatto, ho riprocessato il batch"}]}
    _set_esc_client(monkeypatch, conv)
    asyncio.run(hub._maybe_surface_escalation("personale", "personalChef-local", 5, since=10))
    assert _EscClient.posts == []


def test_surface_escalation_swallows_fetch_error(monkeypatch):
    # "never break the wake": get_conversation raising must not propagate and
    # must result in no post.
    _set_esc_client(monkeypatch, {"status": "open", "messages": []})

    async def _boom(self, conv_id):
        raise RuntimeError("hub down")
    monkeypatch.setattr(_EscClient, "get_conversation", _boom)

    result = asyncio.run(
        hub._maybe_surface_escalation("personale", "personalChef-local", 5, since=10))
    assert result is None
    assert _EscClient.posts == []


def test_surface_escalation_swallows_post_error(monkeypatch):
    # A valid escalation is found but send_message raises -> still no propagation.
    conv = {"status": "open", "messages": [
        {"id": 11, "from_agent": "personalChef-local", "needs_human": False,
         "context": {"escalation": {"blocked": True, "reason": "GDPR",
                                    "decision_needed": "Autorizzi?"}}}]}
    _set_esc_client(monkeypatch, conv)

    async def _boom(self, conv_id, body, needs_human=False, context=None):
        raise RuntimeError("post failed")
    monkeypatch.setattr(_EscClient, "send_message", _boom)

    result = asyncio.run(
        hub._maybe_surface_escalation("personale", "personalChef-local", 5, since=10))
    assert result is None
    assert _EscClient.posts == []


def test_topic_reply_routes_migration_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "mig.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    posted = []

    class FakeClient:
        def __init__(self, *a):
            pass
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            posted.append((conv_id, body))
            return {"id": 1}

    monkeypatch.setattr(hub.hub_client, "HubClient", FakeClient)
    monkeypatch.setitem(hub._S, "clients", {})
    monkeypatch.setitem(hub._S, "config", hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={}, turn_cap=12))

    async def fake_on_human_decision(chat_id, topic_id, text):
        return True   # this topic belongs to a migration awaiting_human
    monkeypatch.setattr(_migrate_mod, "on_human_decision", fake_on_human_decision)

    handled = asyncio.run(hub.handle_topic_reply(-100, 42, "procedi"))
    assert handled is True
    assert posted == []   # migration handler owns it; nothing posted to the hub bus


def test_topic_reply_routes_pipeline_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "pipe.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    posted = []

    class FakeClient:
        def __init__(self, *a):
            pass
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            posted.append((conv_id, body))
            return {"id": 1}

    monkeypatch.setattr(hub.hub_client, "HubClient", FakeClient)
    monkeypatch.setitem(hub._S, "clients", {})
    monkeypatch.setitem(hub._S, "config", hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={}, turn_cap=12))

    async def fake_migrate_no_handle(chat_id, topic_id, text):
        return False   # not a migration topic -> falls through to pipeline check
    monkeypatch.setattr(_migrate_mod, "on_human_decision", fake_migrate_no_handle)

    async def fake_on_human_decision(chat_id, topic_id, text):
        return True   # this topic belongs to a pipeline awaiting_human
    monkeypatch.setattr(_pipeline_mod, "on_human_decision", fake_on_human_decision)

    handled = asyncio.run(hub.handle_topic_reply(-100, 42, "procedi"))
    assert handled is True
    assert posted == []   # pipeline handler owns it; nothing posted to the hub bus


def test_topic_reply_falls_through_when_not_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "mig2.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    hub_store.upsert_conv(5, tenant="personale", chat_id=-100, topic_id=77)

    sent = {}

    class FakeClient:
        def __init__(self, *a):
            pass
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            sent.update(conv_id=conv_id, body=body)
            return {"id": 1}

    monkeypatch.setattr(hub.hub_client, "HubClient", FakeClient)
    monkeypatch.setitem(hub._S, "clients", {})
    monkeypatch.setitem(hub._S, "config", hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={}, turn_cap=12))

    async def fake_on_human_decision(chat_id, topic_id, text):
        return False   # not a migration topic -> fall through to hub-conversation path
    monkeypatch.setattr(_migrate_mod, "on_human_decision", fake_on_human_decision)

    handled = asyncio.run(hub.handle_topic_reply(-100, 77, "usa il fornitore B"))
    assert handled is True
    assert sent == {"conv_id": 5, "body": "usa il fornitore B"}


# append to tests/test_hub.py
import asyncio as _asyncio_cw

import hub as _hub_cw
import wake_pool as _wp_cw


async def _agen(items):
    for it in items:
        yield it


def test_consume_local_runs_different_convs_concurrently(monkeypatch):
    _wp_cw.configure(5)
    active = 0
    peak = 0

    async def fake_dispatch(tenant, agent_id, ev_type, data):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await _asyncio_cw.sleep(0.02)
        active -= 1

    monkeypatch.setattr(_hub_cw, "_dispatch", fake_dispatch)
    monkeypatch.setattr(_hub_cw.hub_store, "set_cursor", lambda *a, **k: None)

    events = _agen([
        ("new_message", {"conversation_id": 1}, 201),
        ("new_message", {"conversation_id": 2}, 202),
    ])
    n = _asyncio_cw.run(
        _hub_cw._consume("acme", "offerte-dev", False, events, 200))
    assert n == 2
    assert peak == 2


def test_consume_telegram_is_serial(monkeypatch):
    active = 0
    peak = 0

    async def fake_dispatch(tenant, agent_id, ev_type, data):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await _asyncio_cw.sleep(0.02)
        active -= 1

    monkeypatch.setattr(_hub_cw, "_dispatch", fake_dispatch)
    cursors = []
    monkeypatch.setattr(_hub_cw.hub_store, "set_cursor",
                        lambda t, a, c: cursors.append(c))

    events = _agen([
        ("new_message", {"conversation_id": 1}, 301),
        ("new_message", {"conversation_id": 2}, 302),
    ])
    n = _asyncio_cw.run(
        _hub_cw._consume("acme", "owner-telegram", True, events, 300))
    assert n == 2
    assert peak == 1
    assert cursors == [301, 302]


def test_consume_local_drains_and_advances_cursor(monkeypatch):
    _wp_cw.configure(5)

    async def fake_dispatch(tenant, agent_id, ev_type, data):
        await _asyncio_cw.sleep(0.01)

    monkeypatch.setattr(_hub_cw, "_dispatch", fake_dispatch)
    persisted = []
    monkeypatch.setattr(_hub_cw.hub_store, "set_cursor",
                        lambda t, a, c: persisted.append(c))

    events = _agen([
        ("new_message", {"conversation_id": 1}, 401),
        ("new_message", {"conversation_id": 2}, 402),
    ])
    _asyncio_cw.run(
        _hub_cw._consume("acme", "offerte-dev", False, events, 400))
    assert persisted[-1] == 402


def test_wake_prompt_documents_typed_verdicts():
    import hub
    assert "pipeline_verdict" in hub._WAKE_PROMPT
    assert "migrate_verdict" in hub._WAKE_PROMPT


# ---------------------------------------------------------------------------
# Boot-time roster reconciliation (#2 follow-up): an agent configured here but
# unknown to the bus answers 403 to every call, for ever, with no single line
# saying so. _check_local_roster turns that into one actionable error.
# ---------------------------------------------------------------------------


def _roster_cfg(local_agents=None):
    return hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="telegram-owner", telegram_chat_id=-100,
        local_agents=local_agents or {"cosmos-prod": "cosmos", "network-prod": "network"},
        turn_cap=12,
    )


class _FakeRosterClient:
    def __init__(self, roster=None, exc=None):
        self._roster, self._exc = roster or [], exc

    async def list_agents(self, tenant):
        if self._exc:
            raise self._exc
        return self._roster


def test_roster_check_names_the_identity_that_would_have_worked(monkeypatch, caplog):
    monkeypatch.setitem(hub._S, "config", _roster_cfg())
    roster = [
        {"name": "cosmos", "project": "cosmos", "is_active": True},
        {"name": "cosmos-minipc", "project": "cosmos", "is_active": True},
    ]
    monkeypatch.setattr(hub, "_client", lambda t, a: _FakeRosterClient(roster))

    with caplog.at_level("ERROR"):
        _run(hub._check_local_roster())

    text = caplog.text
    assert "cosmos-prod" in text and "cosmos, cosmos-minipc" in text
    # the second missing agent has no counterpart at all -- still reported
    assert "network-prod" in text and "No agent is registered for that project" in text


def test_roster_check_is_quiet_when_everything_is_registered(monkeypatch, caplog):
    monkeypatch.setitem(hub._S, "config", _roster_cfg({"cosmos": "cosmos"}))
    roster = [{"name": "cosmos", "project": "cosmos", "is_active": True}]
    monkeypatch.setattr(hub, "_client", lambda t, a: _FakeRosterClient(roster))

    with caplog.at_level("ERROR"):
        _run(hub._check_local_roster())
    assert caplog.records == []


def test_roster_check_reports_a_node_with_no_accepted_identity(monkeypatch, caplog):
    """If even the roster call is refused the node has no working identity at
    all -- a superset of the per-agent problem, and just as invisible."""
    monkeypatch.setitem(hub._S, "config", _roster_cfg())
    monkeypatch.setattr(
        hub, "_client", lambda t, a: _FakeRosterClient(exc=RuntimeError("403"))
    )

    with caplog.at_level("ERROR"):
        _run(hub._check_local_roster())
    assert "roster check FAILED" in caplog.text


def test_roster_check_ignores_deactivated_agents(monkeypatch, caplog):
    """An agent row left on the bus but flagged inactive answers 403 like a
    missing one, so it must not count as registered."""
    monkeypatch.setitem(hub._S, "config", _roster_cfg({"cosmos": "cosmos"}))
    roster = [{"name": "cosmos", "project": "cosmos", "is_active": False}]
    monkeypatch.setattr(hub, "_client", lambda t, a: _FakeRosterClient(roster))

    with caplog.at_level("ERROR"):
        _run(hub._check_local_roster())
    assert "bus identity MISSING" in caplog.text


def test_roster_check_warns_about_device_agents_nobody_listens_to(monkeypatch, caplog):
    """#58: registered as `<key>-<dev_device>` but absent from HUB_LOCAL_AGENTS
    means conversations to it wake nobody, while the forward check says OK."""
    import dataclasses
    cfg = dataclasses.replace(
        _roster_cfg({"offerte-dev-linux": "offerte"}), dev_device="dev-linux"
    )
    monkeypatch.setitem(hub._S, "config", cfg)
    roster = [
        {"name": "offerte-dev-linux", "project": "offerte", "is_active": True},
        {"name": "ticket-manager-dev-linux", "project": "ticketManager", "is_active": True},
        {"name": "launcher-dev", "project": "launcher", "is_active": True},
        {"name": "old-dev-linux", "project": "old", "is_active": False},
    ]
    monkeypatch.setattr(hub, "_client", lambda t, a: _FakeRosterClient(roster))

    with caplog.at_level("WARNING"):
        _run(hub._check_local_roster())

    [warning] = [r.getMessage() for r in caplog.records if "UNHEARD" in r.getMessage()]
    unheard = warning.split(":", 1)[1].split(" registered", 1)[0]
    assert unheard.strip() == "ticket-manager-dev-linux"
    # already served, another device's suffix, inactive rows: none are reported


def test_roster_check_skips_reverse_direction_without_dev_device(monkeypatch, caplog):
    monkeypatch.setitem(hub._S, "config", _roster_cfg({"cosmos": "cosmos"}))
    roster = [
        {"name": "cosmos", "project": "cosmos", "is_active": True},
        {"name": "x-dev-linux", "project": "x", "is_active": True},
    ]
    monkeypatch.setattr(hub, "_client", lambda t, a: _FakeRosterClient(roster))

    with caplog.at_level("WARNING"):
        _run(hub._check_local_roster())
    assert "UNHEARD" not in caplog.text


# ---------------------------------------------------------------------------
# A2A webhook mode is a BOT capability. HUB_A2A_WEBHOOK_URL is a per-device var
# read by every service on the box, but only the bot serves /a2a/webhook (it is
# mounted on api.app) and only bot.on_startup registers the push configs. A
# worker that honoured it dropped its SSE listeners and received nothing.
# ---------------------------------------------------------------------------


def _webhook_cfg(role):
    return hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="telegram-owner", telegram_chat_id=-100,
        local_agents={"cosmos": "cosmos"}, turn_cap=12, role=role,
        a2a_webhook_url="http://pc:10027", a2a_webhook_token="t",
    )


def _count_listeners(monkeypatch, cfg):
    """Run start_local_listeners with every background task stubbed out."""
    spawned = []
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "local_tasks", [])
    monkeypatch.setitem(hub._S, "status_task", None)
    monkeypatch.setitem(hub._S, "roster_task", None)

    class _FakeTask:
        def done(self):
            return False

    def fake_create_task(coro):
        coro.close()  # never actually run: we only count the _listen calls
        return _FakeTask()

    monkeypatch.setattr(hub.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(hub, "_listen", lambda t, a: spawned.append((t, a)) or _noop())
    return hub.start_local_listeners(), spawned


def _noop():
    async def _c():
        return None
    return _c()


def test_worker_keeps_sse_despite_webhook_mode(monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        n, spawned = _count_listeners(monkeypatch, _webhook_cfg("worker"))
    assert spawned == [("personale", "cosmos")]
    assert n == 1
    assert "A2A webhook mode ignored for role=worker" in caplog.text


def test_bot_still_drops_sse_in_webhook_mode(monkeypatch):
    n, spawned = _count_listeners(monkeypatch, _webhook_cfg("hub"))
    assert spawned == [] and n == 0


def test_worker_refuses_to_register_push_configs(monkeypatch, caplog):
    """Registering would make Network consider the worker's agents reachable and
    stop warning about undelivered messages — worse than the SSE it replaced."""
    monkeypatch.setitem(hub._S, "config", _webhook_cfg("worker"))
    called = []
    monkeypatch.setattr(hub, "_a2a_client", lambda t, a: called.append(a))

    with caplog.at_level("WARNING"):
        assert _run(hub.register_push_webhooks()) == 0
    assert called == []
    assert "refused for role=worker" in caplog.text


# --- #32: un 401/403 non e' un guasto di rete -------------------------------


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeHTTPStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"Client error '{status_code}'")
        self.response = _FakeResp(status_code)


def _auth_cfg():
    return hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"acme": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"itvault-dev-linux": "itvault"},
        turn_cap=12)


def test_listen_gives_up_after_n_auth_rejects(tmp_path, monkeypatch):
    """Un 401 ripetuto ferma il listener invece di ritentare per sempre."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "a.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    monkeypatch.setitem(hub._S, "config", _auth_cfg())
    monkeypatch.setitem(hub._S, "dead_listeners", {})
    monkeypatch.setitem(hub._S, "send_to", None)
    monkeypatch.setattr(hub, "_ensure_bus_epoch", lambda tenant: asyncio.sleep(0))

    conn = {"n": 0}

    async def fake_stream(*a, **k):
        conn["n"] += 1
        raise _FakeHTTPStatusError(401)
        yield  # pragma: no cover - makes this an async generator
    monkeypatch.setattr(hub.hub_client, "stream_events", fake_stream)

    asyncio.run(hub._listen("acme", "itvault-dev-linux"))

    assert conn["n"] == hub.AUTH_REJECTS_BEFORE_GIVING_UP  # e poi basta
    dead = hub.dead_listeners()
    assert [(d["tenant"], d["agent"], d["status"]) for d in dead] == [
        ("acme", "itvault-dev-linux", 401)
    ]


def test_listen_keeps_retrying_transport_faults(tmp_path, monkeypatch):
    """Un errore di rete resta retry-safe: nessuna rinuncia, nessun morto."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "b.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    monkeypatch.setitem(hub._S, "config", _auth_cfg())
    monkeypatch.setitem(hub._S, "dead_listeners", {})
    monkeypatch.setattr(hub, "_ensure_bus_epoch", lambda tenant: asyncio.sleep(0))

    conn = {"n": 0}

    async def fake_stream(*a, **k):
        conn["n"] += 1
        if conn["n"] > hub.AUTH_REJECTS_BEFORE_GIVING_UP + 2:
            raise asyncio.CancelledError
        raise ConnectionError("bus unreachable")
        yield  # pragma: no cover
    monkeypatch.setattr(hub.hub_client, "stream_events", fake_stream)

    with __import__("pytest").raises(asyncio.CancelledError):
        asyncio.run(hub._listen("acme", "itvault-dev-linux"))
    assert hub.dead_listeners() == []


def test_listen_auth_reject_counter_resets_on_a_good_connection(tmp_path, monkeypatch):
    """Rifiuti NON consecutivi non sommano: la soglia parla di uno stato stabile."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    monkeypatch.setitem(hub._S, "config", _auth_cfg())
    monkeypatch.setitem(hub._S, "dead_listeners", {})
    monkeypatch.setattr(hub, "_ensure_bus_epoch", lambda tenant: asyncio.sleep(0))

    # 401, 401, stream buono, 401 -> il contatore riparte, nessuna rinuncia
    script = [401, 401, None, 401]
    conn = {"n": 0}

    async def fake_stream(*a, **k):
        i = conn["n"]
        conn["n"] += 1
        if i >= len(script):
            raise asyncio.CancelledError
        if script[i] is not None:
            raise _FakeHTTPStatusError(script[i])
        return
        yield  # pragma: no cover
    monkeypatch.setattr(hub.hub_client, "stream_events", fake_stream)

    with __import__("pytest").raises(asyncio.CancelledError):
        asyncio.run(hub._listen("acme", "itvault-dev-linux"))
    assert hub.dead_listeners() == []


def test_auth_reject_status_reads_the_cause_chain():
    inner = _FakeHTTPStatusError(403)
    try:
        try:
            raise inner
        except Exception as exc:
            raise RuntimeError("wrapped") from exc
    except Exception as outer:
        assert hub._auth_reject_status(outer) == 403
    assert hub._auth_reject_status(ConnectionError("nope")) is None


def _origin_cfg(monkeypatch):
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "clients", {})
    monkeypatch.setattr(hub.claude_runner, "host_exec_enabled", lambda: True)
    monkeypatch.setattr(hub.hub_client, "nome_host", lambda: "hostA")


def test_wake_resumes_origin_session(tmp_path, monkeypatch):
    # Origin session on THIS host, for THIS agent, closed (no registry entry):
    # the wake must resume it in its own cwd instead of a derived session (#51).
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h_origin.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    _origin_cfg(monkeypatch)

    from projects import escape_path

    origin = {"session_id": "sid-origin", "host": "hostA", "cwd": "/nodes/foo",
              "agent": "personalChef-local"}
    projects_dir = tmp_path / "projects"
    d = projects_dir / escape_path("/nodes/foo")
    d.mkdir(parents=True, exist_ok=True)
    (d / "sid-origin.jsonl").write_text("x", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)  # visible, empty: session ended
    monkeypatch.setattr(hub, "_HUB_PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(hub, "_HUB_SESSIONS_DIR", sessions_dir)

    calls = {}

    async def fake_run_claude(prompt, *, workspace, persona, resume_id=None,
                              extra_env=None, host_cwd=None, on_session=None):
        calls.update(resume_id=resume_id, host_cwd=host_cwd, workspace=workspace)
        return "done", "sid-origin"

    class _OkClient:
        def __init__(self, *a):
            pass
        async def get_conversation(self, conv_id):
            return {"status": "open", "origin_session": origin,
                    "messages": [{"id": 999, "from_agent": "personalChef-local"}]}
    monkeypatch.setattr(hub.hub_client, "HubClient", _OkClient)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "from_agent": "mycorise-local",
            "subject": "s", "body": "riprocessa", "priority": "normal"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert calls["resume_id"] == "sid-origin"
    assert calls["host_cwd"] == "/nodes/foo"


def test_wake_skips_origin_on_another_host(tmp_path, monkeypatch):
    # The conversation was opened from a session on ANOTHER host: this dispatcher
    # is not the one to resume it, and must not spawn a headless twin (#51).
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h_origin_skip.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    _origin_cfg(monkeypatch)

    origin = {"session_id": "sid-origin", "host": "hostB", "cwd": "/nodes/foo",
              "agent": "personalChef-local"}

    ran = []

    async def fake_run_claude(*a, **k):
        ran.append(1)
        return "x", "sid"

    class _OkClient:
        def __init__(self, *a):
            pass
        async def get_conversation(self, conv_id):
            return {"status": "open", "origin_session": origin,
                    "messages": [{"id": 999, "from_agent": "personalChef-local"}]}
    monkeypatch.setattr(hub.hub_client, "HubClient", _OkClient)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "from_agent": "mycorise-local",
            "subject": "s", "body": "riprocessa", "priority": "normal"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert ran == []  # never spawned
    assert (hub_store.get_conv(5) or {}).get("turns", 0) == 0  # no turn consumed


def test_wake_skips_while_a_terminal_session_is_replying(tmp_path, monkeypatch):
    # #54: no origin_session for this agent -- the terminal session REPLIED on a
    # conversation someone else opened -- and it is still alive. A headless twin
    # would answer under the same name: it must not be spawned.
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h_voice.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    _origin_cfg(monkeypatch)

    from projects import escape_path

    voice = {"session_id": "sid-term", "host": "hostA", "cwd": "/nodes/foo",
             "agent": "personalChef-local"}
    projects_dir = tmp_path / "projects"
    d = projects_dir / escape_path("/nodes/foo")
    d.mkdir(parents=True, exist_ok=True)
    (d / "sid-term.jsonl").write_text("x", encoding="utf-8")  # mtime = now
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "sid-term.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hub, "_HUB_PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(hub, "_HUB_SESSIONS_DIR", sessions_dir)

    ran = []

    async def fake_run_claude(*a, **k):
        ran.append(1)
        return "x", "sid"

    class _OkClient:
        def __init__(self, *a):
            pass
        async def get_conversation(self, conv_id):
            return {"status": "open", "origin_session": None, "messages": [
                {"id": 1, "from_agent": "mycorise-local"},
                {"id": 2, "from_agent": "personalChef-local",
                 "context": {"voice": "interactive", "session": voice}},
                {"id": 3, "from_agent": "mycorise-local"},
            ]}
    monkeypatch.setattr(hub.hub_client, "HubClient", _OkClient)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 6, "message_id": 3, "from_agent": "mycorise-local",
            "subject": "s", "body": "e allora?", "priority": "normal"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert ran == []
    assert (hub_store.get_conv(6) or {}).get("turns", 0) == 0


def _two_silent_agents(tmp_path, monkeypatch, name):
    """Two local agents on one conversation, neither of which ever answers.
    The fake bus records every post and hands it back as the next event."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / name))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    bus = {"messages": [{"id": 1, "from_agent": "human", "body": "ciao"}]}
    runs = []

    async def fake_run_claude(prompt, **k):
        runs.append(prompt)
        return "x", "sid"

    class _BusClient:
        def __init__(self, base_url, api_key, agent_id, *a, **k):
            self.agent_id = agent_id
        async def get_conversation(self, conv_id):
            return {"status": "open", "messages": list(bus["messages"])}
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            msg = {"id": len(bus["messages"]) + 1, "from_agent": self.agent_id,
                   "body": body, "needs_human": needs_human}
            bus["messages"].append(msg)
            return msg
    monkeypatch.setattr(hub.hub_client, "HubClient", _BusClient)
    monkeypatch.setitem(hub._S, "clients", {})
    monkeypatch.setitem(hub._S, "inflight_wakes", {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100,
        local_agents={"a-dev": "personalChef", "b-dev": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)
    return hub_store, bus, runs


def _event(msg):
    return {"conversation_id": 290, "message_id": msg["id"],
            "from_agent": msg["from_agent"], "body": msg["body"]}


def test_auto_notices_do_not_wake_each_other(tmp_path, monkeypatch):
    """#59, conv. #290: A gives up and posts "non ha risposto" AS A; that notice
    reached B as a newer message, lifted B's gag, reset the turns, B gave up and
    posted its own -- forever. Now: one notice, and nobody is woken by it."""
    hub_store, bus, runs = _two_silent_agents(tmp_path, monkeypatch, "p.json")
    # B was already gagged on an earlier message, as in the incident.
    hub_store.set_wake_escalated(290, at_message_id=0)

    asyncio.run(hub._wake_local_agent(
        "personale", "a-dev", "new_message", _event(bus["messages"][0])))
    assert len(runs) == 2                                  # A's two attempts
    notices = [m for m in bus["messages"] if m.get("needs_human")]
    assert len(notices) == 1 and hub.is_auto_notice(notices[0]["body"])

    # Deliver A's notice to both agents, as the bus does.
    for agent in ("b-dev", "a-dev"):
        asyncio.run(hub._wake_local_agent(
            "personale", agent, "new_message", _event(notices[0])))

    assert len(runs) == 2                                  # nobody woken
    assert len([m for m in bus["messages"] if m.get("needs_human")]) == 1
    assert hub_store.wake_escalated_at(290) is not None    # gag NOT lifted
    assert hub_store.get_conv(290)["turns"] == 1           # turns NOT reset


def test_one_notice_per_conversation_until_real_content(tmp_path, monkeypatch):
    """Even if both agents fail on the same real message, only the first notice
    lands; a real message re-arms the conversation for the next one."""
    hub_store, bus, runs = _two_silent_agents(tmp_path, monkeypatch, "q.json")
    first = _event(bus["messages"][0])
    asyncio.run(hub._wake_local_agent("personale", "a-dev", "new_message", first))
    asyncio.run(hub._wake_local_agent("personale", "b-dev", "new_message", first))
    assert len([m for m in bus["messages"] if m.get("needs_human")]) == 1

    bus["messages"].append({"id": len(bus["messages"]) + 1,
                            "from_agent": "human", "body": "riprovate"})
    asyncio.run(hub._wake_local_agent(
        "personale", "a-dev", "new_message", _event(bus["messages"][-1])))
    assert len([m for m in bus["messages"] if m.get("needs_human")]) == 2


def test_is_auto_notice_recognises_tag_and_legacy_wording():
    assert hub.is_auto_notice(hub._auto_notice("qualunque cosa"))
    assert hub.is_auto_notice(
        "L'agente x-dev non ha risposto alla conversazione #290 dopo 2 tentativi "
        "automatici: serve un umano.")
    assert hub.is_auto_notice(
        "Raggiunto il tetto di 12 turni automatici: serve una decisione umana.")
    assert not hub.is_auto_notice("L'agente x-dev dice che il deploy e' fatto")
    assert not hub.is_auto_notice(None)


def test_deliberate_no_reply_is_handled_not_escalated(tmp_path, monkeypatch):
    """#61, conv. #290: the woken agent reads a conclusive message and rightly
    stays silent. With the marker that is an outcome: no retry, no notice."""
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "nr.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    runs = []
    prompts = []

    async def fake_run_claude(prompt, **k):
        runs.append(1)
        prompts.append(prompt)
        return f"Messaggio conclusivo, niente da aggiungere.\n{hub.NO_REPLY_MARKER}", "sid-A"

    posted = []
    class _SilentClient:
        def __init__(self, *a):
            pass
        async def get_conversation(self, conv_id):
            return {"status": "open", "messages": [{"id": 1, "from_agent": "other"}]}
        async def send_message(self, conv_id, body, needs_human=False, context=None):
            posted.append((conv_id, body))
            return {"id": 2}
    monkeypatch.setattr(hub.hub_client, "HubClient", _SilentClient)
    monkeypatch.setitem(hub._S, "clients", {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"personale": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"personalChef-local": "personalChef"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "message_id": 1, "from_agent": "other",
            "body": "Non vi serve fare altro"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert runs == [1]                                    # no second attempt
    assert posted == []                                   # no "non ha risposto"
    assert not hub_store.get_conv(5).get("wake_escalated")
    assert hub.NO_REPLY_MARKER in prompts[0]              # the worker is told how


def test_chose_not_to_reply():
    assert hub.chose_not_to_reply(f"ok\n{hub.NO_REPLY_MARKER}")
    assert not hub.chose_not_to_reply("ho risposto")
    assert not hub.chose_not_to_reply("")
    assert not hub.chose_not_to_reply(None)


# --- TicketManager#10: session registered at start, never twinned -----------


def test_wake_registers_session_running_at_first_event(tmp_path, monkeypatch):
    # The Launcher link must work WHILE the session works: the session is on
    # record (running) from the run's first event, and stops running after.
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr(hub.asyncio, "sleep", _noop_sleep)
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)

    during = {}

    async def fake_run_claude(prompt, *, workspace, persona, resume_id=None,
                              extra_env=None, on_session=None):
        on_session("sid-live")
        during.update(hub_store.get_conv(5)["sessions"]["personalChef-local"])
        return "fatto", "sid-live"

    _set_verify_client(monkeypatch, [
        {"status": "open", "messages": [{"id": 2, "from_agent": "personalChef-local"}]}])
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "message_id": 1, "from_agent": "other", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))

    assert during["session_id"] == "sid-live" and during["running"] is True
    assert during["started_at"]
    after = hub_store.get_conv(5)["sessions"]["personalChef-local"]
    assert after["running"] is False
    assert after["started_at"] == during["started_at"]


def test_wake_skips_session_held_by_interactive_resume(tmp_path, monkeypatch):
    # The Launcher resumed the worker's own session interactively (in its
    # worktree): a headless resume would be a second process on that UUID.
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "held.json"))
    import importlib
    importlib.reload(hub_store := __import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    _origin_cfg(monkeypatch)
    hub_store.set_session(5, "sid-live", agent_id="personalChef-local", workspace="")

    from projects import escape_path

    projects_dir = tmp_path / "projects"
    d = projects_dir / escape_path("/wt/foo")
    d.mkdir(parents=True)
    (d / "sid-live.jsonl").write_text('{"cwd":"/wt/foo"}\n', encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "sid-live.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hub, "_HUB_PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(hub, "_HUB_SESSIONS_DIR", sessions_dir)

    class _NoOriginClient:
        def __init__(self, *a):
            pass
        async def get_conversation(self, conv_id):
            return {"status": "open", "messages": [
                {"id": 1, "from_agent": "other"},
                {"id": 9, "from_agent": "personalChef-local"}]}
        def __getattr__(self, name):
            async def _noop(*a, **k):
                return {}
            return _noop
    monkeypatch.setattr(hub.hub_client, "HubClient", _NoOriginClient)

    ran = []

    async def fake_run_claude(*a, **k):
        ran.append(k.get("resume_id"))
        return "x", "sid-live"
    monkeypatch.setitem(hub._S, "run_claude", fake_run_claude)

    data = {"conversation_id": 5, "message_id": 1, "from_agent": "other", "body": "b"}
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message", data))
    assert ran == []

    # Once the interactive session is gone, the worker resumes it again.
    (sessions_dir / "sid-live.json").unlink()
    asyncio.run(hub._wake_local_agent("personale", "personalChef-local", "new_message",
                                      {**data, "message_id": 3}))
    assert ran and ran[0] == "sid-live"
