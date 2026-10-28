# tests/test_album_debounce.py
"""A Telegram album (several photos sent together) must collapse into ONE agent
turn. Telegram delivers each photo as a separate update sharing a
``media_group_id``; without debounce the (chat, topic) session gets N serialized
turns that pile up and hang. ``handle_photo`` buffers the album and fires a
single ``_invoke_and_reply`` once the photos stop arriving."""
import asyncio
import types

import bot
import projects


def _photo_update(chat_id, msg_id, media_group_id=None, caption=None):
    """A minimal photo Update; the real download is monkeypatched away."""
    photo = types.SimpleNamespace(get_file=None)
    msg = types.SimpleNamespace(
        photo=[photo],
        media_group_id=media_group_id,
        caption=caption,
        message_id=msg_id,
        chat_id=chat_id,
        message_thread_id=None,
    )

    async def reply_text(text, **kw):
        return None

    msg.reply_text = reply_text
    return types.SimpleNamespace(
        effective_message=msg,
        effective_chat=types.SimpleNamespace(id=chat_id, type="group"),
        effective_user=types.SimpleNamespace(id=999),
        get_bot=lambda: None,
    )


def _wire(monkeypatch, tmp_path):
    # A workspace that actually holds a repo: the write path refuses to save a
    # photo into an un-cloned workspace (the shared .ws-placeholder).
    (tmp_path / "node.config").write_text("{}", encoding="utf-8")
    proj = projects.Project("homeProductFinder", "P", "P", "", str(tmp_path))
    agent = types.SimpleNamespace(name="a", persona="")
    monkeypatch.setattr(bot, "_profile", lambda u: types.SimpleNamespace(admin=True))
    monkeypatch.setattr(bot, "_is_stale", lambda m: False)
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: (proj, agent))
    monkeypatch.setattr(bot, "can_access", lambda p, k: True)

    async def fake_dl(msg, chat_id, project, ag, tid):
        return f"{project.workspace}/raw/telegram/{msg.message_id}.jpg"

    monkeypatch.setattr(bot, "_download_photo", fake_dl)

    calls = []

    async def fake_invoke(update, project, ag, prompt):
        calls.append({"prompt": prompt, "msg_id": update.effective_message.message_id})

    monkeypatch.setattr(bot, "_invoke_and_reply", fake_invoke)
    monkeypatch.setattr(bot, "ALBUM_DEBOUNCE_SEC", 0.05)
    bot._album_buffer.clear()
    return calls


def test_album_collapses_to_one_turn(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)

    async def scenario():
        for mid in (10, 11, 12):
            cap = "12,90 Action" if mid == 10 else None
            await bot.handle_photo(_photo_update(-5474171040, mid, "MG1", cap), None)
        await asyncio.sleep(0.2)  # let the debounced flush fire

    asyncio.run(scenario())

    assert len(calls) == 1, calls
    prompt = calls[0]["prompt"]
    assert "album di 3 foto" in prompt
    for mid in (10, 11, 12):
        assert f"{mid}.jpg" in prompt
    assert "12,90 Action" in prompt  # caption from the first photo survives
    assert calls[0]["msg_id"] == 10  # reply anchored to the album's first photo
    assert not bot._album_buffer  # buffer drained


def test_single_photo_runs_immediately(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)

    async def scenario():
        await bot.handle_photo(_photo_update(-5474171040, 20, None, "solo"), None)

    asyncio.run(scenario())

    assert len(calls) == 1, calls
    prompt = calls[0]["prompt"]
    assert prompt.startswith("[CMD:/dispensa] ")
    assert "20.jpg" in prompt
    assert "solo" in prompt
    assert "album" not in prompt


def test_two_albums_dont_mix(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)

    async def scenario():
        await bot.handle_photo(_photo_update(-5474171040, 1, "A", None), None)
        await bot.handle_photo(_photo_update(-5474171040, 2, "A", None), None)
        await bot.handle_photo(_photo_update(-5474171040, 3, "B", None), None)
        await asyncio.sleep(0.2)

    asyncio.run(scenario())

    assert len(calls) == 2, calls
    by_first = sorted(c["msg_id"] for c in calls)
    assert by_first == [1, 3]  # one turn per album, anchored on each first photo
    a = next(c for c in calls if c["msg_id"] == 1)
    assert "1.jpg" in a["prompt"] and "2.jpg" in a["prompt"] and "3.jpg" not in a["prompt"]
