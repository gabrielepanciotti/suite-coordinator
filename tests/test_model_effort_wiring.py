"""The model/effort chosen in Telegram must reach the actual claude argv —
both on the cold path (run_claude) and on the warm one (LiveSessionManager)."""

import asyncio
import logging

import claude_runner
from live_session import LiveSessionManager


class _Boom(Exception):
    """Abort the spawn once argv has been captured — we only assert on argv."""


def _capture_argv(monkeypatch, module):
    seen = {}

    async def fake_exec(*cmd, **kwargs):
        seen["cmd"] = list(cmd)
        raise _Boom()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_exec)
    return seen


def _flags(cmd, flag):
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == flag and i + 1 < len(cmd)]


def test_run_claude_passes_model_and_effort(monkeypatch):
    seen = _capture_argv(monkeypatch, claude_runner)

    async def scenario():
        try:
            await claude_runner.run_claude(
                "ciao", workspace=".", persona=None, model="opus", effort="max"
            )
        except _Boom:
            pass

    asyncio.run(scenario())
    assert _flags(seen["cmd"], "--model") == ["opus"]
    assert _flags(seen["cmd"], "--effort") == ["max"]


def test_run_claude_without_overrides_keeps_env_default_and_omits_effort(monkeypatch):
    seen = _capture_argv(monkeypatch, claude_runner)

    async def scenario():
        try:
            await claude_runner.run_claude("ciao", workspace=".", persona=None)
        except _Boom:
            pass

    asyncio.run(scenario())
    assert _flags(seen["cmd"], "--model") == [claude_runner.CLAUDE_MODEL]
    assert _flags(seen["cmd"], "--effort") == []  # unset -> let the CLI decide


def _manager(**kw):
    defaults = dict(
        claude_bin="claude",
        model="sonnet",
        setting_sources=None,
        strict_mcp=False,
        idle_ttl_sec=60,
        max_timeout_sec=60,
        idle_event_timeout_sec=10,
        stream_limit=1024,
        logger=logging.getLogger("test"),
    )
    defaults.update(kw)
    return LiveSessionManager(**defaults)


def test_live_argv_overrides_manager_model_and_adds_effort():
    m = _manager()
    argv = m._argv(persona=None, resume_id=None, model="opus", effort="high")
    assert _flags(argv, "--model") == ["opus"]
    assert _flags(argv, "--effort") == ["high"]
    # no override -> the manager default, still no --effort
    argv = m._argv(persona=None, resume_id=None)
    assert _flags(argv, "--model") == ["sonnet"]
    assert _flags(argv, "--effort") == []


def test_live_respawns_when_overrides_change(monkeypatch):
    """A warm process spawned as sonnet cannot serve an opus turn: --model binds
    at spawn time, so the manager must retire it and resume the transcript."""
    m = _manager()
    spawned = []

    class _FakeProc:
        returncode = None

    class _FakeLive:
        def __init__(self, overrides, session_id):
            self.proc = _FakeProc()
            self.overrides = overrides
            self.session_id = session_id
            self.lock = asyncio.Lock()
            self.last_used = 0.0
            self.stderr_task = None

    async def fake_spawn(key, *, workspace, persona, resume_id, model=None, effort=None):
        spawned.append({"resume_id": resume_id, "model": model, "effort": effort})
        return _FakeLive((model, effort), session_id="sess-1")

    async def fake_kill(live):
        return None

    async def fake_pump(live, prompt, on_progress):
        # (text, session_id, usage): the third slot is the run's measured cost
        # (#28), None here because this test spawns no real process.
        return "ok", "sess-1", None

    monkeypatch.setattr(m, "_spawn", fake_spawn)
    monkeypatch.setattr(m, "_kill", fake_kill)
    monkeypatch.setattr(m, "_pump", fake_pump)

    async def scenario():
        key = (1, 2)
        common = dict(workspace=".", persona=None, resume_id=None)
        await m.run_turn(key, prompt="a", **common)
        # same overrides -> reuse, no new spawn
        await m.run_turn(key, prompt="b", **common)
        # different model -> respawn, resuming the session the first one built
        await m.run_turn(key, prompt="c", **common, model="opus", effort="max")

    asyncio.run(scenario())
    assert len(spawned) == 2, "second identical turn must reuse the warm process"
    assert spawned[1]["model"] == "opus" and spawned[1]["effort"] == "max"
    assert spawned[1]["resume_id"] == "sess-1", "context must carry over"
