"""Lessons queued for a night job instead of transcribed on the box (AcademicAssistant #34).

A project that declares ``[telegram] audio_queue`` gets the recording saved as
usual plus one JSON entry in that directory; nothing is transcribed and no
agent turn starts. The group is told the lesson comes tomorrow morning.
"""
import asyncio
import json

import pytest
from test_audio_preprocess import _toml
from test_lesson_audio import AA_CHAT, _update, wired  # noqa: F401 - fixture

import bot
import manifest

QUEUE = "99_raw/coda_lezioni"


def test_manifest_parses_audio_queue(tmp_path):
    tg = _toml(tmp_path, f'[telegram]\ngroup_chat_id = -1001\naudio_queue = "{QUEUE}"\n')
    assert tg.audio_queue == QUEUE


@pytest.mark.parametrize("raw", ['"/abs/coda"', '"../fuori"', '"a/../b"', '"C:/coda"', '""', "3"])
def test_manifest_ignores_an_unsafe_audio_queue(tmp_path, raw):
    assert _toml(tmp_path, f"[telegram]\ngroup_chat_id = -1001\naudio_queue = {raw}\n").audio_queue is None


@pytest.fixture
def queued(wired, monkeypatch):  # noqa: F811
    conf = manifest.TelegramConf(group_chat_id=AA_CHAT, agent="tutor", audio_command="/lezione",
                                 audio_preprocess="sh x {file} {out}", audio_queue=QUEUE)
    monkeypatch.setattr(manifest, "load", lambda p: manifest.Manifest(telegram=conf))
    spawned = []
    monkeypatch.setattr(bot, "_spawn_background", lambda coro, name: (spawned.append(name), coro.close()))
    wired.spawned = spawned
    return wired


def test_a_lesson_is_queued_not_transcribed_nor_handed_to_the_agent(queued):
    upd, replies, order, downloaded = _update(AA_CHAT, kind="audio", mime="audio/mpeg",
                                              file_name="lezione.mp3", caption="Storia dell'arte moderna")
    reactions = []

    async def set_reaction(r):
        reactions.append(r)

    upd.effective_message.set_reaction = set_reaction
    assert asyncio.run(bot.handle_project_audio(upd, None)) is True
    assert queued.invoked == [] and queued.spawned == []  # niente agente, niente trascrizione
    assert replies == [] and reactions == [["👀"]]  # nessuna risposta: solo la reazione «preso in carico»
    (entry,) = (queued.ws / QUEUE).glob("*.json")
    data = json.loads(entry.read_text(encoding="utf-8"))
    assert data["audio"].startswith(".claude/inbox/telegram/2026-10-12_42_") and data["audio"].endswith("lezione.mp3")
    assert data["argomento"] == "Storia dell'arte moderna" and data["message_id"] == 42
    assert entry.stem == data["audio"].rsplit("/", 1)[1][: -len(".mp3")]
    assert (queued.ws / data["audio"]).exists() and not list((queued.ws / QUEUE).glob("*.tmp"))


def test_a_queue_write_failure_is_told_to_group_and_owner(queued, monkeypatch):
    def boom(*a, **k):
        raise OSError("disco pieno")

    monkeypatch.setattr(bot, "_enqueue_lesson", boom)
    dms = []

    async def fake_notify(update, context, project, rel, error, title="x"):
        dms.append((title, error))

    monkeypatch.setattr(bot, "_notify_owner_of_preprocess_failure", fake_notify)
    upd, replies, *_ = _update(AA_CHAT, kind="voice")
    assert asyncio.run(bot.handle_project_audio(upd, None)) is True
    assert replies == [bot.LESSON_AUDIO_FAILED_MSG]  # la reazione non è una risposta
    assert dms == [("Lezione non messa in coda", "OSError: disco pieno")]


def test_request_run_leaves_a_flag_only_when_a_lesson_waits(queued):
    ws = queued.ws
    flag = ws / QUEUE / bot.LESSON_NOW_FLAG
    fake = type("P", (), {"workspace": str(ws), "key": "aa"})()
    assert bot._request_lesson_run(fake, QUEUE) == 0 and not flag.exists()  # coda assente
    (ws / QUEUE).mkdir(parents=True, exist_ok=True)
    assert bot._request_lesson_run(fake, QUEUE) == 0 and not flag.exists()  # coda vuota
    (ws / QUEUE / "a.json").write_text("{}", encoding="utf-8")
    assert bot._request_lesson_run(fake, QUEUE) == 1 and flag.exists()
    assert not flag.name.endswith(".json")  # il job non la legge come lezione


def test_anonymous_admin_of_a_lesson_group_is_a_participant_not_an_admin(queued):  # noqa: F811
    upd, *_ = _update(AA_CHAT, kind="audio", mime="audio/mpeg", file_name="l.mp3")
    upd.effective_user.id = bot.ANONYMOUS_ADMIN_ID
    prof = bot._profile(upd)
    assert prof is not None and prof.is_admin is False and len(prof.projects) == 1
    other, *_ = _update(AA_CHAT + 1, kind="audio", mime="audio/mpeg", file_name="l.mp3")
    other.effective_user.id = bot.ANONYMOUS_ADMIN_ID
    assert bot._anonymous_admin_profile(other) is None  # gruppo senza audio_command


def test_get_file_retries_then_gives_up_without_agent_chatter(monkeypatch):
    monkeypatch.setattr(bot, "DOWNLOAD_RETRY_WAITS", (0.0, 0.0))
    calls = []

    class Media:
        async def get_file(self, **kw):
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("boom")
            return "ok"

    assert asyncio.run(bot._get_file_with_retry(Media())) == "ok" and len(calls) == 3

    class Dead:
        async def get_file(self, **kw):
            raise TimeoutError("boom")

    with pytest.raises(TimeoutError):
        asyncio.run(bot._get_file_with_retry(Dead()))


@pytest.fixture(autouse=True)
def _no_download_retry_waits(monkeypatch):
    """I tentativi di download aspettano 5+20+60 s: nei test non si aspetta."""
    monkeypatch.setattr(bot, "DOWNLOAD_RETRY_WAITS", ())


def test_replay_falls_back_to_the_group_binding_for_an_unbound_topic(monkeypatch):
    """Il live ricade sul binding del gruppo: il replay deve fare lo stesso, o perde ogni messaggio degli argomenti."""
    group = (object(), object())
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: group if (c, t) == (-1, 0) else (None, None))
    assert bot._replay_bound(-1, 13) is True and bot._replay_binding(-1, 13) == group
    assert bot._replay_bound(-2, 13) is False
