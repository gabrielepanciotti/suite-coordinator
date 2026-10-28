import pytest
import bot


def test_strip_confirm_sentinel_present():
    text, wants = bot._strip_confirm_sentinel("Imposto martedì 18–22.\n⟦CONFIRM⟧")
    assert wants is True
    assert text == "Imposto martedì 18–22."
    assert "⟦CONFIRM⟧" not in text


def test_strip_confirm_sentinel_absent():
    text, wants = bot._strip_confirm_sentinel("Ecco lo stato attuale.")
    assert wants is False
    assert text == "Ecco lo stato attuale."


def test_kb_confirm_callback_data_roundtrips():
    kb = bot._kb_confirm(-1003455335251, 0)
    ok, no = kb.inline_keyboard[0][0], kb.inline_keyboard[0][1]
    assert bot.parse_cb(ok.callback_data) == ["cf", "ok", "-1003455335251", "0"]
    assert bot.parse_cb(no.callback_data) == ["cf", "no", "-1003455335251", "0"]
    assert len(ok.callback_data.encode()) <= 64


def test_group_message_prompt_includes_tg_id():
    p = bot._group_message_prompt("Owner", 111111, "segnami pronto", None, False)
    assert "mittente: Owner (tg_id: 111111)." in p
    assert "Testo: segnami pronto" in p


def test_group_message_prompt_with_attachment():
    p = bot._group_message_prompt("a member", 222, "", ".claude/inbox/telegram/x.jpg", False)
    assert "tg_id: 222" in p
    assert "Allegato" in p


import asyncio


def _fake_update(data):
    captured = {}

    class FakeQuery:
        def __init__(self):
            self.data = data

        async def answer(self):
            captured["answered"] = True

        async def edit_message_reply_markup(self, reply_markup=None):
            captured["markup_cleared"] = reply_markup is None

    class FakeUpdate:
        callback_query = FakeQuery()


    return FakeUpdate(), captured


def test_on_confirm_callback_ok_reinvokes(monkeypatch):
    update, captured = _fake_update("cf:ok:-100123:0")
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: ("PROJ", "AGENT"))

    async def fake_invoke(context, chat_id, topic_id, project, agent, prompt):
        captured["call"] = (chat_id, topic_id, project, agent, prompt)

    monkeypatch.setattr(bot, "_invoke_into_topic", fake_invoke)
    asyncio.run(bot.on_confirm_callback(update, None))

    assert captured["answered"] is True
    assert captured["markup_cleared"] is True
    chat_id, topic_id, project, agent, prompt = captured["call"]
    assert (chat_id, topic_id) == (-100123, 0)
    assert (project, agent) == ("PROJ", "AGENT")
    assert prompt.startswith("[CMD:/confirm-yes]")


def test_on_confirm_callback_no_sends_cancel(monkeypatch):
    update, captured = _fake_update("cf:no:-100123:0")
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: ("PROJ", "AGENT"))

    async def fake_invoke(context, chat_id, topic_id, project, agent, prompt):
        captured["prompt"] = prompt

    monkeypatch.setattr(bot, "_invoke_into_topic", fake_invoke)
    asyncio.run(bot.on_confirm_callback(update, None))
    assert captured["prompt"].startswith("[CMD:/confirm-no]")


def test_on_confirm_callback_no_binding_noops(monkeypatch):
    update, captured = _fake_update("cf:ok:-100123:0")
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: (None, None))

    async def fake_invoke(*a, **k):
        captured["called"] = True

    monkeypatch.setattr(bot, "_invoke_into_topic", fake_invoke)
    asyncio.run(bot.on_confirm_callback(update, None))
    assert "called" not in captured


def test_send_confirm_buttons_posts_keyboard():
    captured = {}

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None, message_thread_id=None):
            captured.update(
                chat_id=chat_id, text=text, markup=reply_markup, thread=message_thread_id
            )

    asyncio.run(bot._send_confirm_buttons(FakeBot(), -100123, 5, 5))
    assert captured["chat_id"] == -100123
    assert captured["thread"] == 5
    kb = captured["markup"]
    assert bot.parse_cb(kb.inline_keyboard[0][0].callback_data) == ["cf", "ok", "-100123", "5"]
    assert bot.parse_cb(kb.inline_keyboard[0][1].callback_data) == ["cf", "no", "-100123", "5"]


def test_strip_prod_sentinel_present_with_confirm_after():
    text, live = bot._strip_prod_sentinel("Corretto e verificato.\n⟦PROD⟧\n⟦CONFIRM⟧")
    assert live is True
    assert text == "Corretto e verificato.\n⟦CONFIRM⟧"
    text, wants = bot._strip_confirm_sentinel(text)
    assert (text, wants) == ("Corretto e verificato.", True)


def test_strip_prod_sentinel_absent_leaves_reply_untouched():
    reply = "Ho corretto il codice, manca il deploy."
    assert bot._strip_prod_sentinel(reply) == (reply, False)


def test_prod_reaction_lands_on_the_remembered_request():
    import asyncio

    calls = []

    class FakeBot:
        async def set_message_reaction(self, chat_id, message_id, reactions):
            calls.append((chat_id, message_id, reactions))

    bot._LAST_REQUEST_MSG[(-100, 6)] = 87
    asyncio.run(bot._react_request_in_prod(FakeBot(), -100, 6))
    asyncio.run(bot._react_request_in_prod(FakeBot(), -100, 7))
    assert calls == [(-100, 87, [bot.REACT_PROD])]


@pytest.fixture(autouse=True)
def _presser_is_admin(monkeypatch):
    """Chi preme deve poter guidare una sessione (#89): qui e' un admin."""
    async def admin(update):
        return True

    monkeypatch.setattr(bot, "_session_full", admin)
