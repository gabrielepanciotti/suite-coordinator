# tests/test_workspace_write_guard.py
"""A file must never be saved into a workspace that holds no repo.

Compose mounts every node without a WS_* from the SHARED `.ws-placeholder`,
read-write. So a photo sent to a project this device does not have used to be
saved happily — no error anywhere — into Coordinator/.ws-placeholder, mixed in
with every other absent project. The live registry drops those nodes; the
static fallback (the only source on a device with no reachable Life backend)
does not, so the write path itself has to refuse.
"""
import asyncio
import datetime as dt
import types

import bot
import projects


def _update(chat_id=-100, msg_id=7, *, document=None, text=None, caption=None,
            photo=True):
    replies = []
    msg = types.SimpleNamespace(
        photo=[types.SimpleNamespace(get_file=None)] if (photo and document is None) else [],
        document=document,
        text=text,
        caption=caption,
        media_group_id=None,
        message_id=msg_id,
        chat_id=chat_id,
        message_thread_id=None,
        date=None,
        from_user=types.SimpleNamespace(id=42, first_name="Tester", username=None),
    )

    async def reply_text(t, **kw):
        replies.append(t)

    msg.reply_text = reply_text
    upd = types.SimpleNamespace(
        effective_message=msg,
        effective_chat=types.SimpleNamespace(id=chat_id, type="group"),
        effective_user=types.SimpleNamespace(id=999),
        get_bot=lambda: None,
    )
    return upd, replies


def _placeholder(tmp_path):
    """The shared placeholder as Docker leaves it: .gitkeep + a nested
    `children/` mountpoint, which is exactly what made it look cloned."""
    ws = tmp_path / "ws-placeholder"
    (ws / "children" / "someChild").mkdir(parents=True)
    (ws / ".gitkeep").write_text("", encoding="utf-8")
    return ws


def test_photo_is_refused_when_the_workspace_holds_no_repo(monkeypatch, tmp_path):
    ws = _placeholder(tmp_path)
    proj = projects.Project("travel", "🧳 Travel", "🧳", "", str(ws))
    agent = types.SimpleNamespace(name="a", persona="")
    monkeypatch.setattr(bot, "_profile", lambda u: types.SimpleNamespace(admin=True))
    monkeypatch.setattr(bot, "_is_stale", lambda m: False)
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: (proj, agent))
    monkeypatch.setattr(bot, "can_access", lambda p, k: True)
    monkeypatch.setattr(bot, "_served_by_another_bot", lambda p, b: None)

    downloaded = []

    async def fake_dl(*a, **kw):
        downloaded.append(a)
        return "nope"

    invoked = []

    async def fake_invoke(*a, **kw):
        invoked.append(a)

    monkeypatch.setattr(bot, "_download_photo", fake_dl)
    monkeypatch.setattr(bot, "_invoke_and_reply", fake_invoke)
    bot._album_buffer.clear()

    upd, replies = _update()
    asyncio.run(bot.handle_photo(upd, None))

    assert downloaded == [], "la foto e' stata scaricata in un workspace senza repo"
    assert invoked == [], "l'agente e' stato svegliato su un file mai salvato"
    assert replies and "Travel" in replies[0]
    # Nothing was written into the placeholder.
    assert not (ws / "raw").exists()


def test_group_attachment_is_refused_and_the_text_still_reaches_the_agent(
    monkeypatch, tmp_path
):
    ws = _placeholder(tmp_path)
    proj = projects.Project("travel", "🧳 Travel", "🧳", "", str(ws))
    agent = types.SimpleNamespace(name="a", persona="")
    monkeypatch.setattr(bot, "resolve_project", lambda k: proj)
    monkeypatch.setattr(bot, "resolve_agent", lambda p, n: agent)
    monkeypatch.setattr(bot, "resolve_agent_by_intent", lambda p, t, a: a)

    prompts = []

    async def fake_invoke(update, project, ag, prompt):
        prompts.append(prompt)

    monkeypatch.setattr(bot, "_invoke_and_reply", fake_invoke)

    doc = types.SimpleNamespace(file_name="listino.pdf", get_file=None)
    upd, _ = _update(document=doc, caption="ecco il listino")
    asyncio.run(bot.handle_project_group_message(upd, None, project_key="travel"))

    assert prompts, "il messaggio non e' arrivato all'agente"
    assert "non e' stato scaricato" in prompts[0]
    assert "ecco il listino" in prompts[0]
    assert not (ws / ".claude").exists(), "inbox creata in un workspace senza repo"


def test_a_text_only_group_message_leaves_no_empty_inbox_behind(monkeypatch, tmp_path):
    ws = tmp_path / "cloned"
    ws.mkdir()
    (ws / "node.config").write_text("{}", encoding="utf-8")
    proj = projects.Project("etl", "ETL", "", "", str(ws))
    agent = types.SimpleNamespace(name="a", persona="")
    monkeypatch.setattr(bot, "resolve_project", lambda k: proj)
    monkeypatch.setattr(bot, "resolve_agent", lambda p, n: agent)
    monkeypatch.setattr(bot, "resolve_agent_by_intent", lambda p, t, a: a)

    async def fake_invoke(*a, **kw):
        return None

    monkeypatch.setattr(bot, "_invoke_and_reply", fake_invoke)
    upd, _ = _update(text="ciao", photo=False)
    asyncio.run(bot.handle_project_group_message(upd, None, project_key="etl"))

    assert not (ws / ".claude").exists()


def test_a_large_group_document_waits_for_the_local_bot_api(monkeypatch, tmp_path):
    # The local Bot API answers getFile only after pulling the whole file: a
    # 50 MB PDF blew PTB's 5 s default and the agent was told "not downloaded".
    ws = tmp_path / "cloned"
    ws.mkdir()
    (ws / "node.config").write_text("{}", encoding="utf-8")
    proj = projects.Project("etl", "ETL", "", "", str(ws))
    agent = types.SimpleNamespace(name="a", persona="")
    monkeypatch.setattr(bot, "resolve_project", lambda k: proj)
    monkeypatch.setattr(bot, "resolve_agent", lambda p, n: agent)
    monkeypatch.setattr(bot, "resolve_agent_by_intent", lambda p, t, a: a)

    async def fake_invoke(*a, **kw):
        return None

    monkeypatch.setattr(bot, "_invoke_and_reply", fake_invoke)
    seen = {}

    class _File:
        async def download_to_drive(self, path):
            seen["path"] = path

    async def get_file(**kw):
        seen.update(kw)
        return _File()

    doc = types.SimpleNamespace(file_name="dispensa.pdf", get_file=get_file)
    upd, _ = _update(document=doc, caption="dispensa")
    monkeypatch.setattr(bot, "_to_local", lambda d: dt.datetime(2026, 10, 25))
    asyncio.run(bot.handle_project_group_message(upd, None, project_key="etl"))

    assert seen.get("read_timeout") == bot.LESSON_GET_FILE_TIMEOUT_S
    assert seen["path"].endswith("dispensa.pdf")
