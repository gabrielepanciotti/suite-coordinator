"""``@argomento: <name>`` aims a group notification at one forum topic.

AcademicAssistant's post-lesson reminder asks a member to upload the recording in
the subject's topic (2026-10-16): the topic changes lesson by lesson, so the
provider names it in the text and the scheduler resolves it.
"""
import asyncio
from types import SimpleNamespace

import bot
import notif_store
import notifications
import scheduler

CHAT = -1004317396342


def test_directive_is_split_off():
    assert notifications.split_topic_directive("@argomento: Fotografia\nCaricami qui l'audio") == (
        "Fotografia", "Caricami qui l'audio")


def test_text_without_directive_is_untouched():
    assert notifications.split_topic_directive("Ciao") == (None, "Ciao")


def test_empty_directive_is_still_stripped():
    assert notifications.split_topic_directive("@argomento:\nCiao") == (None, "Ciao")


def _fire(monkeypatch, text, targets, resolver):
    notif = SimpleNamespace(key="k", enabled=True, destination="group", params={}, provider="agent")
    monkeypatch.setattr(notif_store, "get", lambda k: notif)
    monkeypatch.setattr(notif_store, "record_run", lambda k, w: None)
    monkeypatch.setattr(notif_store, "record_outcome", lambda *a: None)
    monkeypatch.setattr(scheduler.notifications, "is_banned", lambda n: False)
    monkeypatch.setattr(scheduler.overview_store, "is_paused", lambda p: False)
    monkeypatch.setattr(scheduler.notifications, "resolve_targets", lambda n: targets)
    sent = []

    async def fake_provider(n, *, run_claude):
        return text

    async def fake_send(chat_id, thread_id, body):
        sent.append((chat_id, thread_id, body))

    monkeypatch.setattr(scheduler.providers, "run_provider", fake_provider)
    monkeypatch.setitem(scheduler._S, "send_target", fake_send)
    monkeypatch.setitem(scheduler._S, "run_claude", None)
    monkeypatch.setitem(scheduler._S, "topic_thread", resolver)
    asyncio.run(scheduler._fire("k"))
    return sent


def test_group_message_goes_to_the_named_topic(monkeypatch):
    sent = _fire(monkeypatch, "@argomento: Stili del tatuaggio\nCaricami qui la lezione",
                 [(CHAT, None)], lambda chat, name: 31 if name == "Stili del tatuaggio" else None)
    assert sent == [(CHAT, 31, "Caricami qui la lezione")]


def test_unknown_topic_falls_back_to_general_without_the_line(monkeypatch):
    sent = _fire(monkeypatch, "@argomento: Boh\nCiao", [(CHAT, None)], lambda chat, name: None)
    assert sent == [(CHAT, None, "Ciao")]


def test_explicit_thread_is_kept(monkeypatch):
    sent = _fire(monkeypatch, "@argomento: Fotografia\nCiao", [(CHAT, 7)], lambda chat, name: 21)
    assert sent == [(CHAT, 7, "Ciao")]


def test_bot_resolves_topic_names_case_insensitively(monkeypatch):
    monkeypatch.setattr(bot, "TOPIC_NAMES", {(CHAT, 21): "Fotografia", (-1, 21): "Fotografia"})
    assert bot._topic_thread_by_name(CHAT, " fotografia ") == 21
    assert bot._topic_thread_by_name(CHAT, "Pittura") is None
