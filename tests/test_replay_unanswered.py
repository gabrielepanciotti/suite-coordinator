"""Selection of messages to replay after an outage or a failed turn."""

import datetime as dt
import json

import replay_unanswered as ru

NOW = dt.datetime(2026, 10, 9, 14, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))
CHAT = -1003875691522


def _msg(mid, text, *, minutes_ago=60, topic=6, **kw):
    ts = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    rec = {
        "ts": ts, "chat": CHAT, "chat_tipo": "supergroup", "topic": topic,
        "msg_id": mid, "da_id": 1087968824, "da": "Group", "tipo": "testo",
        "testo": text, "modificato": False,
    }
    rec.update(kw)
    return rec


def _turn(prompt, *, minutes_ago=55, ok=True, out="fatto", topic=6):
    ts = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    return {"ts": ts, "chat": CHAT, "topic": topic, "ok": ok, "in": prompt, "out": out}


def _ids(selected):
    return [m["msg_id"] for m in selected]


def test_answered_ok_is_not_replayed():
    msgs = [_msg(1, "ciao, come va?")]
    turns = [_turn("ciao, come va?")]
    assert ru.select_unanswered(msgs, turns, now=NOW) == []


def test_answered_with_wrapped_prompt_and_extra_whitespace():
    msgs = [_msg(1, "Quando provo  a loggare\ncon google")]
    turns = [_turn("[CMD:/group-message] mittente: X. Testo: quando provo a loggare con google")]
    assert ru.select_unanswered(msgs, turns, now=NOW) == []


def test_model_prefix_stripped_from_prompt_still_matches():
    msgs = [_msg(1, "opus/high: rifai il report")]
    turns = [_turn("rifai il report")]
    assert ru.select_unanswered(msgs, turns, now=NOW) == []


def test_failed_turn_is_replayed():
    msgs = [_msg(137, "Quando provo a loggare con il mio account google")]
    turns = [_turn("Quando provo a loggare con il mio account google", ok=False,
                   out="❌ opencode: Unexpected server error.")]
    assert _ids(ru.select_unanswered(msgs, turns, now=NOW)) == [137]


def test_ok_flag_but_error_marker_or_empty_out_counts_as_failed():
    msgs = [_msg(1, "uno"), _msg(2, "due")]
    turns = [_turn("uno", out="⏱ timeout"), _turn("due", out="   ")]
    assert _ids(ru.select_unanswered(msgs, turns, now=NOW)) == [1, 2]


def test_no_turn_is_replayed():
    msgs = [_msg(5, "c'è nessuno?")]
    assert _ids(ru.select_unanswered(msgs, [], now=NOW)) == [5]


def test_turn_before_message_does_not_count():
    msgs = [_msg(5, "ripeti", minutes_ago=30)]
    turns = [_turn("ripeti", minutes_ago=40)]
    assert _ids(ru.select_unanswered(msgs, turns, now=NOW)) == [5]


def test_turn_on_other_topic_does_not_count():
    msgs = [_msg(5, "ciao", topic=6)]
    turns = [_turn("ciao", topic=7)]
    assert _ids(ru.select_unanswered(msgs, turns, now=NOW)) == [5]


def test_out_of_window_is_ignored():
    msgs = [_msg(1, "vecchio", minutes_ago=13 * 60), _msg(2, "recente", minutes_ago=60)]
    assert _ids(ru.select_unanswered(msgs, [], now=NOW)) == [2]


def test_window_from_chat_capped_at_24h():
    msgs = [_msg(1, "ieri", minutes_ago=20 * 60), _msg(2, "l'altro ieri", minutes_ago=30 * 60)]
    out = ru.select_unanswered(msgs, [], now=NOW, window_for=lambda _c: 72 * 3600)
    assert _ids(out) == [1]


def test_too_fresh_is_left_to_normal_pipeline():
    msgs = [_msg(1, "appena scritto", minutes_ago=1)]
    assert ru.select_unanswered(msgs, [], now=NOW) == []


def test_already_replayed_or_skipped_is_ignored():
    msgs = [_msg(1, "a"), _msg(2, "b"), _msg(3, "c"), _msg(4, "d")]
    state = {"items": {
        ru.msg_key(CHAT, 1): {"status": ru.DONE},
        ru.msg_key(CHAT, 2): {"status": ru.SKIPPED},
        ru.msg_key(CHAT, 3): {"status": ru.FAILED, "attempts": 2},
        ru.msg_key(CHAT, 4): {"status": ru.PENDING, "attempts": 1},
    }}
    # A pending (interrupted) attempt is still a candidate; the caller caps it.
    assert _ids(ru.select_unanswered(msgs, [], now=NOW, state=state)) == [4]


def test_commands_service_bots_channels_and_unbound_are_ignored():
    msgs = [
        _msg(1, "/status", tipo="comando"),
        _msg(2, "", tipo="servizio"),
        _msg(4, "dal bot", da_id=999),
        _msg(5, "in un canale", chat_tipo="channel"),
        _msg(6, "topic libero", topic=99),
        _msg(7, "prod: rilascia"),
        _msg(8, "valido"),
    ]
    out = ru.select_unanswered(
        msgs, [], now=NOW, bot_ids={999}, is_bound=lambda _c, t: t != 99
    )
    assert _ids(out) == [8]


# --- everything else: media, private chats, edits (2026-10-11) ----------------

CUTOVER = NOW - dt.timedelta(hours=6)
PRIV = 4242


def _raw(mid, **kw):
    base = {"message_id": mid, "date": 0, "chat": {"id": CHAT, "type": "supergroup"}}
    base.update(kw)
    return base


def _voice(mid, **kw):
    return _msg(mid, "", tipo="vocale",
                media={"tipo": "vocale", "file_id": f"F{mid}", "durata": 7},
                raw=_raw(mid, voice={"file_id": f"F{mid}", "file_unique_id": "u",
                                     "duration": 7}), **kw)


def test_voice_without_answer_is_replayed():
    out = ru.select_unanswered([_voice(20)], [], now=NOW, all_since=CUTOVER)
    assert _ids(out) == [20]
    assert ru.is_recoverable(out[0])
    assert ru.build_raw(out[0])["voice"]["file_id"] == "F20"


def test_voice_handled_without_turn_is_not_replayed():
    out = ru.select_unanswered([_voice(20)], [], now=NOW, all_since=CUTOVER,
                               handled={ru.msg_key(CHAT, 20)})
    assert out == []


def test_photo_with_caption_answered_by_msg_id_turn():
    photo = _msg(21, "scontrino di ieri", tipo="foto",
                 raw=_raw(21, photo=[{"file_id": "P", "file_unique_id": "p",
                                      "width": 1, "height": 1}],
                          caption="scontrino di ieri"))
    assert _ids(ru.select_unanswered([photo], [], now=NOW, all_since=CUTOVER)) == [21]
    turn = dict(_turn("[CMD:/dispensa] /ws/raw/telegram/x.jpg", minutes_ago=50), msg_id=21)
    assert ru.select_unanswered([photo], [turn], now=NOW, all_since=CUTOVER) == []


def test_legacy_photo_turn_matched_by_saved_file_name():
    photo = _msg(22, "", tipo="foto", raw=_raw(22, photo=[]))
    turn = _turn(f"[CMD:/dispensa] /ws/raw/telegram/2026-10-09_133000_{CHAT}_22.jpg")
    assert ru.select_unanswered([photo], [turn], now=NOW, all_since=CUTOVER) == []


def test_album_answered_once_for_all_its_photos():
    a = _msg(30, "", tipo="foto", media_group_id="G", raw=_raw(30))
    b = _msg(31, "", tipo="foto", media_group_id="G", raw=_raw(31))
    out = ru.select_unanswered([a, b], [], now=NOW, all_since=CUTOVER)
    assert [[m["msg_id"] for m in u] for u in ru.group_albums(out)] == [[30, 31]]
    turn = dict(_turn("album", minutes_ago=50), msg_id=30)
    assert ru.select_unanswered([a, b], [turn], now=NOW, all_since=CUTOVER) == []


def test_recordings_in_one_album_are_replayed_one_by_one_and_one_answer_does_not_cover_the_other():
    """Due lezioni mandate nello stesso album sono due lavori: rispondere alla prima non chiude la seconda."""
    a = _msg(50, "Stili del tatuaggio", tipo="audio", media_group_id="L", raw=_raw(50))
    b = _msg(51, "", tipo="audio", media_group_id="L", raw=_raw(51))
    turn = dict(_turn("lezione", minutes_ago=50), msg_id=50)
    out = ru.select_unanswered([a, b], [turn], now=NOW, all_since=CUTOVER)
    assert _ids(out) == [51]
    both = ru.select_unanswered([a, b], [], now=NOW, all_since=CUTOVER)
    assert [[m["msg_id"] for m in u] for u in ru.group_albums(both)] == [[50], [51]]


def test_private_message_without_answer_is_replayed():
    dm = _msg(40, "ricordami il dentista", chat=PRIV, chat_tipo="private", topic=None)
    out = ru.select_unanswered([dm], [], now=NOW, all_since=CUTOVER)
    assert _ids(out) == [40]
    raw = ru.build_raw(out[0])  # old-format text row: rebuilt from its fields
    assert raw["chat"] == {"id": PRIV, "type": "private"}
    assert raw["text"] == "ricordami il dentista"
    assert raw["from"]["id"] == 1087968824


def test_private_message_answered_is_not_replayed():
    dm = _msg(40, "ricordami il dentista", chat=PRIV, chat_tipo="private", topic=None)
    turn = dict(_turn("ricordami il dentista", topic=0), chat=PRIV)
    assert ru.select_unanswered([dm], [turn], now=NOW, all_since=CUTOVER) == []


def test_edited_message_counts_once_with_its_latest_version():
    orig = _msg(50, "compra il latte", minutes_ago=60)
    edit = _msg(50, "compra il latte e il pane", minutes_ago=58, modificato=True)
    out = ru.select_unanswered([edit, orig], [], now=NOW)
    assert _ids(out) == [50]
    assert out[0]["testo"] == "compra il latte e il pane"
    assert out[0]["ts"] == orig["ts"]  # dated when it was first sent


def test_edit_after_answer_does_not_trigger_replay():
    orig = _msg(51, "compra il latte", minutes_ago=60)
    edit = _msg(51, "compra il latte!", minutes_ago=30, modificato=True)
    turns = [_turn("compra il latte", minutes_ago=59)]
    assert ru.select_unanswered([orig, edit], turns, now=NOW) == []


def test_already_recovered_media_is_not_selected_again():
    state = {"items": {ru.msg_key(CHAT, 20): {"status": ru.DONE}}}
    assert ru.select_unanswered([_voice(20)], [], now=NOW, all_since=CUTOVER,
                                state=state) == []


def test_media_with_file_is_kept_past_the_chat_window():
    old = _voice(20, minutes_ago=13 * 60)
    out = ru.select_unanswered([old], [], now=NOW, all_since=NOW - dt.timedelta(days=2))
    assert _ids(out) == [20]


def test_media_and_private_before_cutover_are_left_alone():
    """Before the cutover no file_id was archived: those rows can't be judged."""
    v = _voice(20, minutes_ago=7 * 60)
    dm = _msg(41, "ciao", chat=PRIV, chat_tipo="private", minutes_ago=7 * 60)
    txt = _msg(42, "testo di gruppo", minutes_ago=7 * 60)
    assert _ids(ru.select_unanswered([v, dm, txt], [], now=NOW, all_since=CUTOVER)) == [42]


def test_old_format_media_is_selected_but_not_recoverable():
    """A peer still on the old code archived a voice note with no file_id: it
    is selected, so the bot can say in the thread that it must be resent."""
    legacy = _msg(23, "", tipo="vocale")  # no raw, no media
    out = ru.select_unanswered([legacy], [], now=NOW, all_since=CUTOVER)
    assert _ids(out) == [23]
    assert not ru.is_recoverable(out[0])
    assert ru.build_raw(out[0]) is None


def test_cutover_is_the_latest_device_and_survives_merge(tmp_path):
    state = {"items": {}}
    t1 = ru.cutover(state, "pc", now=NOW - dt.timedelta(hours=2))
    t2 = ru.cutover(state, "minipc", now=NOW)
    assert t1 == NOW - dt.timedelta(hours=2) and t2 == NOW
    # A device keeps its first mark.
    assert ru.cutover(state, "pc", now=NOW + dt.timedelta(days=1)) == NOW
    path = tmp_path / "replayed.json"
    ru.save_state(str(path), state, now=NOW)
    assert ru.load_state(str(path))["since_all"].keys() == {"pc", "minipc"}


def test_duplicates_from_conflict_copies_count_once_and_sorted():
    a, b = _msg(2, "secondo", minutes_ago=30), _msg(1, "primo", minutes_ago=90)
    assert _ids(ru.select_unanswered([a, b, a, b], [], now=NOW)) == [1, 2]


def test_state_roundtrip_merges_conflict_copies(tmp_path):
    path = tmp_path / "replayed.json"
    k = ru.msg_key(CHAT, 137)
    path.write_text(json.dumps({"items": {k: {"status": ru.PENDING, "attempts": 1}}}))
    (tmp_path / "replayed.sync-conflict-20261009-1-ABC.json").write_text(
        json.dumps({"items": {k: {"status": ru.SKIPPED, "attempts": 0,
                                  "at": NOW.isoformat()}}})
    )
    state = ru.load_state(str(path))
    assert state["items"][k]["status"] == ru.SKIPPED
    assert state["items"][k]["attempts"] == 1
    ru.mark(state, ru.msg_key(CHAT, 1), status=ru.DONE, now=NOW, attempts=1)
    ru.save_state(str(path), state, now=NOW)
    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["items"][ru.msg_key(CHAT, 1)]["status"] == ru.DONE


def test_read_jsonl_includes_conflict_copy_and_filters_by_since(tmp_path):
    main = tmp_path / "conversations.jsonl"
    main.write_text(json.dumps(_turn("x", minutes_ago=10)) + "\n", encoding="utf-8")
    (tmp_path / "conversations.sync-conflict-20261009-145955-7RM4QUO.jsonl").write_text(
        json.dumps(_turn("y", minutes_ago=10)) + "\nnot json\n"
        + json.dumps(_turn("old", minutes_ago=48 * 60)) + "\n",
        encoding="utf-8",
    )
    rows = ru.read_jsonl(str(main), since=NOW - dt.timedelta(hours=24))
    assert sorted(r["in"] for r in rows) == ["x", "y"]


def test_replay_prompt_contains_text_so_its_turn_matches():
    m = _msg(9, "Correggi il login")
    turns = [_turn(ru.build_prompt(m), minutes_ago=5)]
    assert ru.select_unanswered([m], turns, now=NOW) == []


# --- periodic job wiring (bot.py) ---------------------------------------------


class _Manager:
    def __init__(self, polling):
        self.is_polling = polling


async def test_periodic_tick_runs_only_where_polling(monkeypatch):
    import bot

    calls = []

    async def fake_replay():
        calls.append(1)
        return 1

    monkeypatch.setattr(bot, "_replay_unanswered", fake_replay)
    monkeypatch.setitem(bot._OWNERSHIP, "manager", _Manager(False))
    assert await bot._replay_tick() == 0
    assert calls == []
    monkeypatch.setitem(bot._OWNERSHIP, "manager", _Manager(True))
    assert await bot._replay_tick() == 1
    assert calls == [1]


async def test_periodic_tick_never_raises(monkeypatch):
    import bot

    async def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(bot, "_replay_unanswered", boom)
    monkeypatch.setitem(bot._OWNERSHIP, "manager", _Manager(True))
    assert await bot._replay_tick() == 0


def test_job_registered_every_ten_minutes_in_existing_scheduler(monkeypatch):
    import bot
    import scheduler

    jobs = {}

    class FakeSched:
        def add_job(self, func, trigger, id, **kw):
            jobs[id] = (func, trigger, kw)

    monkeypatch.setitem(scheduler._S, "sched", FakeSched())
    monkeypatch.setattr(bot, "REPLAY_ENABLED", True)
    monkeypatch.setattr(bot, "REPLAY_INTERVAL_MIN", 10)
    bot._register_replay_job()
    func, trigger, kw = jobs[bot.REPLAY_JOB_ID]
    assert func is bot._replay_tick
    assert trigger.interval == dt.timedelta(minutes=10)
    assert kw["max_instances"] == 1 and kw["replace_existing"]


# --- the replay loop (bot.py): re-dispatch through the live handlers ----------


class _FakeBot:
    id = 777

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class _FakeApp:
    """Stands in for a PTB Application: records what went back through it and
    plays the handler's part by filling the replay outcome."""

    def __init__(self, outcomes):
        self.bot = _FakeBot()
        self.outcomes = list(outcomes)
        self.updates = []

    async def process_update(self, update):
        import bot

        self.updates.append(update)
        holder = bot._REPLAY.get()
        assert holder is not None, "replay context must be set for the handlers"
        holder["outcome"] = self.outcomes.pop(0) if self.outcomes else None


def _wire(monkeypatch, tmp_path, rows, outcomes):
    import bot

    log = tmp_path / "messages.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(bot, "MSG_LOG_PATH", str(log))
    monkeypatch.setattr(bot, "CONV_LOG_PATH", str(tmp_path / "conversations.jsonl"))
    monkeypatch.setattr(bot, "HANDLED_LOG_PATH", str(tmp_path / "handled.jsonl"))
    monkeypatch.setattr(bot, "REPLAY_STATE_PATH", str(tmp_path / "replayed.json"))
    monkeypatch.setattr(bot, "REPLAY_ENABLED", True)
    monkeypatch.setattr(bot, "_REPLAY_LOCK", None)
    monkeypatch.setattr(bot, "_replay_window", lambda _c: ru.DEFAULT_WINDOW_SEC)
    monkeypatch.setattr(bot, "_replay_bound", lambda _c, _t: True)
    monkeypatch.setattr(bot, "_replay_route", lambda rec: "default")
    monkeypatch.setattr(bot, "_polls_bot", lambda _n: True)
    # The cutover was recorded long ago: every row of the test is past it.
    (tmp_path / "replayed.json").write_text(
        json.dumps({"items": {},
                    "since_all": {bot._replay_device(): "2000-01-01T00:00:00+00:00"}}),
        encoding="utf-8",
    )
    app = _FakeApp(outcomes)
    monkeypatch.setattr(bot, "_BOT", app.bot)
    monkeypatch.setattr(bot, "_BOTS", {"default": app.bot})
    monkeypatch.setattr(bot, "_APPS", {"default": app})
    return bot, app


def _now_rows(*rows):
    """Rows of the helpers above, re-dated relative to the real clock."""
    now = dt.datetime.now(dt.UTC)
    out = []
    for r in rows:
        r = dict(r)
        delta = NOW - dt.datetime.fromisoformat(r["ts"])
        r["ts"] = (now - delta).isoformat(timespec="seconds")
        out.append(r)
    return out


async def test_loop_replays_voice_through_handlers_and_marks_done(monkeypatch, tmp_path):
    bot, app = _wire(monkeypatch, tmp_path, _now_rows(_voice(20)), ["ok"])
    assert await bot._replay_unanswered() == 1
    (upd,) = app.updates
    assert upd.message.voice.file_id == "F20"
    assert upd.message.message_id == 20
    state = ru.load_state(str(tmp_path / "replayed.json"))
    assert state["items"][ru.msg_key(CHAT, 20)]["status"] == ru.DONE
    # Second scan: nothing left to do, no loop.
    assert await bot._replay_unanswered() == 0
    assert len(app.updates) == 1


async def test_loop_retries_then_posts_one_failure_note(monkeypatch, tmp_path):
    bot, app = _wire(monkeypatch, tmp_path, _now_rows(_voice(20)), ["failed", "failed"])
    assert await bot._replay_unanswered() == 0
    assert len(app.updates) == ru.MAX_ATTEMPTS
    ((chat, text, kw),) = app.bot.sent
    assert chat == CHAT and text == bot.REPLAY_FAILED_MSG
    assert kw["reply_parameters"].message_id == 20
    assert kw["message_thread_id"] == 6


async def test_loop_old_format_media_asks_to_resend_in_thread(monkeypatch, tmp_path):
    bot, app = _wire(monkeypatch, tmp_path, _now_rows(_msg(23, "", tipo="vocale")), [])
    assert await bot._replay_unanswered() == 0
    assert app.updates == []
    ((chat, text, kw),) = app.bot.sent
    assert text == bot.REPLAY_LOST_MSG and kw["reply_parameters"].message_id == 23
    state = ru.load_state(str(tmp_path / "replayed.json"))
    assert state["items"][ru.msg_key(CHAT, 23)]["status"] == ru.LOST


async def test_loop_silent_handler_is_skipped_not_failed(monkeypatch, tmp_path):
    bot, app = _wire(monkeypatch, tmp_path, _now_rows(_voice(20)), [None])
    assert await bot._replay_unanswered() == 0
    assert app.bot.sent == []
    state = ru.load_state(str(tmp_path / "replayed.json"))
    assert state["items"][ru.msg_key(CHAT, 20)]["status"] == ru.SKIPPED


async def test_loop_leaves_messages_of_a_bot_polled_elsewhere(monkeypatch, tmp_path):
    bot, app = _wire(monkeypatch, tmp_path, _now_rows(_voice(20)), ["ok"])
    monkeypatch.setattr(bot, "_polls_bot", lambda _n: False)
    assert await bot._replay_unanswered() == 0
    assert app.updates == [] and app.bot.sent == []


async def test_loop_private_text_goes_back_as_a_private_update(monkeypatch, tmp_path):
    dm = _msg(40, "ricordami il dentista", chat=PRIV, chat_tipo="private", topic=None)
    bot, app = _wire(monkeypatch, tmp_path, _now_rows(dm), ["ok"])
    assert await bot._replay_unanswered() == 1
    (upd,) = app.updates
    assert upd.effective_chat.type == "private"
    assert upd.message.text == "ricordami il dentista"


def test_archive_keeps_file_id_and_raw_of_a_real_voice_message(monkeypatch, tmp_path):
    """Round trip: what archive_message writes is what the replay rebuilds."""
    import asyncio

    from telegram import Chat, Message, Update, User, Voice

    import bot

    msg = Message(
        message_id=99,
        date=dt.datetime(2026, 10, 11, 10, 0, tzinfo=dt.UTC),
        chat=Chat(id=CHAT, type="supergroup", title="ComUnity", is_forum=True),
        from_user=User(id=5, first_name="Socio", is_bot=False),
        voice=Voice(file_id="AwACAg", file_unique_id="uq", duration=12,
                    mime_type="audio/ogg"),
        message_thread_id=6,
        is_topic_message=True,
    )
    dest = tmp_path / "messages.jsonl"
    monkeypatch.setattr(bot, "MSG_LOG_PATH", str(dest))
    asyncio.run(bot.archive_message(Update(update_id=1, message=msg), None))
    (row,) = [json.loads(r) for r in dest.read_text(encoding="utf-8").splitlines()]
    assert row["tipo"] == "vocale"
    assert row["media"]["file_id"] == "AwACAg" and row["media"]["durata"] == 12
    assert row["bot"] == "default"
    again = Update.de_json({"update_id": 0, "message": ru.build_raw(row)}, None)
    assert again.message.voice.file_id == "AwACAg"
    assert again.message.message_thread_id == 6


async def test_replay_unit_reaches_a_real_handler_with_context(monkeypatch):
    """A real PTB Application: the rebuilt update hits the registered handler,
    inside the replay context, and the old message is not dropped as stale."""
    from telegram.ext import Application, MessageHandler, filters

    import bot

    seen = []

    async def handler(update, _context):
        seen.append((update.message.voice.file_id, bot._is_stale(update.message)))
        bot._REPLAY.get()["outcome"] = "ok"

    app = Application.builder().token("1:fake").build()
    app.add_handler(MessageHandler(filters.VOICE, handler))
    app._initialized = True  # initialize() would call getMe on the network
    old =_voice(20, minutes_ago=600)
    old["raw"]["date"] = int((dt.datetime.now(dt.UTC) - dt.timedelta(hours=10)).timestamp())
    assert await bot._replay_unit(app, [old]) == "ok"
    assert seen == [("F20", False)]
    assert bot._REPLAY.get() is None  # reset after the unit


def test_turn_row_names_the_message_it_answers(monkeypatch, tmp_path):
    import bot

    dest = tmp_path / "conversations.jsonl"
    monkeypatch.setattr(bot, "CONV_LOG_PATH", str(dest))
    token = bot._TURN_MSG.set((CHAT, 77))
    try:
        bot._log_turn(chat_id=CHAT, topic_id=6, project_key="p", agent_name="a",
                      prompt="x", reply="y", resume_id=None, session_id=None,
                      elapsed_ms=1, ok=True)
    finally:
        bot._TURN_MSG.reset(token)
    (row,) = [json.loads(r) for r in dest.read_text(encoding="utf-8").splitlines()]
    assert row["msg_id"] == 77


def test_media_with_file_survives_past_24h_but_text_does_not():
    days3 = 3 * 24 * 60
    msgs = [_voice(30, minutes_ago=days3), _msg(31, "vecchio testo", minutes_ago=days3)]
    out = ru.select_unanswered(msgs, [], now=NOW, window_for=lambda _c: 72 * 3600,
                               all_since=CUTOVER - dt.timedelta(days=10))
    assert _ids(out) == [30]


def test_media_older_than_media_window_is_dropped():
    old = _voice(32, minutes_ago=7 * 24 * 60)
    out = ru.select_unanswered([old], [], now=NOW, all_since=CUTOVER - dt.timedelta(days=10))
    assert out == []
