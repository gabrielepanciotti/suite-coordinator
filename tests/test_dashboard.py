"""Dashboard model: two-level (project, agent) topic binding + state migration,
and the callback_data codec used by the inline keyboard."""
import pytest

import bot


def setup_function():
    bot.TOPIC_BINDING.clear()
    bot.SESSIONS.clear()


# --- Two-level binding + state migration (Task 3) ----------------------------
def test_bind_and_resolve_two_level():
    bot._bind_topic(100, 5, "personalChef", "generale")
    proj, agent = bot._topic_binding(100, 5)
    assert proj.key == "personalChef" and agent.name == "generale"


def test_topic_binding_unbound_is_none():
    assert bot._topic_binding(100, 99) == (None, None)


def test_load_state_migrates_legacy_string_binding(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    # legacy shape: topics value was a bare agent key
    p.write_text('{"topics": {"1:2": "personalChef"}, "sessions": {}}', encoding="utf-8")
    monkeypatch.setattr(bot, "STATE_PATH", str(p))
    bot.TOPIC_BINDING.clear()
    bot._load_state()
    assert bot.TOPIC_BINDING[(1, 2)] == ("personalChef", "generale")


def test_state_roundtrip_two_level(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "STATE_PATH", str(tmp_path / "state.json"))
    bot.TOPIC_BINDING.clear()
    bot.SESSIONS.clear()
    bot._bind_topic(7, 3, "personalChef", "menu-planner")
    bot.TOPIC_BINDING.clear()
    bot._load_state()
    assert bot.TOPIC_BINDING[(7, 3)] == ("personalChef", "menu-planner")


def test_load_state_drops_unknown_project(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text(
        '{"topics": {"1:2": ["ghost", "generale"]}, "sessions": {}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(bot, "STATE_PATH", str(p))
    bot.TOPIC_BINDING.clear()
    bot._load_state()
    assert (1, 2) not in bot.TOPIC_BINDING  # 'ghost' is not a real project


# --- callback_data codec (Task 5) --------------------------------------------
def test_callback_codec_roundtrip():
    data = bot.pack_cb("act", "new", "cosmos", "ui-components")
    assert len(data) <= 64
    assert bot.parse_cb(data) == ["act", "new", "cosmos", "ui-components"]


def test_callback_codec_session_id_fits():
    sid = "6d485d50-369e-5bb2-a7c3-eb44c8a50ff4"
    data = bot.pack_cb("act", "res", "cosmos", sid)
    assert len(data) <= 64
    assert bot.parse_cb(data)[-1] == sid


def test_pack_cb_raises_on_oversized():
    with pytest.raises(ValueError):
        bot.pack_cb("act", "new", "scheduler", "a" * 80)


# --- _rel_time buckets (Task 9) ----------------------------------------------
def test_rel_time_buckets(monkeypatch):
    import time as _t
    now = 1_000_000.0
    monkeypatch.setattr(_t, "time", lambda: now)
    # bot imports `time` module; patch there:
    monkeypatch.setattr(bot.time, "time", lambda: now)
    assert bot._rel_time(now - 120) == "2m"
    assert bot._rel_time(now - 7200) == "2h"
    assert bot._rel_time(now - 172800) == "2g"
