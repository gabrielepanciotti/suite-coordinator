"""State persistence: topic bindings + sessions survive a restart."""
import bot


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "STATE_PATH", str(tmp_path / "state.json"))
    bot.TOPIC_BINDING.clear()
    bot.SESSIONS.clear()

    bot._bind_topic(100, 5, "personalChef", "generale")
    bot._set_session(100, 5, "sid-1")

    # Simulate a restart: wipe memory, reload from disk.
    bot.TOPIC_BINDING.clear()
    bot.SESSIONS.clear()
    bot._load_state()

    assert bot.TOPIC_BINDING[(100, 5)] == ("personalChef", "generale")
    assert bot.SESSIONS[(100, 5)][0] == "sid-1"


def test_load_drops_bindings_for_unknown_projects(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text(
        '{"topics": {"1:2": ["ghost", "generale"]}, "sessions": {}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(bot, "STATE_PATH", str(p))
    bot.TOPIC_BINDING.clear()
    bot._load_state()
    assert (1, 2) not in bot.TOPIC_BINDING  # 'ghost' is not a real project


def test_save_is_skipped_when_data_dir_missing(monkeypatch):
    monkeypatch.setattr(bot, "STATE_PATH", "/no_such_dir_zzz/state.json")
    bot.TOPIC_BINDING.clear()
    # must not raise even though it can't persist
    bot._bind_topic(1, 2, "personalChef", "generale")
    assert bot.TOPIC_BINDING[(1, 2)] == ("personalChef", "generale")
