"""Task 12 — the generic remote card's ``cx:`` callback in bot.py (Coordinator#45).

Same shape as tests/test_redmine_bot.py: drives the real handler
(``bot._on_remote_card_cb``) with a fake Update/CallbackQuery.
``remote_cards.forward_action`` is monkeypatched so no HTTP call is made —
this suite is about what the handler does with the node's answer, not about
the HTTP round-trip itself (that's covered in tests/test_remote_cards.py).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import remote_cards


@pytest.fixture(autouse=True)
def _no_session_gate(monkeypatch):
    # Who may press the card is #89's business (test_ruoli_89): here, the card.
    monkeypatch.setattr(bot, "SESSION_GATE", False)


class _FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()


class _FakeUpdate:
    def __init__(self, data: str):
        self.callback_query = _FakeQuery(data)


def _cb(source="tm", card_id="rm-38", action="take") -> str:
    return remote_cards.callback_data(source, card_id, action)


def _tap(monkeypatch, data: str, result: dict) -> _FakeQuery:
    async def _fake_forward(source, card_id, action):
        return result

    monkeypatch.setattr(remote_cards, "forward_action", _fake_forward)
    update = _FakeUpdate(data)
    asyncio.run(bot._on_remote_card_cb(update, MagicMock()))
    return update.callback_query


def test_answer_with_buttons_edits_card_with_markup(monkeypatch):
    result = {
        "text": "✅ preso in carico",
        "buttons": [[{"label": "↩️ Indietro", "action": "back"}]],
    }
    q = _tap(monkeypatch, _cb(), result)

    q.edit_message_text.assert_awaited_once()
    args, kwargs = q.edit_message_text.call_args
    assert args[0] == "✅ preso in carico"
    markup = kwargs["reply_markup"]
    btn = markup.inline_keyboard[0][0]
    assert btn.text == "↩️ Indietro"
    assert btn.callback_data == remote_cards.callback_data("tm", "rm-38", "back")
    q.answer.assert_awaited_once_with()


def test_buttons_empty_edits_with_no_markup_card_closed(monkeypatch):
    result = {"text": "✅ chiuso", "buttons": []}
    q = _tap(monkeypatch, _cb(), result)

    q.edit_message_text.assert_awaited_once_with("✅ chiuso", reply_markup=None)
    q.answer.assert_awaited_once_with()


def test_buttons_absent_edits_with_no_markup(monkeypatch):
    """A node may omit ``buttons`` entirely on a closing answer — same as []."""
    result = {"text": "✅ chiuso"}
    q = _tap(monkeypatch, _cb(), result)

    q.edit_message_text.assert_awaited_once_with("✅ chiuso", reply_markup=None)
    q.answer.assert_awaited_once_with()


def test_toast_only_answers_without_editing(monkeypatch):
    result = {"toast": "già gestito"}
    q = _tap(monkeypatch, _cb(), result)

    q.edit_message_text.assert_not_awaited()
    q.answer.assert_awaited_once_with("già gestito")


def test_text_and_toast_edits_the_card_and_answers_with_toast(monkeypatch):
    """The controller ruling: both keys together means BOTH things happen."""
    result = {"text": "✅ inviata", "buttons": [], "toast": "mail inviata"}
    q = _tap(monkeypatch, _cb(), result)

    q.edit_message_text.assert_awaited_once_with("✅ inviata", reply_markup=None)
    q.answer.assert_awaited_once_with("mail inviata")


def test_unreachable_node_toasts_and_does_not_edit(monkeypatch):
    """forward_action degrades an unreachable node to a toast on its own
    (tests/test_remote_cards.py covers that); the handler just has to
    relay it and never touch the card."""
    result = {"toast": "⚠️ tm non raggiungibile, riprova"}
    q = _tap(monkeypatch, _cb(), result)

    q.edit_message_text.assert_not_awaited()
    q.answer.assert_awaited_once_with("⚠️ tm non raggiungibile, riprova")


def test_malformed_callback_data_answers_card_non_valida_without_forwarding(monkeypatch):
    called = []

    async def _fake_forward(*a, **k):
        called.append(1)
        return {}

    monkeypatch.setattr(remote_cards, "forward_action", _fake_forward)
    update = _FakeUpdate("cx:tm:rm-38")  # missing the action segment
    asyncio.run(bot._on_remote_card_cb(update, MagicMock()))
    q = update.callback_query

    q.answer.assert_awaited_once_with("Card non valida.")
    q.edit_message_text.assert_not_awaited()
    assert called == []  # forward_action must never run on malformed data


def test_malformed_node_answer_still_answers_once_and_does_not_crash(monkeypatch):
    """A node returning garbage (not a dict) must degrade to a generic toast,
    never raise out of the handler."""
    q = _tap(monkeypatch, _cb(), result=None)  # type: ignore[arg-type]

    q.edit_message_text.assert_not_awaited()
    q.answer.assert_awaited_once_with("⚠️ Risposta non valida da tm.")


def test_answer_without_text_or_toast_gets_generic_toast(monkeypatch):
    """A dict with neither ``text`` nor ``toast`` tells the operator nothing:
    a silent answer() would look like the tap did something."""
    q = _tap(monkeypatch, _cb(), result={"buttons": []})

    q.edit_message_text.assert_not_awaited()
    q.answer.assert_awaited_once_with("⚠️ Risposta non valida da tm.")


# ---------------------------------------------------------------------------
# Round 1 review fix: edit happens FIRST, q.answer LAST, neither can stop the
# other. A callback query expires ~15s after the tap, and by the time this
# handler gets here it has already waited on forward_action — an expired
# query makes q.answer() raise, and that must not swallow an edit for an
# action the node had already executed.
# ---------------------------------------------------------------------------

def test_expired_callback_query_answer_raises_edit_still_happens(monkeypatch):
    """q.answer() raising (expired query) must not prevent the edit, and must
    not escape the handler."""
    result = {"text": "✅ preso in carico", "buttons": []}

    async def _fake_forward(source, card_id, action):
        return result

    monkeypatch.setattr(remote_cards, "forward_action", _fake_forward)
    update = _FakeUpdate(_cb())
    q = update.callback_query
    q.answer.side_effect = Exception("Query is too old and response timeout expired")

    asyncio.run(bot._on_remote_card_cb(update, MagicMock()))  # must not raise

    q.edit_message_text.assert_awaited_once_with("✅ preso in carico", reply_markup=None)
    q.answer.assert_awaited_once_with()


def test_edit_raises_answer_still_called_exactly_once(monkeypatch):
    """A failing edit (e.g. Telegram's 'message is not modified') must not
    prevent q.answer() from running, and must not escape the handler."""
    result = {"text": "✅ preso in carico", "buttons": []}

    async def _fake_forward(source, card_id, action):
        return result

    monkeypatch.setattr(remote_cards, "forward_action", _fake_forward)
    update = _FakeUpdate(_cb())
    q = update.callback_query
    q.edit_message_text.side_effect = Exception("Message is not modified")

    asyncio.run(bot._on_remote_card_cb(update, MagicMock()))  # must not raise

    q.edit_message_text.assert_awaited_once_with("✅ preso in carico", reply_markup=None)
    q.answer.assert_awaited_once_with()


def test_digest_card_is_updated_in_the_digest_not_on_the_tapped_message(monkeypatch):
    applied = []

    async def _apply(source, card_id, result):
        applied.append((source, card_id, result))
        return True

    monkeypatch.setattr(remote_cards, "apply_answer", _apply)
    q = _tap(monkeypatch, _cb(), {"text": "✅ Preso in carico", "buttons": []})
    assert applied == [("tm", "rm-38", {"text": "✅ Preso in carico", "buttons": []})]
    q.edit_message_text.assert_not_awaited()
    q.answer.assert_awaited_once_with("✅ Preso in carico")
