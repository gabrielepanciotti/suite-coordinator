"""Which course a lesson recording belongs to (the preprocess ``{topic}``).

2026-10-16: a member posted three lessons in the General topic with the course as
caption ("Stili del tatuaggio"); the topic was empty, so the pipeline only
transcribed. The caption, or its album's caption, now names the course.
"""
import types

import pytest

import audio_preprocess
import bot

CHAT = -1004317396342


def _msg(*, thread=None, caption=None, group=None):
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(id=CHAT), chat_id=CHAT,
        message_thread_id=thread, is_topic_message=thread is not None,
        caption=caption, media_group_id=group, reply_to_message=None,
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(bot, "TOPIC_NAMES", {})
    monkeypatch.setattr(bot, "_ALBUM_CAPTIONS", {})


def test_topic_name_wins_over_caption():
    bot.TOPIC_NAMES[(CHAT, 25)] = "Fotografia"
    assert bot._lesson_topic(CHAT, _msg(thread=25, caption="Pittura")) == "Fotografia"


def test_general_topic_falls_back_to_caption():
    assert bot._lesson_topic(CHAT, _msg(caption=" Stili del tatuaggio ")) == "Stili del tatuaggio"


def test_captionless_album_item_takes_the_album_caption():
    bot._remember_album_caption(_msg(caption="Stili del tatuaggio", group="143"))
    assert bot._lesson_topic(CHAT, _msg(group="143")) == "Stili del tatuaggio"


def test_command_prefix_is_dropped():
    assert bot._lesson_topic(CHAT, _msg(caption="/lezione Pittura")) == "Pittura"


def test_nothing_known_is_empty():
    assert bot._lesson_topic(CHAT, _msg()) == ""


@pytest.mark.parametrize("code", [-9, 137, 247])
def test_sigkill_is_named(code):
    assert "memoria" in audio_preprocess.exit_reason(code)


def test_plain_failure_keeps_the_code():
    assert audio_preprocess.exit_reason(2) == "exit 2"
