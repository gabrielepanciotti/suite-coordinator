"""Recorded lessons sent to a project group reach that group's agent (a member).

A group whose manifest declares ``[telegram] audio_command`` turns every voice
note, audio file, video, video note or audio/video document into a turn of the
group's agent (``[CMD:<audio_command>]``), with the file saved in the inbox.
Every branch answers: a lesson sent into silence is a lesson lost.
Groups without ``audio_command`` keep today's behaviour exactly.
"""
import asyncio
import datetime as dt
import types

import pytest
from telegram.error import BadRequest

import bot
import manifest
import notifications
import profiles
import projects

AA_CHAT = -1009990077
OTHER_CHAT = -100


def _proj(key, ws):
    return projects.Project(key, key, "", f"/host/{key}", str(ws))


def _update(chat_id, *, kind="voice", mime=None, file_name=None, caption=None,
            get_file_error=None):
    replies = []
    order = []
    downloaded = []

    async def download_to_drive(path):
        with open(path, "wb") as fh:
            fh.write(b"OggS")
        downloaded.append(path)

    async def get_file(**kwargs):
        if get_file_error is not None:
            raise get_file_error
        return types.SimpleNamespace(download_to_drive=download_to_drive)

    media = types.SimpleNamespace(get_file=get_file, mime_type=mime, file_name=file_name)
    msg = types.SimpleNamespace(
        voice=None, audio=None, video=None, video_note=None, document=None,
        photo=None, text=None, caption=caption,
        message_id=42, message_thread_id=None,
        date=dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC),
        from_user=types.SimpleNamespace(id=555, first_name="a member", username=None),
    )
    setattr(msg, kind, media)

    async def reply_text(t, **kw):
        replies.append(t)
        order.append(("reply", t))

    msg.reply_text = reply_text
    upd = types.SimpleNamespace(
        effective_message=msg,
        effective_chat=types.SimpleNamespace(id=chat_id, type="supergroup"),
        effective_user=types.SimpleNamespace(id=555),
    )
    return upd, replies, order, downloaded


@pytest.fixture
def wired(monkeypatch, tmp_path):
    aa_ws = tmp_path / "AcademicAssistant"
    aa_ws.mkdir()
    (aa_ws / "CLAUDE.md").write_text("x", encoding="utf-8")
    aa = _proj("academicAssistant", aa_ws)
    other = _proj("home", tmp_path)
    mans = {
        aa.key: manifest.Manifest(telegram=manifest.TelegramConf(
            group_chat_id=AA_CHAT, agent="tutor", audio_command="/lezione")),
        other.key: manifest.Manifest(telegram=manifest.TelegramConf(
            group_chat_id=OTHER_CHAT, agent="generale")),
    }
    monkeypatch.setattr(manifest, "load", lambda p: mans[p.key])
    monkeypatch.setattr(projects, "list_projects", lambda: [aa, other])
    monkeypatch.setattr(bot, "resolve_project",
                        lambda k: {"academicassistant": aa, "home": other}.get((k or "").lower()))
    monkeypatch.setattr(bot, "resolve_agent",
                        lambda p, name: types.SimpleNamespace(name=name))
    monkeypatch.setattr(bot, "_profile_or_guest", lambda u: types.SimpleNamespace(admin=False))
    monkeypatch.setattr(bot, "_is_stale", lambda m: False)
    monkeypatch.setattr(bot, "can_access", lambda p, k: True)
    monkeypatch.setattr(bot, "_served_by_another_bot", lambda p, b=None: None)
    monkeypatch.setattr(bot, "_current_bot_name", lambda c: "default")
    invoked = []

    async def fake_invoke(update, project, agent, prompt):
        invoked.append((project.key, agent.name, prompt))

    monkeypatch.setattr(bot, "_invoke_and_reply", fake_invoke)
    return types.SimpleNamespace(aa=aa, ws=aa_ws, invoked=invoked)


def test_manifest_parses_audio_command(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "coordinator.toml").write_text(
        '[telegram]\ngroup_chat_id = -1001\nagent = "tutor"\naudio_command = "/lezione"\n',
        encoding="utf-8",
    )
    tg = manifest.load(_proj("x", tmp_path)).telegram
    assert tg.audio_command == "/lezione"


def test_manifest_without_audio_command_is_none(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "coordinator.toml").write_text(
        "[telegram]\ngroup_chat_id = -1001\n", encoding="utf-8")
    assert manifest.load(_proj("y", tmp_path)).telegram.audio_command is None


def test_audio_command_for_the_bound_group(wired):
    assert bot.audio_command_for(AA_CHAT) == "/lezione"
    assert bot.audio_command_for(OTHER_CHAT) is None
    assert bot.audio_command_for(-1) is None


def test_voice_in_the_lesson_group_reaches_the_tutor(wired):
    upd, replies, order, downloaded = _update(AA_CHAT, kind="voice")
    asyncio.run(bot.handle_voice(upd, None))
    assert len(wired.invoked) == 1
    key, agent, prompt = wired.invoked[0]
    assert key == "academicAssistant" and agent == "tutor"
    assert "[CMD:/lezione]" in prompt and "Allegato:" in prompt
    assert ".claude/inbox/telegram/2026-10-12_42_" in prompt
    assert len(downloaded) == 1 and downloaded[0].startswith(str(wired.ws))
    assert replies and replies[0].startswith("🎙 Ricevuta!")


@pytest.mark.parametrize("kind,mime,name", [
    ("audio", "audio/mpeg", "lezione.mp3"),
    ("document", "audio/mpeg", "registrazione.mp3"),
    ("video", "video/mp4", None),
    ("video_note", None, None),
])
def test_audio_files_and_documents_reach_the_tutor(wired, kind, mime, name):
    upd, replies, order, downloaded = _update(AA_CHAT, kind=kind, mime=mime, file_name=name)
    handled = asyncio.run(bot.handle_project_audio(upd, None))
    assert handled is True
    assert len(wired.invoked) == 1
    assert "[CMD:/lezione]" in wired.invoked[0][2] and "Allegato:" in wired.invoked[0][2]
    assert replies[0] == ("🎙 Ricevuta! La trascrivo e ti mando gli appunti appena pronti "
                          "(per una lezione lunga ci vuole circa un'ora).")
    if name:
        assert wired.invoked[0][2].endswith(name)


def test_a_too_big_file_is_said_and_not_invoked(wired, caplog):
    upd, replies, order, downloaded = _update(
        AA_CHAT, kind="audio", mime="audio/mpeg",
        get_file_error=BadRequest("File is too big"))
    handled = asyncio.run(bot.handle_project_audio(upd, None))
    assert handled is True
    assert wired.invoked == []
    assert any("troppo grande" in r for r in replies)
    assert replies[-1] == ("😕 Questo file è troppo grande per arrivarmi. Prova a dividere la "
                           "registrazione in due parti e mandale una dopo l'altra.")
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_another_download_failure_is_not_silent(wired):
    upd, replies, order, downloaded = _update(
        AA_CHAT, kind="audio", mime="audio/mpeg", get_file_error=RuntimeError("boom"))
    assert asyncio.run(bot.handle_project_audio(upd, None)) is True
    assert wired.invoked == []
    assert replies, "a failed download must still answer"


def test_a_non_media_document_is_not_ours(wired):
    upd, replies, *_ = _update(AA_CHAT, kind="document", mime="application/pdf")
    assert asyncio.run(bot.handle_project_audio(upd, None)) is False
    assert wired.invoked == [] and replies == []


def test_voice_in_a_group_without_audio_command_keeps_the_voice_inbox(wired, monkeypatch):
    upd, replies, *_ = _update(OTHER_CHAT, kind="voice")
    assert asyncio.run(bot.handle_project_audio(upd, None)) is False
    queued = []
    monkeypatch.setattr(bot, "_topic_binding",
                        lambda c, t: (wired.aa, types.SimpleNamespace(name="g")))

    async def fake_v2t(msg):
        return "un'idea"

    monkeypatch.setattr(bot, "_voice_to_text", fake_v2t)
    monkeypatch.setattr(bot.voice_inbox, "instance_dir", lambda: "/inst")
    monkeypatch.setattr(bot.voice_inbox, "append_idea",
                        lambda inst, nodo, text, s, **kw: queued.append((inst, text)) or "/p")
    monkeypatch.setattr(bot.voice_inbox, "publish", lambda inst, nodo: True)
    monkeypatch.setattr(bot.voice_inbox, "instance_name", lambda inst: "inst")
    asyncio.run(bot.handle_voice(upd, None))
    assert queued == [("/inst", "un'idea")]
    assert wired.invoked == []


def test_the_lesson_filter_matches_only_lesson_groups(wired):
    f = bot.LESSON_AUDIO_FILTER
    aa_msg = types.SimpleNamespace(chat_id=AA_CHAT, chat=types.SimpleNamespace(id=AA_CHAT))
    other_msg = types.SimpleNamespace(chat_id=OTHER_CHAT, chat=types.SimpleNamespace(id=OTHER_CHAT))
    assert f.filter(aa_msg) is True
    assert f.filter(other_msg) is False


def test_lesson_audio_handler_runs_before_the_group_catch_all(monkeypatch):
    from telegram.ext import Application

    monkeypatch.setattr(bot.manifest, "group_bindings", lambda: [(-100, 0, "home", "g")])
    app = Application.builder().token("1:x").build()
    bot._register_default_handlers(app)
    callbacks = [getattr(h.callback, "func", h.callback) for h in app.handlers[0]]
    assert (callbacks.index(bot.handle_project_audio)
            < callbacks.index(bot.handle_project_group_message))


def test_member_can_access_academic_assistant():
    member = next(p for p in profiles.PROFILES if p.key == "member")
    assert profiles.can_access(member, "academicAssistant")


@pytest.mark.parametrize("key", [
    "academicassistant-prelezione",
    "academicassistant-postlezione",
    "academicassistant-sentinella",
])
def test_lesson_notifications_are_allowlisted_by_default(key):
    assert key in notifications.load_allowlist({})


def test_an_edited_recording_is_not_transcribed_again(wired):
    # 2026-10-16: an edited_message of a lesson already processed queued it again.
    upd, replies, order, downloaded = _update(AA_CHAT, kind="audio", mime="audio/mpeg",
                                              file_name="lezione.mp3", caption="Fotografia")
    upd.edited_message = upd.effective_message
    assert asyncio.run(bot.handle_project_audio(upd, None)) is True
    assert wired.invoked == [] and replies == [] and downloaded == []


@pytest.fixture(autouse=True)
def _no_download_retry_waits(monkeypatch):
    """I tentativi di download aspettano 5+20+60 s: nei test non si aspetta."""
    monkeypatch.setattr(bot, "DOWNLOAD_RETRY_WAITS", ())
