"""Issue #74: a skip for a live interactive session is not final. If the session
has not written its transcript since the message arrived, the worker is woken on
its own derived session; if it is working, it stays quiet. And the process-wide
wake bound covers the claude run only, so a skip never waits behind long runs."""

from __future__ import annotations

import asyncio

import pytest

import claude_runner
import hub
import hub_origin
import hub_store
import llm_usage
import projects
import wake_pool

INTERACTIVE = "1345854f-interactive"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(hub_store, "HUB_TOPICS_PATH", str(tmp_path / "hub_topics.json"))
    for key in ("inflight_wakes", "inflight_msgs", "wake_drainers", "clients",
                "idle_voice_rechecks"):
        monkeypatch.setitem(hub._S, key, {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"acme": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"offerte-dev": "offerte"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setattr(projects, "resolve_project",
                        lambda key: type("P", (), {"key": key, "workspace": "/ws"})())
    monkeypatch.setattr(hub_store, "is_watched", lambda t, a: False)
    monkeypatch.setattr(hub_store, "is_claimed", lambda t, a: False)
    monkeypatch.setattr(claude_runner, "host_exec_enabled", lambda: False)
    monkeypatch.setattr(llm_usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(hub, "IDLE_VOICE_RECHECK_S", 0.01)
    monkeypatch.setattr(hub, "IDLE_VOICE_MAX_RECHECKS", 3)

    async def no(*a, **k):
        return False

    monkeypatch.setattr(hub, "_no_reply_needed", no)

    async def ctx(_t, _a, _c, data):
        return data.get("context")

    monkeypatch.setattr(hub, "_message_context", ctx)

    state = {"runs": [], "awaiting": True, "last_write": None, "replied": False}

    # The interactive session on this host is alive: the voice rule says skip.
    async def origin_turn(_t, _a, _c):
        return hub_origin.Turn("skip", "origin session is alive; its loop takes the turn",
                               session_id=INTERACTIVE, cwd="/ws")

    monkeypatch.setattr(hub, "_origin_turn", origin_turn)

    async def still_awaiting(_t, _a, _c):
        return state["awaiting"] and not state["replied"]

    monkeypatch.setattr(hub, "_still_awaiting", still_awaiting)
    monkeypatch.setattr(hub_origin, "last_write", lambda _p, _sid: state["last_write"]())

    class _Bus:
        async def set_delivery(self, *a, **k):
            return {}

        async def get_conversation(self, conv_id):
            msgs = [{"id": 1, "from_agent": "ticket-manager"}]
            if state["replied"]:
                msgs.append({"id": 100, "from_agent": "offerte-dev", "body": "fatto"})
            return {"status": "open", "messages": msgs}

        async def send_message(self, *a, **k):
            return {}

    monkeypatch.setattr(hub, "_client", lambda t, a: _Bus())

    async def fake_run(prompt, *, workspace, persona, resume_id=None, extra_env=None, **kw):
        state["runs"].append({"resume": resume_id})
        state["replied"] = True
        return claude_runner.RunResult("ok", "headless-sid")

    monkeypatch.setitem(hub._S, "run_claude", fake_run)
    return state


def _ev(msg_id=1):
    return {"conversation_id": 496, "message_id": msg_id, "from_agent": "ticket-manager",
            "subject": "Ticket #60", "body": "diagnostica"}


async def _wake_and_settle(*events):
    for ev in events:
        await hub._wake_local_agent("acme", "offerte-dev", "new_message", ev)
    while hub._S["idle_voice_rechecks"]:
        await asyncio.gather(*hub._S["idle_voice_rechecks"].values(), return_exceptions=True)


def test_idle_interactive_session_wakes_the_worker_once(env):
    # Transcript last written long before the message: nobody is behind it.
    env["last_write"] = lambda: 1.0
    asyncio.run(_wake_and_settle(_ev()))
    assert len(env["runs"]) == 1
    # Its own derived session, never the interactive session's UUID.
    assert env["runs"][0]["resume"] != INTERACTIVE


def test_active_interactive_session_stays_the_only_voice(env):
    import time

    # Transcript keeps being written: the session is working, no twin.
    env["last_write"] = lambda: time.time() + 60
    asyncio.run(_wake_and_settle(_ev()))
    assert env["runs"] == []


def test_answered_meanwhile_wakes_nobody(env):
    env["last_write"] = lambda: 1.0
    env["awaiting"] = False
    asyncio.run(_wake_and_settle(_ev()))
    assert env["runs"] == []


def test_a_newer_message_replaces_the_pending_recheck(env):
    env["last_write"] = lambda: 1.0
    asyncio.run(_wake_and_settle(_ev(1), _ev(2)))
    assert len(env["runs"]) == 1


def test_skip_does_not_wait_for_busy_claude_slots(env, monkeypatch):
    """Three long runs hold every slot: an unrelated skip is still decided now."""
    monkeypatch.setattr(hub, "IDLE_VOICE_RECHECK_S", 0)  # no recheck in this test
    wake_pool.configure(1)

    decided = asyncio.Event()

    async def main():
        release = asyncio.Event()

        async def long_run():
            async with wake_pool.slot():
                await release.wait()

        hog = asyncio.create_task(long_run())
        await asyncio.sleep(0)

        orig = hub._dispatch

        async def dispatch(tenant, agent_id, ev_type, data):
            await hub._wake_local_agent(tenant, agent_id, ev_type, data)
            decided.set()

        monkeypatch.setattr(hub, "_dispatch", dispatch)
        tracker = wake_pool.Tracker(0, lambda c: None)
        tracker.on_received(1)
        await asyncio.wait_for(
            hub._run_event("acme", "offerte-dev", "new_message", _ev(), 1, tracker), 1.0
        )
        monkeypatch.setattr(hub, "_dispatch", orig)
        release.set()
        await hog

    asyncio.run(main())
    assert decided.is_set()
    assert env["runs"] == []
