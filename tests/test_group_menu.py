"""Button menu of a project group, declared in the manifest as ``[[menu]]``.

A first-year student uses her group from a phone: /start and /menu must show a
handful of clear buttons, not the Coordinator's generic help. Groups without a
``[[menu]]`` keep /start -> cmd_help and /menu -> cmd_progetti exactly as today.
"""
import asyncio
import types

import pytest

import bot
import group_menu
import manifest
import projects
from manifest import MenuItem

AA_CHAT = -1009990077
OTHER_CHAT = -100

ITEMS = [
    MenuItem("oggi", "📅 Oggi", "/oggi", ""),
    MenuItem("orari", "🗓 Orari", "/orari", ""),
    MenuItem("scadenze", "⏳ Scadenze", "/scadenze", ""),
    MenuItem("lezione", "🎙 Registra lezione", "", "Mandami qui l'audio della lezione."),
    MenuItem("interroga", "🎓 Interrogami", "/interroga", "Su quale materia o argomento?"),
    MenuItem("spiega", "💡 Spiegami", "/spiega", "Cosa vuoi che ti rispieghi?"),
    MenuItem("cfa", "🎯 CFA", "/cfa", ""),
]

CAPS = [
    manifest.Capability("oggi", "Oggi", "[CMD:/oggi]", "both", "Uso: /oggi"),
    manifest.Capability(
        "interroga", "Interrogami", "[CMD:/interroga]", "both",
        "Uso: /interroga <corso o argomento> — es. /interroga Storia dell'arte",
    ),
]


# --- manifest parsing ----------------------------------------------------------

def _proj(key, ws):
    return projects.Project(key, key, "", f"/host/{key}", str(ws))


def test_manifest_parses_menu(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "coordinator.toml").write_text(
        '[telegram]\ngroup_chat_id = -1001\n\n'
        '[[menu]]\nkey = "oggi"\nlabel = "📅 Oggi"\ncmd = "/oggi"\n\n'
        '[[menu]]\nkey = "lezione"\nlabel = "🎙 Registra"\nhint = "Manda l\'audio"\n\n'
        '[[menu]]\nlabel = "senza chiave"\n\n'
        '[[menu]]\nkey = "rotto"\nlabel = "x"\ncmd = "oggi con spazi"\n',
        encoding="utf-8",
    )
    items = manifest.load(_proj("m", tmp_path)).menu
    assert items == [
        MenuItem("oggi", "📅 Oggi", "/oggi", ""),
        MenuItem("lezione", "🎙 Registra", "", "Manda l'audio"),
    ]


def test_manifest_without_menu_is_empty(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "coordinator.toml").write_text(
        "[telegram]\ngroup_chat_id = -1001\n", encoding="utf-8")
    assert manifest.load(_proj("n", tmp_path)).menu == []


# --- pure keyboard / resolve -----------------------------------------------------

def test_keyboard_has_two_buttons_per_row():
    kb = group_menu.build_keyboard(ITEMS)
    rows = kb.inline_keyboard
    assert len(rows) == 4
    assert [len(r) for r in rows] == [2, 2, 2, 1]
    assert rows[0][0].callback_data == "gm:oggi"
    assert rows[0][0].text == "📅 Oggi"


def test_resolve_known_and_unknown():
    assert group_menu.resolve(ITEMS, "gm:interroga").key == "interroga"
    assert group_menu.resolve(ITEMS, "gm:nope") is None
    assert group_menu.resolve(ITEMS, "nm:oggi") is None
    assert group_menu.resolve(ITEMS, "") is None


def test_hint_reply_reminds_the_command_form():
    item = group_menu.resolve(ITEMS, "gm:interroga")
    text = group_menu.hint_text(item, CAPS[1].hint)
    assert "Su quale materia o argomento?" in text
    assert "/interroga Storia dell'arte" in text


def test_hint_reply_without_example_still_names_the_command():
    item = group_menu.resolve(ITEMS, "gm:spiega")
    text = group_menu.hint_text(item, "")
    assert "Cosa vuoi che ti rispieghi?" in text and "/spiega" in text


def test_hint_only_item_answers_only_the_hint():
    item = group_menu.resolve(ITEMS, "gm:lezione")
    assert group_menu.hint_text(item, "") == "Mandami qui l'audio della lezione."


# --- bot wiring ------------------------------------------------------------------

class _Msg:
    def __init__(self, text="/start"):
        self.text = text
        self.message_thread_id = None
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


class _Query:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers: list[tuple] = []

    async def answer(self, *a, **kw):
        self.answers.append((a, kw))


def _upd(chat_id, msg, query=None):
    return types.SimpleNamespace(
        effective_message=msg,
        effective_chat=types.SimpleNamespace(id=chat_id, type="supergroup"),
        effective_user=types.SimpleNamespace(id=555),
        callback_query=query,
    )


@pytest.fixture
def wired(monkeypatch, tmp_path):
    aa = _proj("academicAssistant", tmp_path)
    other = _proj("home", tmp_path)
    mans = {
        aa.key: manifest.Manifest(
            telegram=manifest.TelegramConf(group_chat_id=AA_CHAT, agent="tutor"),
            capabilities=CAPS,
            menu=ITEMS,
        ),
        other.key: manifest.Manifest(
            telegram=manifest.TelegramConf(group_chat_id=OTHER_CHAT, agent="generale")),
    }
    monkeypatch.setattr(manifest, "load", lambda p: mans[p.key])
    monkeypatch.setattr(projects, "list_projects", lambda: [aa, other])
    monkeypatch.setattr(bot, "resolve_project",
                        lambda k: {"academicassistant": aa, "home": other}.get((k or "").lower()))
    monkeypatch.setattr(bot, "_profile_or_guest", lambda u: types.SimpleNamespace(is_admin=False))
    monkeypatch.setattr(bot, "_profile", lambda u: types.SimpleNamespace(is_admin=False))
    monkeypatch.setattr(bot, "_served_by_another_bot", lambda p, b=None: None)
    monkeypatch.setattr(bot, "_current_bot_name", lambda c: "default")
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: (None, None))
    ran = []

    async def fake_run(update, project_key, cap_key, args=None):
        ran.append((project_key, cap_key, args))

    monkeypatch.setattr(bot, "_run_capability", fake_run)
    return types.SimpleNamespace(ran=ran, aa=aa)


def test_start_in_a_menu_group_shows_the_buttons(wired):
    msg = _Msg("/start")
    asyncio.run(bot.cmd_help(_upd(AA_CHAT, msg), None))
    assert len(msg.replies) == 1
    text, kw = msg.replies[0]
    kb = kw["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "gm:oggi"


def test_menu_in_a_menu_group_shows_the_buttons(wired):
    msg = _Msg("/menu")
    asyncio.run(bot.cmd_progetti(_upd(AA_CHAT, msg), None))
    assert len(msg.replies) == 1
    assert msg.replies[0][1]["reply_markup"].inline_keyboard[0][0].callback_data == "gm:oggi"


def test_start_in_a_group_without_menu_is_unchanged(wired):
    msg = _Msg("/start")
    asyncio.run(bot.cmd_help(_upd(OTHER_CHAT, msg), None))
    assert len(msg.replies) == 1
    text, kw = msg.replies[0]
    assert text.startswith(bot.HELP_HTML[:40])
    assert "reply_markup" not in kw


def test_menu_in_a_group_without_menu_is_unchanged(wired, monkeypatch):
    monkeypatch.setattr(bot, "_kb_home", lambda profile: "HOME_KB")
    msg = _Msg("/menu")
    asyncio.run(bot.cmd_progetti(_upd(OTHER_CHAT, msg), None))
    assert msg.replies == [(bot.HOME_TITLE, {"parse_mode": bot.ParseMode.HTML,
                                             "reply_markup": "HOME_KB"})]


def test_tap_on_a_command_button_runs_the_capability(wired):
    msg = _Msg("menu")
    q = _Query("gm:oggi", msg)
    asyncio.run(bot.on_group_menu_callback(_upd(AA_CHAT, msg, q), None))
    assert q.answers, "answerCallbackQuery always"
    assert wired.ran == [("academicAssistant", "oggi", "")]
    assert msg.replies == []


def test_tap_on_a_hint_button_answers_with_the_hint(wired):
    msg = _Msg("menu")
    q = _Query("gm:interroga", msg)
    asyncio.run(bot.on_group_menu_callback(_upd(AA_CHAT, msg, q), None))
    assert q.answers
    assert wired.ran == []
    assert len(msg.replies) == 1
    assert "Su quale materia o argomento?" in msg.replies[0][0]
    assert "/interroga Storia dell'arte" in msg.replies[0][0]


def test_tap_on_a_hint_only_button(wired):
    msg = _Msg("menu")
    q = _Query("gm:lezione", msg)
    asyncio.run(bot.on_group_menu_callback(_upd(AA_CHAT, msg, q), None))
    assert q.answers and wired.ran == []
    assert msg.replies[0][0] == "Mandami qui l'audio della lezione."


def test_tap_on_an_unknown_button_only_answers(wired):
    msg = _Msg("menu")
    q = _Query("gm:sparito", msg)
    asyncio.run(bot.on_group_menu_callback(_upd(AA_CHAT, msg, q), None))
    assert q.answers and wired.ran == [] and msg.replies == []


def test_tap_by_a_stranger_is_refused(wired, monkeypatch):
    monkeypatch.setattr(bot, "_profile_or_guest", lambda u: None)
    msg = _Msg("menu")
    q = _Query("gm:oggi", msg)
    asyncio.run(bot.on_group_menu_callback(_upd(AA_CHAT, msg, q), None))
    assert q.answers and wired.ran == []


def test_capability_handler_still_parses_typed_args(monkeypatch, wired):
    handler = bot._make_capability_handler("academicAssistant", "interroga")
    msg = _Msg("/interroga Storia dell'arte")
    asyncio.run(handler(_upd(AA_CHAT, msg), None))
    assert wired.ran == [("academicAssistant", "interroga", None)]


def test_run_capability_with_explicit_args_ignores_the_menu_text(wired, monkeypatch):
    """A tap replies under the bot's own menu message: its text is not the ask."""
    monkeypatch.setattr(bot, "_run_capability", _REAL_RUN)
    monkeypatch.setattr(bot, "resolve_agent", lambda p, name: types.SimpleNamespace(name=name))
    seen = []

    async def fake_invoke(update, project, agent, prompt):
        seen.append((project.key, agent.name, prompt))

    monkeypatch.setattr(bot, "_invoke_and_reply", fake_invoke)
    msg = _Msg("🎓 Cosa vuoi fare? Tocca un bottone")
    asyncio.run(bot._run_capability(_upd(AA_CHAT, msg), "academicAssistant", "oggi", ""))
    assert seen == [("academicAssistant", "tutor", "[CMD:/oggi]")]


_REAL_RUN = bot._run_capability


def test_sections_start_new_rows():
    from manifest import MenuItem
    items = [
        MenuItem("a", "A", cmd="/a", section="Studio"),
        MenuItem("b", "B", cmd="/b", section="Studio"),
        MenuItem("c", "C", cmd="/c", section="Studio"),
        MenuItem("d", "D", cmd="/d", section="Altro"),
    ]
    rows = group_menu.build_keyboard(items).inline_keyboard
    assert [[b.text for b in r] for r in rows] == [["A", "B"], ["C"], ["D"]]


def test_resolve_arg_and_extract_buttons():
    from manifest import MenuItem
    items = [MenuItem("interroga", "Interrogami", cmd="/interroga")]
    item, arg = group_menu.resolve_arg(items, "gm:interroga:Storia dell'arte")
    assert item is items[0] and arg == "Storia dell'arte"
    assert group_menu.resolve_arg(items, "gm:nope:x") == (None, "")
    body, kb = group_menu.extract_buttons("Lezioni\n[[btn: Salta | salta:12]]\nfine")
    assert body == "Lezioni\nfine"
    assert kb.inline_keyboard[0][0].callback_data == "gm:salta:12"
    body, kb = group_menu.extract_buttons("solo testo")
    assert body == "solo testo" and kb is None
