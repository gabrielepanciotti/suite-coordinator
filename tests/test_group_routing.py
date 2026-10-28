# tests/test_group_routing.py
import bot
import manifest


def test_seed_group_bindings_binds_general_topic(monkeypatch):
    bot.TOPIC_BINDING.clear()
    monkeypatch.setattr(manifest, "group_bindings",
                        lambda: [(-100123, 0, "casa", "casa"), (-100777, 0, "travelers", "gonzalo")])
    # resolve_project must accept these keys for _bind to stick; pin to a stub
    monkeypatch.setattr(bot, "resolve_project", lambda k: object() if k else None)
    bot.seed_group_bindings()
    assert bot.TOPIC_BINDING[(-100123, 0)] == ("casa", "casa")
    assert bot.TOPIC_BINDING[(-100777, 0)] == ("travelers", "gonzalo")


def test_seed_group_bindings_binds_forum_topic(monkeypatch):
    """A project living as a topic inside a shared forum binds that topic, not 0."""
    bot.TOPIC_BINDING.clear()
    monkeypatch.setattr(manifest, "group_bindings",
                        lambda: [(-1003899308208, 20, "agenthub", "generale")])
    monkeypatch.setattr(bot, "resolve_project", lambda k: object() if k else None)
    bot.seed_group_bindings()
    assert bot.TOPIC_BINDING[(-1003899308208, 20)] == ("agenthub", "generale")
    assert (-1003899308208, 0) not in bot.TOPIC_BINDING


import manifest as _M


def _cap(key, scope):
    return _M.Capability(key, key.title(), f"cli {key}", scope)


def test_commands_for_context_filters_by_scope():
    caps = [_cap("aggiungi", "both"), _cap("contatta", "group"),
            _cap("refactor", "command-room")]
    group = [c.key for c in bot.commands_for_context(caps, "group")]
    room = [c.key for c in bot.commands_for_context(caps, "command-room")]
    assert group == ["aggiungi", "contatta"]
    assert room == ["aggiungi", "refactor"]


def test_cmd_prompt_for_capability():
    cap = _cap("aggiungi", "both")
    assert bot._cap_prompt(cap, "http://x") == "[CMD:/aggiungi] http://x"
    assert bot._cap_prompt(cap, "") == "[CMD:/aggiungi]"


# --- Part 3: pin helper ---

def test_pin_text_for_reads_manifest(monkeypatch):
    import projects
    p = projects.Project("casa", "Casa", "\U0001F3E0", "", "/ws")
    monkeypatch.setattr(_M, "telegram_of",
                        lambda pr: _M.TelegramConf(group_chat_id=-1, pin="Tabella: http://x"))
    assert bot._pin_text_for(p) == "Tabella: http://x"
    monkeypatch.setattr(_M, "telegram_of", lambda pr: _M.TelegramConf(group_chat_id=-1))
    assert bot._pin_text_for(p) is None


def test_send_and_pin_calls_api(monkeypatch):
    import asyncio
    calls = {}

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            calls["sent"] = (chat_id, text)
            return type("M", (), {"message_id": 42})()
        async def pin_chat_message(self, chat_id, message_id, **kw):
            calls["pinned"] = (chat_id, message_id)

    result = asyncio.run(bot._send_and_pin(FakeBot(), -100123, "Tabella: http://x"))
    assert calls["sent"] == (-100123, "Tabella: http://x")
    assert calls["pinned"] == (-100123, 42)
    assert result is True


def test_send_and_pin_returns_false_when_pin_fails():
    import asyncio

    class FakeBotPinFails:
        async def send_message(self, chat_id, text, **kw):
            return type("M", (), {"message_id": 99})()
        async def pin_chat_message(self, chat_id, message_id, **kw):
            raise Exception("no rights")

    assert asyncio.run(bot._send_and_pin(FakeBotPinFails(), -100, "x")) is False
