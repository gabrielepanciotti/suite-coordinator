# tests/test_catch_up.py
"""Per-group catch-up window: a sleeping bot answers the night's messages."""
import datetime as dt
import textwrap
from types import SimpleNamespace

import bot
import manifest as M
import projects


def _ws(tmp_path, name: str, telegram: str):
    root = tmp_path / name
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "coordinator.toml").write_text(
        textwrap.dedent(telegram), encoding="utf-8"
    )
    return projects.Project(name, name, "\U0001F539", "", str(root))


def test_parse_catch_up_hours_values():
    assert M._parse_catch_up_hours(None) is None
    assert M._parse_catch_up_hours(True) is None
    assert M._parse_catch_up_hours("x") is None
    assert M._parse_catch_up_hours(0) is None
    assert M._parse_catch_up_hours(-3) is None
    assert M._parse_catch_up_hours(12) == 12.0
    assert M._parse_catch_up_hours("10.5") == 10.5
    assert M._parse_catch_up_hours(48) == M.CATCH_UP_MAX_HOURS


def test_catch_up_seconds_takes_the_widest_window_of_the_chat(monkeypatch, tmp_path):
    a = _ws(tmp_path, "a", "[telegram]\ngroup_chat_id = -100\ngroup_thread_id = 5\ncatch_up_hours = 12\n")
    b = _ws(tmp_path, "b", "[telegram]\ngroup_chat_id = -100\ngroup_thread_id = 6\ncatch_up_hours = 2\n")
    c = _ws(tmp_path, "c", "[telegram]\ngroup_chat_id = -200\n")
    monkeypatch.setattr(M.projects, "list_projects", lambda: [a, b, c])
    assert M.catch_up_seconds(-100) == 12 * 3600
    assert M.catch_up_seconds(-200) is None
    assert M.catch_up_seconds(-999) is None


def _msg(chat_id, age_sec):
    return SimpleNamespace(
        chat_id=chat_id,
        date=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_sec),
    )


def test_night_message_answered_only_where_catch_up_is_declared(monkeypatch):
    windows = {-100: 12 * 3600}
    monkeypatch.setattr(bot.manifest, "catch_up_seconds", lambda cid: windows.get(cid))
    night = 9 * 3600
    assert bot._is_stale(_msg(-100, night)) is False
    assert bot._is_stale(_msg(-200, night)) is True
    assert bot._is_stale(_msg(-100, 13 * 3600)) is True


def test_window_never_shrinks_the_global_limit(monkeypatch):
    monkeypatch.setattr(bot.manifest, "catch_up_seconds", lambda cid: 1)
    assert bot._is_stale(_msg(-100, bot.STALE_MAX_AGE_SEC - 5)) is False


def test_lookup_failure_falls_back_to_global_limit(monkeypatch):
    def boom(cid):
        raise RuntimeError("manifest unreadable")

    monkeypatch.setattr(bot.manifest, "catch_up_seconds", boom)
    assert bot._is_stale(_msg(-100, bot.STALE_MAX_AGE_SEC + 60)) is True
    assert bot._is_stale(_msg(-100, 30)) is False
