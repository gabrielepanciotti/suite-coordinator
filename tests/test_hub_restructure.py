# tests/test_hub_restructure.py
import asyncio

import hub


def _run(coro):
    return asyncio.run(coro)


def test_handle_human_event_routes_verdict_to_restructure(monkeypatch):
    called = {}

    async def fake_pipeline_on_verdict(cid, mid):
        return False

    async def fake_migrate_on_verdict(cid, mid):
        return False

    async def fake_restructure_on_verdict(cid, mid):
        called["args"] = (cid, mid)
        return True

    monkeypatch.setattr(hub.pipeline, "on_verdict", fake_pipeline_on_verdict)
    monkeypatch.setattr(hub.migrate, "on_verdict", fake_migrate_on_verdict)
    monkeypatch.setattr(hub.restructure, "on_verdict", fake_restructure_on_verdict)

    _run(hub._handle_human_event("personale", "new_message",
                                 {"conversation_id": 42, "message_id": 7}))
    assert called["args"] == (42, 7)


def test_handle_topic_reply_routes_to_restructure(monkeypatch):
    called = {}

    class _Cfg:
        telegram_chat_id = 555

    hub._S["config"] = _Cfg()

    async def fake_migrate_decision(chat_id, topic_id, text):
        return False

    async def fake_restructure_decision(chat_id, topic_id, text):
        called["args"] = (chat_id, topic_id, text)
        return True

    monkeypatch.setattr(hub.migrate, "on_human_decision", fake_migrate_decision)
    monkeypatch.setattr(hub.restructure, "on_human_decision", fake_restructure_decision)

    handled = _run(hub.handle_topic_reply(555, 901, "procedi"))
    assert handled is True
    assert called["args"] == (555, 901, "procedi")
