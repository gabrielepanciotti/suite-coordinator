"""The warm live-session path must never override AI_BACKEND.

live_session.py speaks claude's stream-json protocol and spawns CLAUDE_BIN; it
has no opencode implementation. Turning CLAUDE_LIVE_SESSIONS on while
AI_BACKEND=opencode used to split the deployment in two without a word:
interactive Telegram turns on claude, wakes/notifiche/butler on opencode.
"""

import importlib

import pytest


def _reload_bot(monkeypatch, *, backend: str, live: str):
    monkeypatch.setenv("AI_BACKEND", backend)
    monkeypatch.setenv("CLAUDE_LIVE_SESSIONS", live)
    import claude_runner
    importlib.reload(claude_runner)
    import bot
    return importlib.reload(bot)


@pytest.fixture(autouse=True)
def _restore_modules():
    """Both modules read their env at import: put them back for everyone else.

    The env is restored here rather than left to ``monkeypatch``: its teardown
    is not guaranteed to run before this finalizer, and reloading claude_runner
    with AI_BACKEND still set to opencode leaks an opencode module into the rest
    of the suite (test_model_effort_wiring then asserts on opencode's argv).
    """
    import os
    saved = {k: os.environ.get(k) for k in ("AI_BACKEND", "CLAUDE_LIVE_SESSIONS")}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    import claude_runner
    importlib.reload(claude_runner)
    import bot
    importlib.reload(bot)


def test_live_sessions_stay_off_under_opencode(monkeypatch):
    bot = _reload_bot(monkeypatch, backend="opencode", live="1")
    assert bot.CLAUDE_LIVE_SESSIONS_REQUESTED is True   # the operator asked
    assert bot.CLAUDE_LIVE_SESSIONS is False            # the backend won


def test_live_sessions_work_on_a_claude_deployment(monkeypatch):
    bot = _reload_bot(monkeypatch, backend="claude", live="1")
    assert bot.CLAUDE_LIVE_SESSIONS is True


def test_default_is_off_on_both_backends(monkeypatch):
    assert _reload_bot(monkeypatch, backend="claude", live="0").CLAUDE_LIVE_SESSIONS is False
    assert _reload_bot(monkeypatch, backend="opencode", live="0").CLAUDE_LIVE_SESSIONS is False
