"""A replay whose target message is gone must still produce an answer."""
import asyncio

import pytest
from telegram.error import BadRequest

import bot


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, message_thread_id=None):
        self.sent.append((chat_id, text, message_thread_id))
        return "plain"


class _FakeMsg:
    message_id = 153
    chat_id = -100
    message_thread_id = 6

    def __init__(self, exc=None):
        self._exc, self._bot = exc, _FakeBot()

    def get_bot(self):
        return self._bot

    async def reply_text(self, text, do_quote=False):
        if self._exc:
            raise self._exc
        return "quoted"


def test_falls_back_when_target_is_gone():
    msg = _FakeMsg(BadRequest("Message to be replied not found"))
    assert asyncio.run(bot._reply_or_plain(msg, "x", quote=True)) == "plain"
    assert msg._bot.sent == [(-100, "x", 6)]


def test_other_errors_still_raise():
    msg = _FakeMsg(BadRequest("Chat not found"))
    with pytest.raises(BadRequest):
        asyncio.run(bot._reply_or_plain(msg, "x"))


def test_happy_path_replies_normally():
    assert asyncio.run(bot._reply_or_plain(_FakeMsg(), "x")) == "quoted"
