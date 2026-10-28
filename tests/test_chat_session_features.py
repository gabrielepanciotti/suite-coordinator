"""Per-chat conversation behaviour of project groups/DMs (2026-10-09).

Day-scoped sessions, reply-to context, chat log, menu-driven session resets,
drill-down menus with a sticky mode, forum topic names, /argomenti and the DM
default project of a profile.
"""
import asyncio
import datetime as dt
import json
import textwrap
import types

import pytest

import bot
import chat_session
import group_menu
import manifest
import profiles
import projects
from manifest import MenuItem

AA_CHAT = -1009990077


# --- manifest ------------------------------------------------------------------
def _ws(tmp_path, toml: str):
    d = tmp_path / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / "coordinator.toml").write_text(textwrap.dedent(toml), encoding="utf-8")
    return projects.Project("x", "X", "\U0001F539", "", str(tmp_path))


def test_manifest_parses_the_new_telegram_keys(tmp_path):
    p = _ws(tmp_path, """
        [telegram]
        group_chat_id = -100
        session_scope = "day"
        menu_resets_session = true
        menu_autohide = true
        chat_log = "brain/chat"
        topics = ["Generale", "Pittura I", "Pittura I", ""]

        [[menu]]
        key = "interroga"
        label = "🎓 Interrogami"
        cmd = "/interroga"
        source = "sh scripts/pyrun scripts/menu_corsi.py {path}"
    """)
    manifest._CACHE.clear()
    tg = manifest.telegram_of(p)
    assert tg.session_scope == "day"
    assert tg.menu_resets_session is True
    assert tg.menu_autohide is True
    assert tg.chat_log == "brain/chat"
    assert tg.topics == ("Generale", "Pittura I")
    assert manifest.menu_of(p)[0].source.endswith("{path}")


def test_manifest_defaults_keep_todays_behaviour(tmp_path):
    p = _ws(tmp_path, """
        [telegram]
        group_chat_id = -100
    """)
    manifest._CACHE.clear()
    tg = manifest.telegram_of(p)
    assert tg.session_scope is None
    assert not tg.menu_resets_session and not tg.menu_autohide
    assert tg.chat_log is None and tg.topics == ()


@pytest.mark.parametrize("raw", ["/etc/x", "../escape", "a/../../b", "C:/x"])
def test_chat_log_rejects_paths_outside_the_workspace(tmp_path, raw):
    p = _ws(tmp_path, f"""
        [telegram]
        chat_log = "{raw}"
        session_scope = "week"
    """)
    manifest._CACHE.clear()
    tg = manifest.telegram_of(p)
    assert tg.chat_log is None
    assert tg.session_scope is None


def test_menu_source_needs_path_placeholder_and_cmd(tmp_path):
    p = _ws(tmp_path, """
        [[menu]]
        key = "a"
        label = "A"
        hint = "ciao"
        source = "sh x.sh {path}"

        [[menu]]
        key = "b"
        label = "B"
        cmd = "/b"
        source = "sh x.sh"
    """)
    manifest._CACHE.clear()
    assert [it.source for it in manifest.menu_of(p)] == ["", ""]


# --- day-scoped sessions -----------------------------------------------------------
ROME = "Europe/Rome"


def _rome(y, m, d, h, mi=0):
    from zoneinfo import ZoneInfo

    return dt.datetime(y, m, d, h, mi, tzinfo=ZoneInfo(ROME))


def test_day_scope_expires_at_local_midnight():
    last = _rome(2026, 10, 9, 23, 50)
    assert not chat_session.session_expired(
        last, _rome(2026, 10, 9, 23, 59), ttl_sec=86400, scope="day", tz_name=ROME)
    assert chat_session.session_expired(
        last, _rome(2026, 10, 10, 0, 5), ttl_sec=86400, scope="day", tz_name=ROME)
    # Without the scope only the idle TTL counts.
    assert not chat_session.session_expired(
        last, _rome(2026, 10, 10, 0, 5), ttl_sec=86400, scope=None, tz_name=ROME)
    assert chat_session.session_expired(
        last, _rome(2026, 10, 9, 23, 55), ttl_sec=60, scope=None, tz_name=ROME)


def test_get_session_honours_day_scope(monkeypatch):
    key = (1, 0)
    yesterday = dt.datetime.now() - dt.timedelta(days=1, minutes=1)
    monkeypatch.setitem(bot.SESSIONS, key, ("ses_old", yesterday))
    monkeypatch.setattr(bot, "SESSION_TTL_SEC", 10 * 86400)
    assert bot._get_session(1, 0) == "ses_old"
    assert bot._get_session(1, 0, scope="day") is None
    assert key not in bot.SESSIONS


# --- reply-to and topic prefix -----------------------------------------------------
def _m(text=None, caption=None, user=None):
    return types.SimpleNamespace(text=text, caption=caption, from_user=user)


def test_reply_to_line():
    human = types.SimpleNamespace(is_bot=False, first_name="a member", username=None, id=7)
    botu = types.SimpleNamespace(is_bot=True, first_name="Coord", username=None, id=8)
    assert chat_session.reply_to_line(None) is None
    assert chat_session.reply_to_line(_m()) is None  # e.g. forum_topic_created
    assert chat_session.reply_to_line(_m("ciao", user=human)) == "In risposta a (a member): «ciao»"
    assert chat_session.reply_to_line(_m(caption="foto", user=botu)) == (
        "In risposta a (bot): «foto»")
    long = chat_session.reply_to_line(_m("x" * 2000, user=human))
    assert long.endswith("…»") and len(long) < 1600


def test_group_message_prompt_carries_reply_to():
    p = bot._group_message_prompt("a member", 7, "e questa?", None, False,
                                  reply_to="In risposta a (bot): «Domanda 1»")
    assert p.startswith("[CMD:/group-message] mittente: a member (tg_id: 7).")
    assert "In risposta a (bot): «Domanda 1»" in p
    assert bot._group_message_prompt("a member", 7, "x", None, False) == (
        "[CMD:/group-message] mittente: a member (tg_id: 7). Testo: x")


def test_topic_prefix_and_names_parser():
    assert chat_session.topic_prefix("Pittura I") == "[Argomento: Pittura I]"
    assert chat_session.topic_prefix(None) == ""
    assert chat_session.parse_topic_names(" Pittura I | Storia | | Storia ") == [
        "Pittura I", "Storia"]
    assert chat_session.parse_topic_names("") == []


def test_topic_names_are_learnt_from_service_messages(monkeypatch):
    monkeypatch.setattr(bot, "TOPIC_NAMES", {})
    created = types.SimpleNamespace(name="Pittura I")
    root = types.SimpleNamespace(forum_topic_created=created, forum_topic_edited=None)
    msg = types.SimpleNamespace(chat=types.SimpleNamespace(id=AA_CHAT), message_thread_id=42,
                                forum_topic_created=None, forum_topic_edited=None,
                                reply_to_message=root)
    bot._learn_topic_name_from(msg)
    assert bot.TOPIC_NAMES[(AA_CHAT, 42)] == "Pittura I"
    edited = types.SimpleNamespace(chat=types.SimpleNamespace(id=AA_CHAT), message_thread_id=42,
                                   forum_topic_edited=types.SimpleNamespace(name="Pittura 1"),
                                   forum_topic_created=None, reply_to_message=None)
    bot._learn_topic_name_from(edited)
    assert bot.TOPIC_NAMES[(AA_CHAT, 42)] == "Pittura 1"


# --- chat log --------------------------------------------------------------------
def test_chat_log_appends_with_daily_header(tmp_path):
    when = dt.datetime(2026, 10, 9, 14, 5)
    path = chat_session.append_chat_log(
        str(tmp_path), "brain/chat", when=when, sender="a member", user_text="ciao",
        reply_text="ciao a member", reply_to="In risposta a (bot): «x»")
    chat_session.append_chat_log(
        str(tmp_path), "brain/chat", when=when, sender="a member", user_text="ancora",
        reply_text="ok")
    text = (tmp_path / "brain" / "chat" / "2026-10-09.md").read_text(encoding="utf-8")
    assert path.endswith("2026-10-09.md")
    assert text.count("# Conversazioni 2026-10-09") == 1
    assert "### 14:05 — a member" in text and "**Tutor:**" in text
    assert "In risposta a (bot): «x»" in text and "ancora" in text


def test_chat_log_never_raises(tmp_path):
    blocker = tmp_path / "brain"
    blocker.write_text("a file, not a dir", encoding="utf-8")
    assert chat_session.append_chat_log(
        str(tmp_path), "brain/chat", when=dt.datetime(2026, 10, 9), sender="x",
        user_text="a", reply_text="b") is None


# --- profile DM project ---------------------------------------------------------------
def test_dm_project_for_profile(monkeypatch):
    member = next(p for p in profiles.PROFILES if p.key == "member")
    monkeypatch.delenv("PROFILE_MEMBER_DM_PROJECT", raising=False)
    assert profiles.dm_project_for(member) is None
    monkeypatch.setenv("PROFILE_MEMBER_DM_PROJECT", "academicAssistant")
    assert profiles.dm_project_for(member) == "academicAssistant"
    monkeypatch.setenv("PROFILE_MEMBER_DM_PROJECT", "offerte")  # not visible to member
    assert profiles.dm_project_for(member) is None


# --- drill-down: pure parts -----------------------------------------------------------
def test_parse_level_validates_json():
    ok = group_menu.parse_level(json.dumps({
        "title": "Corsi",
        "items": [{"label": "🟢 Storia · in corso", "path": "storia", "leaf": False,
                   "name": "Storia"},
                  {"label": "Pittura", "path": "pittura", "leaf": True}],
        "all_label": "📚 Tutti i corsi",
    }))
    assert ok["items"][0]["name"] == "Storia"
    assert ok["items"][1]["name"] == "Pittura" and ok["items"][1]["leaf"] is True
    assert ok["all_label"] == "📚 Tutti i corsi"
    assert group_menu.parse_level("not json") is None
    assert group_menu.parse_level(json.dumps({"items": "x"})) is None
    assert group_menu.parse_level(json.dumps({"items": [{"path": "a"}]})) is None
    assert group_menu.parse_level(json.dumps({"items": []}))["all_label"] is None


def test_page_bounds():
    assert group_menu.page_bounds(0, 0) == (0, 0, 0, 1)
    assert group_menu.page_bounds(14, 0) == (0, 6, 0, 3)
    assert group_menu.page_bounds(14, 2) == (12, 14, 2, 3)
    assert group_menu.page_bounds(14, 9) == (12, 14, 2, 3)  # clamped
    assert group_menu.page_bounds(14, -1) == (0, 6, 0, 3)


def test_callback_registry_ids_are_short_and_bounded():
    reg = chat_session.CallbackRegistry(cap=3)
    ids = [reg.register({"n": i}) for i in range(5)]
    assert len(set(ids)) == 5
    assert reg.get(ids[0]) is None and reg.get(ids[-1]) == {"n": 4}
    assert all(len(group_menu.DRILL_PREFIX + i) <= 64 for i in ids)


def test_drill_keyboard_layout():
    level = {"title": "", "all_label": "📚 Tutti",
             "items": [{"label": f"C{i}", "path": f"p{i}", "leaf": i % 2 == 0, "name": f"C{i}"}
                       for i in range(8)]}
    reg = chat_session.CallbackRegistry()
    kb = group_menu.build_drill_keyboard(level, 1, (("x", "X"),), reg.register)
    rows = kb.inline_keyboard
    assert rows[0][0].text == "📚 Tutti"
    assert [r[0].text for r in rows[1:3]] == ["C6", "C7"]
    assert [b.text for b in rows[3]] == ["◀️", "2/2"]
    assert rows[-1][0].text == group_menu.BACK_LABEL
    sel = reg.get(rows[1][0].callback_data[3:])
    assert sel["kind"] == "sel" and sel["trail"] == (("x", "X"), ("p6", "C6"))
    back = reg.get(rows[-1][0].callback_data[3:])
    assert back == {"kind": "nav", "trail": (), "page": 0}


def test_breadcrumb_and_clean_label():
    assert group_menu.breadcrumb(["Storia", "Barocco"]) == "Storia › Barocco"
    assert group_menu.clean_label("📚 Tutti i corsi") == "tutti i corsi"
    assert group_menu.mode_prefix("🎓 Interrogami", "Storia") == (
        "[Modalità: 🎓 Interrogami — Storia]")


# --- bot wiring: menu reset, drill-down, mode --------------------------------------------
class _Msg:
    def __init__(self, text="/menu", thread=None):
        self.text = text
        self.caption = None
        self.message_thread_id = thread
        self.reply_to_message = None
        self.replies: list = []
        self.edits: list = []
        self.deleted = False
        self.message_id = 1

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))

    async def delete(self):
        self.deleted = True


class _Query:
    def __init__(self, data, message):
        self.data = data
        self.message = message

    async def answer(self, *a, **kw):
        pass


def _upd(msg, query=None, chat_type="supergroup"):
    return types.SimpleNamespace(
        effective_message=msg,
        effective_chat=types.SimpleNamespace(id=AA_CHAT, type=chat_type),
        effective_user=types.SimpleNamespace(id=555, first_name="a member", username=None),
        callback_query=query,
    )


ITEMS = [
    MenuItem("oggi", "📅 Oggi", "/oggi", ""),
    MenuItem("interroga", "🎓 Interrogami", "/interroga", "Su cosa?",
             "sh menu.sh {path}"),
]
CAPS = [
    manifest.Capability("oggi", "Oggi", "[CMD:/oggi]", "both"),
    manifest.Capability("interroga", "Interrogami", "[CMD:/interroga]", "both"),
]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    aa = projects.Project("academicAssistant", "AA", "\U0001F393", "", str(tmp_path))
    tg = manifest.TelegramConf(group_chat_id=AA_CHAT, agent="tutor", menu_resets_session=True,
                               menu_autohide=True, session_scope="day")
    man = manifest.Manifest(telegram=tg, capabilities=CAPS, menu=ITEMS)
    monkeypatch.setattr(manifest, "load", lambda p: man)
    monkeypatch.setattr(projects, "list_projects", lambda: [aa])
    monkeypatch.setattr(bot, "resolve_project", lambda k: aa if k == aa.key else None)
    monkeypatch.setattr(bot, "_profile_or_guest", lambda u: types.SimpleNamespace(is_admin=False))
    monkeypatch.setattr(bot, "_profile", lambda u: types.SimpleNamespace(is_admin=False))
    monkeypatch.setattr(bot, "_served_by_another_bot", lambda p, b=None: None)
    monkeypatch.setattr(bot, "_current_bot_name", lambda c: "default")
    monkeypatch.setattr(bot, "SESSIONS", {})
    monkeypatch.setattr(bot, "_LAST_MENU_KEY", {})
    monkeypatch.setattr(bot, "TOPIC_MODES", {})
    ran = []

    async def fake_run(update, project_key, cap_key, args=None):
        ran.append((project_key, cap_key, args))

    monkeypatch.setattr(bot, "_run_capability", fake_run)
    levels = {
        "": {"title": "", "all_label": "📚 Tutti i corsi",
             "items": [{"label": "🟢 Storia · in corso", "path": "storia", "leaf": False,
                        "name": "Storia"}]},
        "storia": {"title": "Storia", "all_label": "Tutto Storia",
                   "items": [{"label": "Barocco", "path": "storia/barocco", "leaf": True,
                              "name": "Barocco"}]},
    }

    async def fake_source(project, item, path):
        return levels.get(path)

    monkeypatch.setattr(bot, "_run_menu_source", fake_source)
    return types.SimpleNamespace(ran=ran, aa=aa)


def _tap(data, msg=None):
    msg = msg or _Msg()
    q = _Query(data, msg)
    asyncio.run(bot.on_group_menu_callback(_upd(msg, q), None))
    return msg


def test_switching_menu_key_resets_the_session(wired):
    bot.SESSIONS[(AA_CHAT, 0)] = ("ses_a", dt.datetime.now())
    _tap("gm:oggi")
    assert (AA_CHAT, 0) not in bot.SESSIONS  # first tap: new activity
    bot.SESSIONS[(AA_CHAT, 0)] = ("ses_b", dt.datetime.now())
    m = _tap("gm:oggi")
    assert bot.SESSIONS[(AA_CHAT, 0)][0] == "ses_b"  # same key: kept
    assert m.deleted  # menu_autohide
    assert wired.ran[-1] == ("academicAssistant", "oggi", "")


def test_drill_down_select_sets_mode_and_runs(wired):
    m = _tap("gm:interroga")
    text, kw = m.replies[-1]
    assert text == "🎓 Interrogami — scegli"
    kb = kw["reply_markup"].inline_keyboard
    assert kb[0][0].text == "📚 Tutti i corsi"
    # drill into "Storia"
    nav = _Query(kb[1][0].callback_data, m)
    asyncio.run(bot.on_drill_callback(_upd(m, nav), None))
    text, kw = m.edits[-1]
    assert text == "🎓 Interrogami — Storia"
    rows = kw["reply_markup"].inline_keyboard
    assert rows[-1][0].text == group_menu.BACK_LABEL
    leaf = _Query(rows[1][0].callback_data, m)
    asyncio.run(bot.on_drill_callback(_upd(m, leaf), None))
    assert m.edits[-1][0] == "🎓 Interrogami — Storia › Barocco"
    assert wired.ran[-1] == ("academicAssistant", "interroga", "Storia › Barocco")
    mode = bot.TOPIC_MODES[(AA_CHAT, 0)]
    assert mode["breadcrumb"] == "Storia › Barocco" and mode["path"] == "storia/barocco"
    assert bot._mode_prefix(AA_CHAT, 0, wired.aa) == (
        "[Modalità: 🎓 Interrogami — Storia › Barocco]")


def test_all_label_at_root_means_everything(wired):
    m = _tap("gm:interroga")
    kb = m.replies[-1][1]["reply_markup"].inline_keyboard
    asyncio.run(bot.on_drill_callback(_upd(m, _Query(kb[0][0].callback_data, m)), None))
    assert wired.ran[-1] == ("academicAssistant", "interroga", "tutti i corsi")


def test_mode_cleared_by_menu_tap_and_by_day_change(wired, monkeypatch):
    bot.TOPIC_MODES[(AA_CHAT, 0)] = {"project": "academicAssistant", "label": "L",
                                     "breadcrumb": "B", "day": "2000-01-01"}
    assert bot._mode_prefix(AA_CHAT, 0, wired.aa) is None  # earlier day, scope=day
    assert (AA_CHAT, 0) not in bot.TOPIC_MODES
    bot._set_mode(AA_CHAT, 0, {"project": "academicAssistant", "label": "L", "breadcrumb": "B"})
    assert bot._mode_prefix(AA_CHAT, 0, wired.aa) == "[Modalità: L — B]"
    _tap("gm:oggi")
    assert (AA_CHAT, 0) not in bot.TOPIC_MODES


def test_source_failure_falls_back_to_the_hint(wired, monkeypatch):
    async def broken(project, item, path):
        return None

    monkeypatch.setattr(bot, "_run_menu_source", broken)
    m = _tap("gm:interroga")
    assert "Su cosa?" in m.replies[-1][0]


def test_new_command_resets_session_and_mode(wired):
    bot.SESSIONS[(AA_CHAT, 0)] = ("s", dt.datetime.now())
    bot.TOPIC_MODES[(AA_CHAT, 0)] = {"project": "academicAssistant"}
    m = _Msg("/new")
    asyncio.run(bot.cmd_new(_upd(m), None))
    assert m.replies[-1][0] == "🆕 Nuova conversazione."
    assert not bot.SESSIONS and not bot.TOPIC_MODES


# --- /argomenti -------------------------------------------------------------------------
class _TgBot:
    def __init__(self):
        self.created, self.sent = [], []

    async def create_forum_topic(self, chat_id, name):
        self.created.append(name)
        return types.SimpleNamespace(message_thread_id=100 + len(self.created))

    async def send_message(self, chat_id, text, message_thread_id=None, **kw):
        self.sent.append((message_thread_id, text))


def test_argomenti_creates_missing_topics(wired, monkeypatch):
    monkeypatch.setattr(bot, "_is_admin", lambda u: True)
    monkeypatch.setattr(bot, "TOPIC_NAMES", {(AA_CHAT, 7): "Storia"})
    monkeypatch.setattr(bot, "TOPIC_BINDING", {(AA_CHAT, 0): ("academicAssistant", "tutor")})
    tgbot = _TgBot()
    m = _Msg("/argomenti Storia | Pittura I")
    upd = _upd(m)
    upd.effective_chat.is_forum = True
    asyncio.run(bot.cmd_argomenti(upd, types.SimpleNamespace(bot=tgbot)))
    assert tgbot.created == ["Pittura I"]
    assert bot.TOPIC_BINDING[(AA_CHAT, 101)] == ("academicAssistant", "tutor")
    assert bot.TOPIC_NAMES[(AA_CHAT, 101)] == "Pittura I"
    assert tgbot.sent[0][1].startswith("Qui parliamo di Pittura I.")
    assert "Già presenti: Storia" in m.replies[-1][0]


def test_argomenti_is_admin_only(wired, monkeypatch):
    monkeypatch.setattr(bot, "_is_admin", lambda u: False)
    tgbot = _TgBot()
    m = _Msg("/argomenti A")
    asyncio.run(bot.cmd_argomenti(_upd(m), types.SimpleNamespace(bot=tgbot)))
    assert tgbot.created == [] and m.replies == []


# --- engine errors in a project group; DM autobind -----------------------------------------
def _stub_turn(monkeypatch, reply):
    seen = {}

    async def fake_turn(chat_id, topic_id, project, agent, prompt, on_progress, prefs=None):
        seen["prompt"] = prompt
        return reply

    async def fake_finish(thinking, text, send_long, reply_markup=None):
        seen["sent"] = text

    async def noop(*a, **k):
        return None

    owner = []

    async def fake_owner(tg_bot, text):
        owner.append(text)

    monkeypatch.setattr(bot, "_claude_turn", fake_turn)
    monkeypatch.setattr(bot, "_finish_turn", fake_finish)
    monkeypatch.setattr(bot, "_react", noop)
    monkeypatch.setattr(bot, "_send_outbox", noop)
    monkeypatch.setattr(bot, "_notify_owner", fake_owner)
    return seen, owner


def _turn_update(msg, chat_type="supergroup"):
    upd = _upd(msg, chat_type=chat_type)

    class _B:
        async def send_chat_action(self, *a, **k):
            pass

    upd.get_bot = lambda: _B()
    return upd


def test_engine_error_is_masked_in_the_project_group(wired, monkeypatch):
    seen, owner = _stub_turn(monkeypatch, "❌ opencode: Unexpected server error.")
    m = _Msg("ciao")
    m.reply_to_message = None
    asyncio.run(bot._invoke_and_reply(_turn_update(m), wired.aa, projects.GENERALE, "ciao",
                                      context_prefix="In risposta a (bot): «x»"))
    assert seen["sent"] == bot.ENGINE_ERROR_GROUP_MSG
    assert owner and "Unexpected server error" in owner[0]
    assert seen["prompt"] == "In risposta a (bot): «x»\nciao"


def test_turn_timeout_is_masked_for_non_owner_in_group(wired, monkeypatch):
    seen, owner = _stub_turn(monkeypatch, "⏱ Nessuna attività per 900s: probabile blocco")
    m = _Msg("ciao")
    m.reply_to_message = None
    asyncio.run(bot._invoke_and_reply(_turn_update(m), wired.aa, projects.GENERALE, "ciao"))
    assert seen["sent"] == bot.ENGINE_ERROR_GROUP_MSG
    assert owner and "Nessuna attività per 900s" in owner[0]


def test_turn_error_stays_raw_for_the_owner_in_group(wired, monkeypatch):
    seen, owner = _stub_turn(monkeypatch, "❌ opencode exit=1 database is locked")
    monkeypatch.setattr(bot, "_profile", lambda u: types.SimpleNamespace(key="owner", is_admin=True))
    m = _Msg("ciao")
    m.reply_to_message = None
    asyncio.run(bot._invoke_and_reply(_turn_update(m), wired.aa, projects.GENERALE, "ciao"))
    assert seen["sent"] == "❌ opencode exit=1 database is locked" and owner == []


def test_engine_error_stays_raw_in_private(wired, monkeypatch):
    seen, owner = _stub_turn(monkeypatch, "❌ opencode: boom")
    m = _Msg("ciao")
    asyncio.run(bot._invoke_and_reply(_turn_update(m, "private"), wired.aa,
                                      projects.GENERALE, "ciao"))
    assert seen["sent"] == "❌ opencode: boom" and owner == []


def test_topic_and_mode_prefix_reach_the_prompt(wired, monkeypatch):
    seen, _ = _stub_turn(monkeypatch, "ok")
    monkeypatch.setattr(bot, "TOPIC_NAMES", {(AA_CHAT, 42): "Pittura I"})
    bot._set_mode(AA_CHAT, 42, {"project": "academicAssistant", "label": "🎓 Interrogami",
                                "breadcrumb": "Pittura I"})
    m = _Msg("e il colore?", thread=42)
    asyncio.run(bot._invoke_and_reply(_turn_update(m), wired.aa, projects.GENERALE,
                                      "e il colore?"))
    assert seen["prompt"] == (
        "[Argomento: Pittura I]\n[Modalità: 🎓 Interrogami — Pittura I]\ne il colore?")


def test_dm_autobinds_to_profile_project_and_shows_its_menu(wired, monkeypatch):
    monkeypatch.setattr(bot, "TOPIC_BINDING", {})
    monkeypatch.setattr(bot, "_dm_project_for", lambda u: wired.aa)
    monkeypatch.setattr(bot, "resolve_agent", lambda p, n: types.SimpleNamespace(name=n))
    m = _Msg("/start")
    upd = _upd(m, chat_type="private")
    assert bot._maybe_autobind_dm(upd) is True
    assert bot.TOPIC_BINDING[(AA_CHAT, 0)] == ("academicAssistant", "tutor")
    asyncio.run(bot.cmd_help(upd, None))
    assert m.replies[-1][0] == group_menu.MENU_TITLE


# --- review fixes (2026-10-09) ----------------------------------------------------------
def test_topic_id_needs_a_real_topic_message():
    reply_chain = types.SimpleNamespace(message_thread_id=55, is_topic_message=None)
    in_topic = types.SimpleNamespace(message_thread_id=55, is_topic_message=True)
    double = types.SimpleNamespace(message_thread_id=55)  # test doubles keep the id
    assert bot._topic_id(reply_chain) == 0
    assert bot._topic_id(in_topic) == 55
    assert bot._topic_id(double) == 55


def test_registry_ids_differ_across_processes():
    a, b = chat_session.CallbackRegistry(), chat_session.CallbackRegistry()
    ia, ib = a.register({"x": 1}), b.register({"x": 2})
    assert ia != ib and b.get(ia) is None
    assert len(group_menu.DRILL_PREFIX + ia) <= 64


def test_drill_id_from_another_chat_is_refused(wired, monkeypatch):
    m = _tap("gm:interroga")
    data = m.replies[-1][1]["reply_markup"].inline_keyboard[1][0].callback_data
    answers = []

    class _Q(_Query):
        async def answer(self, *a, **kw):
            answers.append(a)

    other = _upd(m, _Q(data, m))
    other.effective_chat = types.SimpleNamespace(id=AA_CHAT, type="supergroup")
    m.message_thread_id = 99  # same chat, other topic
    asyncio.run(bot.on_drill_callback(other, None))
    assert answers[-1] == (bot.DRILL_EXPIRED_MSG,)
    assert not m.edits


def test_topic_created_by_hand_is_bound_to_the_group_project(wired, monkeypatch):
    monkeypatch.setattr(bot, "TOPIC_BINDING", {})
    monkeypatch.setattr(bot, "TOPIC_NAMES", {})
    msg = types.SimpleNamespace(
        chat=types.SimpleNamespace(id=AA_CHAT), message_thread_id=77, is_topic_message=True,
        forum_topic_created=types.SimpleNamespace(name="Anatomia"), forum_topic_edited=None,
        reply_to_message=None)
    upd = types.SimpleNamespace(effective_message=msg,
                                effective_chat=types.SimpleNamespace(id=AA_CHAT))
    asyncio.run(bot.handle_topic_service(upd, None))
    assert bot.TOPIC_BINDING[(AA_CHAT, 77)] == ("academicAssistant", "tutor")
    assert bot.TOPIC_NAMES[(AA_CHAT, 77)] == "Anatomia"


def test_reset_during_a_turn_is_not_undone(wired, monkeypatch):
    async def fake_run_claude(prompt, **kw):
        bot._clear_session(AA_CHAT, 0)  # /new arrives while the model is working
        return ("risposta", "ses_new")

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(bot, "run_claude", fake_run_claude)
    monkeypatch.setattr(bot.indicazioni, "pubblica", lambda *a, **k: None)
    monkeypatch.setattr(bot.llm_usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_log_turn", lambda **k: None)
    monkeypatch.setattr(bot, "CLAUDE_LIVE_SESSIONS", False)
    asyncio.run(bot._claude_turn_locked(AA_CHAT, 0, wired.aa, projects.GENERALE, "x",
                                        None, bot.model_prefs.Prefs()))
    assert (AA_CHAT, 0) not in bot.SESSIONS


def test_argomenti_waits_out_retry_after(wired, monkeypatch):
    from telegram.error import RetryAfter

    monkeypatch.setattr(bot, "_is_admin", lambda u: True)
    monkeypatch.setattr(bot, "TOPIC_NAMES", {})
    monkeypatch.setattr(bot, "TOPIC_BINDING", {(AA_CHAT, 0): ("academicAssistant", "tutor")})
    monkeypatch.setattr(bot, "ARGOMENTI_PAUSE_SEC", 0)
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)

    class _Flaky(_TgBot):
        async def create_forum_topic(self, chat_id, name):
            if not self.created and not getattr(self, "hit", False):
                self.hit = True
                raise RetryAfter(2)
            return await super().create_forum_topic(chat_id, name)

    tgbot = _Flaky()
    m = _Msg("/argomenti A | B")
    upd = _upd(m)
    upd.effective_chat.is_forum = True
    asyncio.run(bot.cmd_argomenti(upd, types.SimpleNamespace(bot=tgbot)))
    assert tgbot.created == ["A", "B"]
    assert slept[0] >= 2


def test_argomenti_reports_the_real_cause(wired, monkeypatch):
    from telegram.error import BadRequest

    monkeypatch.setattr(bot, "_is_admin", lambda u: True)
    monkeypatch.setattr(bot, "TOPIC_NAMES", {})
    monkeypatch.setattr(bot, "TOPIC_BINDING", {(AA_CHAT, 0): ("academicAssistant", "tutor")})

    class _NoRights(_TgBot):
        async def create_forum_topic(self, chat_id, name):
            raise BadRequest("Not enough rights to create a topic")

    m = _Msg("/argomenti A")
    upd = _upd(m)
    upd.effective_chat.is_forum = True
    asyncio.run(bot.cmd_argomenti(upd, types.SimpleNamespace(bot=_NoRights())))
    assert "permessi" in m.replies[-1][0]


def test_chat_log_dm_only_for_dm_project_profiles(wired, monkeypatch, tmp_path):
    tg = manifest.TelegramConf(group_chat_id=AA_CHAT, agent="tutor", chat_log="brain/chat")
    monkeypatch.setattr(manifest, "load", lambda p: manifest.Manifest(telegram=tg))
    m = _Msg("ciao")
    upd = _upd(m, chat_type="private")
    monkeypatch.setattr(bot, "_profile", lambda u: profiles.PROFILES[0])  # owner
    monkeypatch.delenv("PROFILE_OWNER_DM_PROJECT", raising=False)
    bot._maybe_log_chat_turn(upd, wired.aa, None, None, "ok", None)
    assert not (tmp_path / "brain").exists()
    member = next(p for p in profiles.PROFILES if p.key == "member")
    monkeypatch.setattr(bot, "_profile", lambda u: member)
    monkeypatch.setenv("PROFILE_MEMBER_DM_PROJECT", "academicAssistant")
    bot._maybe_log_chat_turn(upd, wired.aa, None, None, "ok", None)
    assert list((tmp_path / "brain" / "chat").glob("*.md"))
