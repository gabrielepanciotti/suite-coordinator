"""The bot must follow a group Telegram promotes to a supergroup.

Promotion changes the chat id, and the resulting failure is silence: the
notification keeps firing into an id nobody answers. Telegram announces it once
and never again, so the handler is the only chance to catch it.
"""
import asyncio
from types import SimpleNamespace

import bot


def _update(chat_id, migrate_to=None):
    msg = SimpleNamespace(migrate_to_chat_id=migrate_to)
    return SimpleNamespace(effective_message=msg,
                           effective_chat=SimpleNamespace(id=chat_id))


def test_migration_repoints_the_notifications(monkeypatch):
    calls = {}

    def fake_remap(old, new):
        calls.update(old=old, new=new)
        return ["life-deriva-rete"]
    monkeypatch.setattr(bot.notif_store, "remap_chat", fake_remap)
    monkeypatch.setattr(bot.projects, "list_projects", lambda: [])

    asyncio.run(bot.handle_chat_migration(_update(-5480477264, -1001234567890), None))
    assert calls == {"old": -5480477264, "new": -1001234567890}


def test_the_twin_event_from_the_new_chat_changes_nothing(monkeypatch):
    # The promoted supergroup receives its own service message (migrate_FROM).
    # Acting on it would remap the chat onto itself and, worse, hide the real one.
    called = []
    monkeypatch.setattr(bot.notif_store, "remap_chat",
                        lambda old, new: called.append((old, new)) or [])
    asyncio.run(bot.handle_chat_migration(_update(-1001234567890, None), None))
    assert called == []


def test_a_manifest_still_pointing_at_the_old_id_is_named(monkeypatch, caplog):
    # coordinator.toml is a versioned file this process must not rewrite, so the
    # most it can honestly do is say which node needs a human.
    monkeypatch.setattr(bot.notif_store, "remap_chat", lambda old, new: [])
    proj = SimpleNamespace(key="travel")
    monkeypatch.setattr(bot.projects, "list_projects", lambda: [proj])
    monkeypatch.setattr(bot.manifest, "telegram_of",
                        lambda p: SimpleNamespace(group_chat_id=-5480477264))

    with caplog.at_level("WARNING"):
        asyncio.run(bot.handle_chat_migration(_update(-5480477264, -100999), None))
    assert "travel" in caplog.text and "coordinator.toml" in caplog.text
