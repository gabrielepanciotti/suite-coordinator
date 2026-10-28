"""Issue #67: a wake that finds the agent's headless slot busy (#38) is parked,
not dropped, and runs when the slot frees up -- also across a restart."""

from __future__ import annotations

import asyncio

import pytest

import hub
import hub_store
import projects


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(hub_store, "HUB_TOPICS_PATH", str(tmp_path / "hub_topics.json"))
    for key in ("inflight_wakes", "inflight_msgs", "wake_drainers", "clients"):
        monkeypatch.setitem(hub._S, key, {})
    monkeypatch.setitem(hub._S, "interrupted_requeued", False)
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"acme": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"etl-dev": "etl"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setattr(projects, "resolve_project",
                        lambda key: type("P", (), {"key": key, "workspace": "/ws"})())
    monkeypatch.setattr(hub_store, "is_watched", lambda t, a: False)

    async def no(*a, **k):
        return False

    monkeypatch.setattr(hub, "_no_reply_needed", no)

    async def ctx(_t, _a, _c, data):
        return data.get("context")

    monkeypatch.setattr(hub, "_message_context", ctx)

    state = {"answered": set(), "runs": [], "gates": {}, "fail": set(), "delivery": []}

    class _Bus:
        async def set_delivery(self, conv_id, st, *, behind=None, position=None):
            state["delivery"].append((conv_id, st, behind, position))
            return {}

        async def get_conversation(self, conv_id):
            last = "etl-dev" if conv_id in state["answered"] else "cruscotto"
            return {"status": "open", "messages": [{"id": 1, "from_agent": last}]}

    monkeypatch.setattr(hub, "_client", lambda t, a: _Bus())

    async def fake_run(tenant, agent_id, ev_type, data, conv_id, project):
        state["runs"].append(conv_id)
        gate = state["gates"].get(conv_id)
        if gate is not None:
            await gate.wait()
        if conv_id in state["fail"]:
            raise RuntimeError("worker crashed")

    monkeypatch.setattr(hub, "_run_local_wake", fake_run)
    return state


def _ev(conv_id, msg_id=1):
    return {"conversation_id": conv_id, "message_id": msg_id,
            "from_agent": "cruscotto", "subject": "s", "body": "b"}


async def _settle():
    for _ in range(20):
        drainers = [t for t in hub._S["wake_drainers"].values() if not t.done()]
        if not drainers:
            return
        await asyncio.gather(*drainers, return_exceptions=True)


def test_second_conversation_runs_after_the_first(env):
    async def go():
        env["gates"][353] = asyncio.Event()
        first = asyncio.create_task(hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353)))
        await asyncio.sleep(0)
        await hub._wake_local_agent("acme", "etl-dev", "conversation_created", _ev(354))
        assert env["runs"] == [353]  # parked, not spawned concurrently
        assert hub_store.queued_wakes("acme", "etl-dev") == [354]
        env["gates"][353].set()
        await first
        await _settle()

    asyncio.run(go())
    assert env["runs"] == [353, 354]
    assert hub_store.queued_wakes("acme", "etl-dev") == []
    assert hub._S["inflight_wakes"] == {}


def test_queue_survives_a_failing_first_wake(env):
    env["fail"].add(353)

    async def go():
        env["gates"][353] = asyncio.Event()
        first = asyncio.create_task(hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353)))
        await asyncio.sleep(0)
        await hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(354))
        await hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(355))
        env["gates"][353].set()
        with pytest.raises(RuntimeError):
            await first
        await _settle()

    asyncio.run(go())
    assert env["runs"] == [353, 354, 355]
    # Network#35: the sender sees the queue, and it moving up.
    assert env["delivery"] == [
        (354, "queued", 353, 1),
        (354, "queued", 353, 1), (355, "queued", 354, 2),
        (354, "running", None, None),
        (355, "queued", 354, 1),
        (355, "running", None, None),
    ]


def test_same_message_twice_is_a_twin_but_a_newer_one_is_queued(env):
    async def go():
        env["gates"][353] = asyncio.Event()
        first = asyncio.create_task(hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353, 7)))
        await asyncio.sleep(0)
        await hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353, 7))
        assert hub_store.queued_wakes("acme", "etl-dev") == []  # SSE+webhook twin
        await hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353, 8))
        assert hub_store.queued_wakes("acme", "etl-dev") == [353]  # real follow-up
        env["gates"][353].set()
        await first
        await _settle()

    asyncio.run(go())
    assert env["runs"] == [353, 353]


def test_queued_conversation_already_answered_is_dropped(env):
    async def go():
        env["gates"][353] = asyncio.Event()
        first = asyncio.create_task(hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353)))
        await asyncio.sleep(0)
        await hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(354))
        env["answered"].add(354)  # e.g. an interactive session replied meanwhile
        env["gates"][353].set()
        await first
        await _settle()

    asyncio.run(go())
    assert env["runs"] == [353]
    assert env["delivery"][-1] == (354, "clear", None, None)


def test_delivery_failure_does_not_block_the_queue(env, monkeypatch):
    class _DeafBus:
        async def set_delivery(self, *a, **k):
            raise RuntimeError("404 on an old Network")

        async def get_conversation(self, conv_id):
            return {"status": "open", "messages": [{"id": 1, "from_agent": "cruscotto"}]}

    monkeypatch.setattr(hub, "_client", lambda t, a: _DeafBus())

    async def go():
        env["gates"][353] = asyncio.Event()
        first = asyncio.create_task(hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353)))
        await asyncio.sleep(0)
        await hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(354))
        env["gates"][353].set()
        await first
        await _settle()

    asyncio.run(go())
    assert env["runs"] == [353, 354]


def test_parked_wake_resumes_after_restart(env, monkeypatch):
    hub_store.enqueue_wake("acme", "etl-dev", "new_message", _ev(356))
    hub_store.enqueue_wake("acme", "other-node", "new_message", _ev(400))  # not ours
    monkeypatch.setattr(hub.wake_pool, "configure", lambda n: None)
    monkeypatch.setattr(hub_store, "purge_legacy_sessions", lambda: 0)
    monkeypatch.setattr(hub, "_listen", lambda t, a: asyncio.sleep(0))

    async def idle():
        return None

    monkeypatch.setattr(hub, "_status_report_loop", idle)
    monkeypatch.setattr(hub, "_check_local_roster", idle)
    monkeypatch.setattr(hub, "_recover_backlog", lambda t, a: idle())
    monkeypatch.setitem(hub._S, "local_tasks", [])

    async def go():
        hub.start_local_listeners()
        await _settle()

    asyncio.run(go())
    assert env["runs"] == [356]
    assert hub_store.queued_wakes("acme", "other-node") == [400]


def test_enqueue_dedups_per_conversation_keeping_the_newest_message(env):
    assert hub_store.enqueue_wake("acme", "etl-dev", "new_message", _ev(354, 3)) is True
    assert hub_store.enqueue_wake("acme", "etl-dev", "new_message", _ev(355, 1)) is True
    assert hub_store.enqueue_wake("acme", "etl-dev", "new_message", _ev(354, 5)) is False
    assert hub_store.enqueue_wake("acme", "etl-dev", "new_message", _ev(354, 4)) is False
    assert hub_store.queued_wakes("acme", "etl-dev") == [354, 355]
    assert hub_store.pop_wake("acme", "etl-dev")["data"]["message_id"] == 5
    assert hub_store.wake_queue_agents() == [("acme", "etl-dev")]


def test_bus_rebuild_clears_the_tenant_queue(env):
    hub_store.enqueue_wake("acme", "etl-dev", "new_message", _ev(354))
    hub_store.reset_bus_state("acme")
    assert hub_store.queued_wakes("acme", "etl-dev") == []


# --- #73: a wake killed mid-run by a container recreate is requeued at boot ---


def _boot(env, monkeypatch):
    monkeypatch.setattr(hub.wake_pool, "configure", lambda n: None)
    monkeypatch.setattr(hub_store, "purge_legacy_sessions", lambda: 0)
    monkeypatch.setattr(hub, "_listen", lambda t, a: asyncio.sleep(0))

    async def idle():
        return None

    monkeypatch.setattr(hub, "_status_report_loop", idle)
    monkeypatch.setattr(hub, "_check_local_roster", idle)
    monkeypatch.setattr(hub, "_recover_backlog", lambda t, a: idle())
    monkeypatch.setitem(hub._S, "local_tasks", [])

    async def go():
        hub.start_local_listeners()
        await _settle()

    asyncio.run(go())


def test_running_mark_is_set_during_the_run_and_cleared_after(env):
    seen = []

    async def go():
        env["gates"][353] = asyncio.Event()
        t = asyncio.create_task(hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353)))
        await asyncio.sleep(0)
        seen.extend(r["conv_id"] for r in hub_store.running_wakes())
        env["gates"][353].set()
        await t

    asyncio.run(go())
    assert seen == [353]
    assert hub_store.running_wakes() == []


def test_failing_run_clears_its_running_mark(env):
    env["fail"].add(353)
    with pytest.raises(RuntimeError):
        asyncio.run(hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353)))
    assert hub_store.running_wakes() == []


def test_interrupted_wake_runs_first_after_restart(env, monkeypatch):
    # The process died with #353 in flight and #356 parked behind it.
    hub_store.mark_wake_running("acme", "etl-dev", "new_message", _ev(353))
    hub_store.enqueue_wake("acme", "etl-dev", "new_message", _ev(356))
    _boot(env, monkeypatch)
    assert env["runs"] == [353, 356]
    assert hub_store.running_wakes() == []


def test_interrupted_wake_already_answered_is_dropped(env, monkeypatch):
    hub_store.mark_wake_running("acme", "etl-dev", "new_message", _ev(353))
    env["answered"].add(353)
    _boot(env, monkeypatch)
    assert env["runs"] == []


def test_requeue_keeps_the_newest_payload_of_the_same_conversation(env):
    hub_store.mark_wake_running("acme", "etl-dev", "new_message", _ev(353, 2))
    hub_store.enqueue_wake("acme", "etl-dev", "new_message", _ev(355))
    hub_store.enqueue_wake("acme", "etl-dev", "new_message", _ev(353, 7))
    assert hub_store.requeue_interrupted_wakes() == [("acme", "etl-dev", 353)]
    assert hub_store.queued_wakes("acme", "etl-dev") == [353, 355]
    assert hub_store.pop_wake("acme", "etl-dev")["data"]["message_id"] == 7
    assert hub_store.requeue_interrupted_wakes() == []


def test_requeue_happens_once_per_process(env, monkeypatch):
    _boot(env, monkeypatch)
    # A second start (ownership flip) must not requeue a run that is alive now.
    hub_store.mark_wake_running("acme", "etl-dev", "new_message", _ev(353))
    _boot(env, monkeypatch)
    assert [r["conv_id"] for r in hub_store.running_wakes()] == [353]


def test_bus_rebuild_clears_the_tenant_running_marks(env):
    hub_store.mark_wake_running("acme", "etl-dev", "new_message", _ev(353))
    hub_store.reset_bus_state("acme")
    assert hub_store.running_wakes() == []


def test_cancelled_run_keeps_its_running_mark(env):
    # SIGTERM on recreate cancels the run: the mark must survive for the next boot.
    async def go():
        env["gates"][353] = asyncio.Event()
        t = asyncio.create_task(hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(353)))
        await asyncio.sleep(0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(go())
    assert [r["conv_id"] for r in hub_store.running_wakes()] == [353]
