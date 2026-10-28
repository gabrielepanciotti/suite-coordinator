# tests/test_group_attesa.py
"""A group added before its project existed gets the menu again once the
registry brings something it has never seen (#53)."""
import asyncio

import bot
import group_register


def test_da_riproporre_only_with_something_new():
    attesa = {1: ["a", "b"], 2: [], 3: ["a"]}
    assert group_register.da_riproporre(attesa, ["a", "b"], set()) == [2, 3]
    assert group_register.da_riproporre(attesa, ["a"], set()) == [2]
    assert group_register.da_riproporre(attesa, ["a", "b", "c"], set()) == [1, 2, 3]


def test_da_riproporre_skips_bound_groups():
    assert group_register.da_riproporre({1: []}, ["a"], {1}) == []


class _Chat:
    def __init__(self, title):
        self.title = title


class _FakeBot:
    def __init__(self, missing=()):
        self.sent, self.missing = [], set(missing)

    async def get_chat(self, chat_id):
        if chat_id in self.missing:
            raise RuntimeError("Chat not found")
        return _Chat(f"G{chat_id}")

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(chat_id)


def _setup(monkeypatch, attesa, legabili):
    monkeypatch.setattr(bot, "_GRUPPI_IN_ATTESA", dict(attesa))
    monkeypatch.setattr(bot, "TOPIC_BINDING", {})
    monkeypatch.setattr(bot, "_progetti_legabili", lambda: [(k, k) for k in legabili])
    monkeypatch.setattr(bot, "_menu_gruppo", lambda i: None)
    monkeypatch.setattr(bot, "_save_state", lambda: None)


def test_new_project_reopens_the_menu_once(monkeypatch):
    _setup(monkeypatch, {-100: ["offerte"]}, ["offerte", "finance"])
    fake = _FakeBot()
    asyncio.run(bot._riproponi_gruppi_in_attesa(fake))
    assert fake.sent == [-100]
    assert bot._GRUPPI_IN_ATTESA[-100] == ["offerte", "finance"]
    # Next refresh with the same registry: nothing new, no second menu.
    asyncio.run(bot._riproponi_gruppi_in_attesa(fake))
    assert fake.sent == [-100]


def test_unreachable_group_is_forgotten(monkeypatch):
    _setup(monkeypatch, {-7: []}, ["finance"])
    fake = _FakeBot(missing={-7})
    asyncio.run(bot._riproponi_gruppi_in_attesa(fake))
    assert fake.sent == []
    assert -7 not in bot._GRUPPI_IN_ATTESA
