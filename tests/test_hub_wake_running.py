"""Issue #70: a direct wake tells the bus it is running (and keeps telling it),
a timed-out attempt resumes the SAME session, a wake can ask for a longer
budget with ``context.max_minutes``, and a timeout is announced on the thread."""

from __future__ import annotations

import asyncio

import pytest

import claude_runner
import hub
import hub_store
import llm_usage
import projects


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(hub_store, "HUB_TOPICS_PATH", str(tmp_path / "hub_topics.json"))
    for key in ("inflight_wakes", "inflight_msgs", "wake_drainers", "clients"):
        monkeypatch.setitem(hub._S, key, {})
    cfg = hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"acme": "k"}, telegram_agent="g",
        telegram_chat_id=-100, local_agents={"etl-dev": "etl"}, turn_cap=12)
    monkeypatch.setitem(hub._S, "config", cfg)
    monkeypatch.setattr(projects, "resolve_project",
                        lambda key: type("P", (), {"key": key, "workspace": "/ws"})())
    monkeypatch.setattr(hub_store, "is_watched", lambda t, a: False)
    monkeypatch.setattr(hub_store, "is_claimed", lambda t, a: False)
    monkeypatch.setattr(claude_runner, "host_exec_enabled", lambda: False)
    monkeypatch.setattr(llm_usage, "record", lambda *a, **k: None)

    async def no(*a, **k):
        return False

    monkeypatch.setattr(hub, "_no_reply_needed", no)

    async def ctx(_t, _a, _c, data):
        return data.get("context")

    monkeypatch.setattr(hub, "_message_context", ctx)

    state = {"delivery": [], "posted": [], "runs": [], "replies": [], "script": []}

    class _Bus:
        async def set_delivery(self, conv_id, st, *, behind=None, position=None):
            state["delivery"].append((conv_id, st))
            return {}

        async def get_conversation(self, conv_id):
            msgs = [{"id": 1, "from_agent": "cruscotto"}]
            msgs += [{"id": 10 + i, "from_agent": a, "body": b}
                     for i, (a, b) in enumerate(state["posted"] + state["replies"])]
            return {"status": "open", "messages": msgs}

        async def send_message(self, conv_id, body, needs_human=False, **kw):
            state["posted"].append(("etl-dev", body))
            return {}

    monkeypatch.setattr(hub, "_client", lambda t, a: _Bus())

    async def fake_run(prompt, *, workspace, persona, resume_id=None, extra_env=None, **kw):
        state["runs"].append({"prompt": prompt, "resume": resume_id, **kw})
        # The heartbeat has already said "running" when claude starts.
        state.setdefault("delivery_at_start", list(state["delivery"]))
        outcome = state["script"].pop(0)
        if outcome == "timeout":
            usage = llm_usage.RunUsage(
                engine="claude", model="m", outcome=claude_runner.OUTCOME_TIMEOUT,
                started_at=None, duration_ms=1, cost_usd=None)
            return claude_runner.RunResult("⏱ Interrotto: superato il limite", "sid-1", usage)
        if outcome == "reply":
            state["replies"].append(("etl-dev", "fatto"))
        return claude_runner.RunResult("ok", "sid-1")

    monkeypatch.setitem(hub._S, "run_claude", fake_run)
    return state


def _ev(context=None):
    ev = {"conversation_id": 375, "message_id": 1, "from_agent": "cruscotto",
          "subject": "s", "body": "fai OutBound#50"}
    if context is not None:
        ev["context"] = context
    return ev


def _wake(context=None):
    asyncio.run(hub._wake_local_agent("acme", "etl-dev", "new_message", _ev(context)))


def test_direct_wake_publishes_running_then_clears(env):
    env["script"] = ["reply"]
    _wake()
    assert env["delivery_at_start"] == [(375, "running")]
    assert env["delivery"][-1] == (375, "clear")


def test_running_is_refreshed_while_the_worker_is_alive(env, monkeypatch):
    monkeypatch.setattr(hub, "WAKE_RUNNING_REFRESH_S", 0.01)

    async def slow_run(prompt, *, workspace, persona, resume_id=None, extra_env=None, **kw):
        await asyncio.sleep(0.08)
        env["replies"].append(("etl-dev", "fatto"))
        return "ok", "sid-1"

    monkeypatch.setitem(hub._S, "run_claude", slow_run)
    _wake()
    running = [d for d in env["delivery"] if d == (375, "running")]
    assert len(running) >= 3
    assert env["delivery"][-1] == (375, "clear")


def test_running_is_cleared_even_when_the_run_crashes(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("cli exploded")

    monkeypatch.setitem(hub._S, "run_claude", boom)
    with pytest.raises(RuntimeError):
        _wake()
    assert env["delivery"][-1] == (375, "clear")


def test_timeout_resumes_the_same_session_and_says_so(env):
    env["script"] = ["timeout", "reply"]
    _wake()
    first, second = env["runs"]
    assert second["resume"] == "sid-1"
    assert "interrotto per tempo" in second["prompt"]
    assert "git status" in second["prompt"]
    notices = [b for _, b in env["posted"]]
    assert len(notices) == 1
    assert notices[0].startswith(hub.AUTO_NOTICE_TAG)
    assert "riprendo" in notices[0]
    # The notice clears "running" on the bus: it is said again right after.
    assert env["delivery"].count((375, "running")) >= 2
    assert not (hub_store.get_conv(375) or {}).get("wake_escalated")


def test_timeout_notice_is_not_counted_as_the_answer(env):
    env["script"] = ["timeout", "silent"]
    _wake()
    # Two attempts, neither answered: escalation, despite our own notice on the thread.
    assert len(env["runs"]) == 2
    assert hub_store.wake_escalated_at(375) == 1
    assert "interrotto per tempo" in env["posted"][-1][1]


def test_non_timeout_retry_still_starts_fresh(env):
    env["script"] = ["silent", "reply"]
    _wake()
    assert env["runs"][1]["resume"] is None
    assert env["posted"] == []


def test_max_minutes_sets_the_budget_with_a_ceiling(env, monkeypatch):
    monkeypatch.setattr(hub, "WAKE_MAX_MINUTES_CAP", 180)
    env["script"] = ["reply"]
    _wake({"max_minutes": 120})
    assert env["runs"][0]["max_timeout"] == 7200

    env["script"] = ["reply"]
    env["runs"].clear()
    env["replies"].clear()
    _wake({"max_minutes": 999})
    assert env["runs"][0]["max_timeout"] == 180 * 60


def test_no_max_minutes_keeps_the_default(env):
    env["script"] = ["reply"]
    _wake()
    assert "max_timeout" not in env["runs"][0]


@pytest.mark.parametrize("raw, expected", [
    (None, None), ("abc", None), (0, None), (-5, None), (True, None),
    (90, 5400), ("30", 1800), (10_000, hub.WAKE_MAX_MINUTES_CAP * 60),
])
def test_wake_max_seconds(raw, expected):
    assert hub.wake_max_seconds({"max_minutes": raw}) == expected


def test_runner_honours_a_per_call_max_timeout():
    import inspect
    for fn in (claude_runner.run_claude, claude_runner._run_claude_cli,
               claude_runner._run_opencode):
        assert "max_timeout" in inspect.signature(fn).parameters
