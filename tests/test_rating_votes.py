"""Test per il voto a pulsanti (1-5) sotto le risposte ComUnity."""
import asyncio
import json

import bot


class _FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.full_name = name


class _FakeMessage:
    def __init__(self, chat_id, topic_id, message_id):
        self.chat_id = chat_id
        self.message_thread_id = topic_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data, message, user):
        self.data = data
        self.message = message
        self.from_user = user
        self.answered = None
        self.markup_cleared = False

    async def answer(self, text=None):
        self.answered = text

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None


class _FakeUpdate:
    def __init__(self, query):
        self.callback_query = query


def test_rating_keyboard_has_five_buttons():
    kb = bot._rating_keyboard()
    row = kb.inline_keyboard[0]
    assert [b.text for b in row] == ["1", "2", "3", "4", "5"]
    assert [b.callback_data for b in row] == ["rt:1", "rt:2", "rt:3", "rt:4", "rt:5"]


def test_append_rating_writes_jsonl_line(monkeypatch, tmp_path):
    path = tmp_path / "ratings.jsonl"
    monkeypatch.setattr(bot, "RATING_LOG_PATH", str(path))
    bot._append_rating({"rating": 2, "message_id": 42})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["rating"] == 2


def test_on_rating_callback_records_vote_and_acks(monkeypatch, tmp_path):
    async def _run():
        path = tmp_path / "ratings.jsonl"
        monkeypatch.setattr(bot, "RATING_LOG_PATH", str(path))
        msg = _FakeMessage(chat_id=-100123, topic_id=6, message_id=999)
        user = _FakeUser(uid=1, name="the owner")
        query = _FakeQuery(data="rt:2", message=msg, user=user)
        update = _FakeUpdate(query)

        await bot.on_rating_callback(update, context=None)

        assert query.answered == "Voto registrato: 2/5"
        rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert rec["rating"] == 2
        assert rec["message_id"] == 999
        assert rec["topic_id"] == 6
        assert rec["project"] == "comunity"
        assert rec["voter_id"] == 1
        assert query.markup_cleared is True

    asyncio.run(_run())


def test_on_rating_callback_ignores_failed_markup_edit(monkeypatch, tmp_path):
    """Un edit fallito (messaggio troppo vecchio, doppio tap) non deve rompere
    l'ack del voto: e' un best-effort, non un requisito."""

    async def _run():
        path = tmp_path / "ratings.jsonl"
        monkeypatch.setattr(bot, "RATING_LOG_PATH", str(path))
        msg = _FakeMessage(chat_id=-100123, topic_id=6, message_id=999)
        user = _FakeUser(uid=1, name="the owner")
        query = _FakeQuery(data="rt:4", message=msg, user=user)

        async def _boom(reply_markup=None):
            raise RuntimeError("Message is not modified")

        query.edit_message_reply_markup = _boom
        update = _FakeUpdate(query)

        await bot.on_rating_callback(update, context=None)

        assert query.answered == "Voto registrato: 4/5"

    asyncio.run(_run())
