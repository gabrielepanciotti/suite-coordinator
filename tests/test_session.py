import datetime as dt

import bot


def setup_function():
    bot.SESSIONS.clear()


def test_set_and_get():
    bot._set_session(10, "toni", "sid-1")
    assert bot._get_session(10, "toni") == "sid-1"


def test_sessions_independent_per_agent():
    bot._set_session(10, "toni", "sid-toni")
    bot._set_session(10, "mycorise", "sid-myco")
    assert bot._get_session(10, "toni") == "sid-toni"
    assert bot._get_session(10, "mycorise") == "sid-myco"


def test_expired_session_returns_none_and_evicts():
    old = dt.datetime.now() - dt.timedelta(seconds=bot.SESSION_TTL_SEC + 60)
    bot.SESSIONS[(10, "toni")] = ("sid-old", old)
    assert bot._get_session(10, "toni") is None
    assert (10, "toni") not in bot.SESSIONS


def test_long_window_survives_gap(monkeypatch):
    # With a long TTL, a follow-up after a 90-min pause (e.g. a pressure-cooker
    # run) must still resume the same session instead of starting empty.
    monkeypatch.setattr(bot, "SESSION_TTL_SEC", 86400)
    ninety_min_ago = dt.datetime.now() - dt.timedelta(minutes=90)
    bot.SESSIONS[(10, "toni")] = ("sid-timer", ninety_min_ago)
    assert bot._get_session(10, "toni") == "sid-timer"
    assert (10, "toni") in bot.SESSIONS


def test_session_persists_round_trip(monkeypatch, tmp_path):
    # The (chat, topic) -> session_id map must survive a container restart:
    # _set_session writes state.json, _load_state restores it. Uses integer
    # topic ids because the on-disk key format is "chat:topic".
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(bot, "STATE_PATH", str(state_file))
    bot.TOPIC_BINDING.clear()
    bot._set_session(10, 7, "sid-persist")
    bot.SESSIONS.clear()  # simulate process restart (in-memory map gone)
    bot._load_state()
    assert bot._get_session(10, 7) == "sid-persist"


def test_get_missing_returns_none():
    assert bot._get_session(99, "toni") is None


def test_clear_session():
    bot._set_session(10, "toni", "sid-1")
    assert bot._clear_session(10, "toni") is True
    assert bot._clear_session(10, "toni") is False
