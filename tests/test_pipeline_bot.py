# tests/test_pipeline_bot.py
import asyncio

import bot


def _run(coro):
    return asyncio.run(coro)


def test_kb_gate_has_approve_and_reject():
    kb = bot._kb_gate(42)
    flat = [b for row in kb.inline_keyboard for b in row]
    datas = {b.callback_data for b in flat}
    assert datas == {"pl:gate:42:ok", "pl:gate:42:no"}


def test_parse_pipeline_args_splits_project_and_task():
    assert bot._parse_pipeline_args("offerte allinea e deploya") == (
        "offerte", "allinea e deploya")
    assert bot._parse_pipeline_args("solo-progetto") == (None, None)
    assert bot._parse_pipeline_args("") == (None, None)


def test_gate_callback_calls_on_gate_decision(monkeypatch):
    calls = []

    async def fake_decision(pid, decision):
        calls.append((pid, decision))

    monkeypatch.setattr(bot.pipeline, "on_gate_decision", fake_decision)

    class _Q:
        data = "pl:gate:42:ok"
        message = None
        async def answer(self, *a, **k): pass
        async def edit_message_reply_markup(self, *a, **k): pass
    class _U:
        callback_query = _Q()
    _run(bot.on_pipeline_callback(_U(), None))
    assert calls == [(42, "ok")]
