# tests/test_voice_inbox.py
"""Voice note -> transcription -> the node's .brain-inbox (issue #56).

The row must be byte-compatible with what the /idea skill writes: the digest
reads both, and a field named differently would be dropped without an error.
"""
import asyncio
import datetime as dt
import json
import pathlib
import types

import pytest

import bot
import projects
import voice_inbox


def _proj(key, label, ws, aliases=()):
    return projects.Project(key, label, "", f"/host/{label.split()[-1]}", str(ws), tuple(aliases))


# --- pure part ---------------------------------------------------------------
def test_row_has_the_idea_skill_format(tmp_path):
    fixed = dt.datetime(2026, 9, 25, 17, 30, tzinfo=dt.UTC)
    path = voice_inbox.append_idea(str(tmp_path), "Home", "comprare le lampadine", "tg:1:2",
                                   now=lambda: fixed)
    assert path == tmp_path / ".brain-inbox" / "idee.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row == {
        "ts": "2026-09-25T17:30:00Z",
        "session": "tg:1:2",
        "nodo": "Home",
        "marcatore": "vocale-telegram",
        "testo": "comprare le lampadine",
    }


def test_rows_are_appended_not_overwritten(tmp_path):
    voice_inbox.append_idea(str(tmp_path), "Life", "uno", "s")
    voice_inbox.append_idea(str(tmp_path), "Life", "due", "s")
    lines = (tmp_path / ".brain-inbox" / "idee.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["testo"] for line in lines] == ["uno", "due"]


@pytest.fixture
def names(tmp_path):
    projs = [_proj("home", "🏠 Home", tmp_path), _proj("offerte", "Hub Offerte", tmp_path, ["hub"])]
    return voice_inbox.node_names(projs, voice_inbox.parse_aliases("casa=home, lavoro = offerte"))


@pytest.mark.parametrize("text,expected", [
    ("Casa, comprare le lampadine", ("home", "comprare le lampadine")),
    ("home: comprare le lampadine", ("home", "comprare le lampadine")),
    ("Hub Offerte. rivedere il listino", ("offerte", "rivedere il listino")),
    ("hub, rivedere il listino", ("offerte", "rivedere il listino")),
    ("Lavoro - chiamare Finelli", ("offerte", "chiamare Finelli")),
])
def test_a_leading_node_name_routes(names, text, expected):
    assert voice_inbox.split_named_node(text, names) == expected


@pytest.mark.parametrize("text", [
    # a node word without punctuation is just the start of a sentence
    "Casa nuova da cercare a Udine",
    # punctuation after a word that names nothing
    "Ciao, ricordami di comprare il latte",
    "",
])
def test_no_name_no_reroute(names, text):
    assert voice_inbox.split_named_node(text, names) == (None, text.strip())


def test_node_dir_name_prefers_the_host_directory():
    p = projects.Project("home", "Home", "", "repos/Home", "/workspaces/home")
    assert voice_inbox.node_dir_name(p) == "Home"
    p = projects.Project("home", "Home", "", "", "/workspaces/home")
    assert voice_inbox.node_dir_name(p) == "home"


# --- handler -----------------------------------------------------------------
def _update(chat_id, chat_type, msg_id=7, thread=None):
    replies = []

    async def download_to_drive(path):
        with open(path, "wb") as fh:
            fh.write(b"OggS")

    async def get_file():
        return types.SimpleNamespace(download_to_drive=download_to_drive)

    msg = types.SimpleNamespace(
        voice=types.SimpleNamespace(get_file=get_file),
        message_id=msg_id,
        message_thread_id=thread,
        date=None,
    )

    async def reply_text(t, **kw):
        replies.append(t)

    msg.reply_text = reply_text
    upd = types.SimpleNamespace(
        effective_message=msg,
        effective_chat=types.SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=types.SimpleNamespace(id=999),
    )
    return upd, replies


@pytest.fixture
def wired(monkeypatch, tmp_path):
    home_ws = tmp_path / "Home"
    life_ws = tmp_path / "Life"
    for ws in (home_ws, life_ws):
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("x", encoding="utf-8")  # a real checkout
    instance = tmp_path / "life-personal"
    instance.mkdir()
    (instance / "instance.config.json").write_text("{}", encoding="utf-8")
    home = _proj("home", "Home", home_ws)
    life = _proj("life", "Life", life_ws)
    by_key = {"home": home, "life": life}
    monkeypatch.setattr(bot, "_profile_or_guest", lambda u: types.SimpleNamespace(admin=True))
    monkeypatch.setattr(bot, "_is_stale", lambda m: False)
    monkeypatch.setattr(bot, "can_access", lambda p, k: True)
    monkeypatch.setattr(bot, "_served_by_another_bot", lambda p, b: None)
    monkeypatch.setattr(bot, "_current_bot_name", lambda c: "coordinator")
    monkeypatch.setattr(bot, "list_projects", lambda: [home, life])
    monkeypatch.setattr(bot, "resolve_project", lambda k: by_key.get((k or "").lower()))
    monkeypatch.setattr(bot, "_topic_binding",
                        lambda c, t: (home, types.SimpleNamespace(name="g")) if c == -100 else (None, None))
    monkeypatch.setenv(voice_inbox.ALIASES_ENV, "casa=home")
    monkeypatch.setenv(voice_inbox.INSTANCE_ENV, str(instance))
    monkeypatch.delenv(voice_inbox.DEFAULT_NODE_ENV, raising=False)
    said = {"text": ""}
    monkeypatch.setattr(bot, "_transcribe", lambda path: said["text"])
    pushes = []
    push_ok = {"value": True}

    def fake_publish(inst, nodo):
        pushes.append((inst, nodo))
        return push_ok["value"]

    monkeypatch.setattr(voice_inbox, "publish", fake_publish)
    return types.SimpleNamespace(home=home_ws, life=life_ws, instance=instance, said=said,
                                 pushes=pushes, push_ok=push_ok)


def _rows(ws):
    p = ws / ".brain-inbox" / "idee.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_group_voice_lands_in_the_instance_queue_tagged_with_the_node(wired):
    wired.said["text"] = "Le lampadine del bagno sono da cambiare"
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    rows = _rows(wired.instance)
    assert [(r["nodo"], r["testo"]) for r in rows] == [
        ("Home", "Le lampadine del bagno sono da cambiare")
    ]
    assert rows[0]["marcatore"] == "vocale-telegram"
    assert rows[0]["session"] == "telegram:-100:7"
    # The node's own queue is the fallback, not the destination.
    assert _rows(wired.home) == []
    assert wired.pushes == [(wired.instance, "Home")]
    assert "lampadine" in replies[0] and "life-personal" in replies[0] and "Home" in replies[0]


def test_direct_chat_defaults_to_life(wired):
    wired.said["text"] = "Un'idea sul sistema di notifiche"
    upd, replies = _update(42, "private")
    asyncio.run(bot.handle_voice(upd, None))
    assert [(r["nodo"], r["testo"]) for r in _rows(wired.instance)] == [
        ("Life", "Un'idea sul sistema di notifiche")
    ]


def test_direct_chat_honours_a_named_node(wired):
    wired.said["text"] = "Casa, comprare le lampadine"
    upd, replies = _update(42, "private")
    asyncio.run(bot.handle_voice(upd, None))
    assert [(r["nodo"], r["testo"]) for r in _rows(wired.instance)] == [
        ("Home", "comprare le lampadine")
    ]


def test_a_node_not_cloned_here_still_reaches_the_instance(wired, tmp_path, monkeypatch):
    """The queue lives in the instance: the node's repo is not needed on this device."""
    empty = tmp_path / "ws-placeholder"
    empty.mkdir()
    (empty / ".gitkeep").write_text("", encoding="utf-8")
    ghost = projects.Project("home", "Home", "", "repos/Home", str(empty))
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: (ghost, None))
    wired.said["text"] = "idea da non perdere"
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert [r["nodo"] for r in _rows(wired.instance)] == ["Home"]
    assert not (empty / ".brain-inbox").exists()


def test_a_failed_push_says_the_row_is_only_local(wired):
    wired.push_ok["value"] = False
    wired.said["text"] = "idea offline"
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert [r["testo"] for r in _rows(wired.instance)] == ["idea offline"]
    assert "solo su questo device" in replies[0]


def test_without_an_instance_the_node_queue_is_the_fallback(wired, monkeypatch):
    monkeypatch.delenv(voice_inbox.INSTANCE_ENV)
    wired.said["text"] = "idea senza istanza"
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert [r["testo"] for r in _rows(wired.home)] == ["idea senza istanza"]
    assert wired.pushes == []
    assert "nessuna istanza" in replies[0]


def test_a_placeholder_mount_is_not_an_instance(tmp_path):
    ph = tmp_path / "ws-placeholder"
    ph.mkdir()
    assert voice_inbox.instance_dir({"LIFE_INSTANCE_DIR": str(ph)}) is None
    assert voice_inbox.instance_dir({}) is None
    (ph / "instance.config.json").write_text("{}", encoding="utf-8")
    assert voice_inbox.instance_dir({"LIFE_INSTANCE_DIR": str(ph)}) == ph


def test_no_instance_and_no_checkout_keeps_the_text_in_the_reply(wired, tmp_path, monkeypatch):
    monkeypatch.delenv(voice_inbox.INSTANCE_ENV)
    empty = tmp_path / "ws-placeholder"
    empty.mkdir()
    (empty / ".gitkeep").write_text("", encoding="utf-8")
    monkeypatch.setattr(bot, "_topic_binding", lambda c, t: (_proj("home", "Home", empty), None))
    wired.said["text"] = "idea da non perdere"
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert not (empty / ".brain-inbox").exists()
    assert "idea da non perdere" in replies[0] and "non è" in replies[0]


def test_unbound_group_is_told_and_nothing_is_transcribed(wired, monkeypatch):
    called = []
    monkeypatch.setattr(bot, "_transcribe", lambda p: called.append(p) or "x")
    upd, replies = _update(-555, "group")
    asyncio.run(bot.handle_voice(upd, None))
    assert called == []
    assert "non è legata" in replies[0]


def test_silence_is_not_queued(wired):
    wired.said["text"] = ""
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert _rows(wired.instance) == []
    assert "Non ho sentito" in replies[0]


def test_a_failed_transcription_is_said_not_swallowed(wired, monkeypatch):
    def boom(path):
        raise RuntimeError("model missing")

    monkeypatch.setattr(bot, "_transcribe", boom)
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert _rows(wired.instance) == []
    assert "Non sono riuscito" in replies[0]


# --- publish -----------------------------------------------------------------
def test_publish_commits_only_the_queue_then_pushes(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    assert voice_inbox.publish(tmp_path, "Home", run=run) is True
    verbs = [c[3] for c in calls]
    assert verbs == ["add", "commit", "pull", "push"]
    commit = calls[1]
    assert commit[-2:] == ["--", ".brain-inbox/idee.jsonl"], "commit must be path-limited"


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:ownerowner/life-personal.git", "life-personal"),
    ("https://github.com/ownerowner/life-personal.git", "life-personal"),
    ("https://github.com/x/life-work", "life-work"),
])
def test_instance_name_comes_from_the_remote_not_the_mount(url, expected):
    def run(args):
        return types.SimpleNamespace(returncode=0, stdout=url + "\n", stderr="")

    assert voice_inbox.instance_name(pathlib.Path("/life-instance"), run=run) == expected


def test_instance_name_falls_back_to_the_directory():
    def run(args):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    assert voice_inbox.instance_name(pathlib.Path("/x/life-personal"), run=run) == "life-personal"


def test_publish_stops_and_reports_on_the_first_failure(tmp_path):
    calls = []

    def run(args):
        calls.append(args[3])
        return types.SimpleNamespace(returncode=1 if args[3] == "pull" else 0,
                                     stdout="", stderr="no network")

    assert voice_inbox.publish(tmp_path, "Home", run=run) is False
    assert calls == ["add", "commit", "pull"]


def test_voice_handler_runs_before_the_dedicated_group_catch_all(monkeypatch):
    """The group loop's handler matches every non-command message: registered
    after it, handle_voice would never see a note from a dedicated group."""
    from telegram.ext import Application

    monkeypatch.setattr(bot.manifest, "group_bindings", lambda: [(-100, 0, "home", "g")])
    app = Application.builder().token("1:x").build()
    bot._register_default_handlers(app)
    callbacks = [getattr(h.callback, "func", h.callback) for h in app.handlers[0]]
    assert callbacks.index(bot.handle_voice) < callbacks.index(bot.handle_project_group_message)


def test_ricorda_queues_typed_text_like_a_voice_note(wired):
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.cmd_ricorda(upd, types.SimpleNamespace(args=["Cambiare", "le", "lampadine"])))
    rows = _rows(wired.instance)
    assert [(r["nodo"], r["testo"], r["marcatore"]) for r in rows] == [
        ("Home", "Cambiare le lampadine", "testo-telegram")
    ]
    assert wired.pushes == [(wired.instance, "Home")]
    assert replies[0].startswith("📝") and "🎙" not in replies[0]


def test_ricorda_in_private_honours_a_named_node(wired):
    upd, _ = _update(5, "private")
    asyncio.run(bot.cmd_ricorda(upd, types.SimpleNamespace(args=["Casa,", "compra", "il", "latte"])))
    assert [(r["nodo"], r["testo"]) for r in _rows(wired.instance)] == [("Home", "compra il latte")]


def test_ricorda_without_text_explains_usage_and_queues_nothing(wired):
    upd, replies = _update(5, "private")
    asyncio.run(bot.cmd_ricorda(upd, types.SimpleNamespace(args=[])))
    assert _rows(wired.instance) == [] and "Uso:" in replies[0]


# --- #78: the queue of the chat's instance, written by the host ------------------


@pytest.fixture
def via_host(wired, monkeypatch):
    """Host bridge on: record what the bot asks the host to queue."""
    import instance_link

    asked = []
    answer = {"res": instance_link.LinkResult(True, True, True, "life-x")}

    async def fake_queue(iid, nodo, testo, session, marker):
        asked.append((iid, nodo, testo, session, marker))
        return answer["res"]

    monkeypatch.setattr(bot.claude_runner, "host_exec_enabled", lambda: True)
    monkeypatch.setattr(instance_link, "queue_idea", fake_queue)
    monkeypatch.setattr(bot, "_legacy_instance_id", lambda: "life-personal")
    wired.asked = asked
    wired.answer = answer
    return wired


def test_group_voice_goes_to_the_rooms_instance_through_the_host(via_host, monkeypatch):
    monkeypatch.setattr(bot, "_binding_instance", lambda c, t: "comunity")
    via_host.said["text"] = "idea per il team"
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert via_host.asked == [
        ("comunity", "Home", "idea per il team", "telegram:-100:7", "vocale-telegram")
    ]
    # Nothing written in the mounted (other) instance, nor in the node.
    assert _rows(via_host.instance) == [] and _rows(via_host.home) == []
    assert "comunity" in replies[0]


def test_private_note_goes_to_the_named_nodes_instance(via_host, monkeypatch):
    monkeypatch.setattr(bot, "_routing_index", lambda: {"home": ("personal",)})
    via_host.said["text"] = "Casa, comprare le lampadine"
    upd, replies = _update(42, "private")
    asyncio.run(bot.handle_voice(upd, None))
    assert [(a[0], a[1], a[2]) for a in via_host.asked] == [
        ("personal", "Home", "comprare le lampadine")
    ]


def test_private_note_of_an_ambiguous_node_takes_the_device_default(via_host, monkeypatch):
    import instances

    monkeypatch.setattr(bot, "_routing_index", lambda: {"life": ("personal", "acme")})
    monkeypatch.setattr(instances, "default_instance_id", lambda env=None: "acme")
    via_host.said["text"] = "un'idea"
    upd, replies = _update(42, "private")
    asyncio.run(bot.handle_voice(upd, None))
    assert via_host.asked[0][0] == "acme"


def test_host_written_but_not_pushed_says_only_local(via_host, monkeypatch):
    import instance_link

    monkeypatch.setattr(bot, "_binding_instance", lambda c, t: "comunity")
    via_host.answer["res"] = instance_link.LinkResult(True, True, False, "life-comunity")
    via_host.said["text"] = "idea offline"
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert "solo su questo device" in replies[0] and "comunity" in replies[0]


def test_host_failure_never_files_the_idea_in_another_instance(via_host, monkeypatch):
    import instance_link

    monkeypatch.setattr(bot, "_binding_instance", lambda c, t: "comunity")
    via_host.answer["res"] = instance_link.LinkResult(False, False, False, "ssh giu'")
    via_host.said["text"] = "idea del team"
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert _rows(via_host.instance) == []  # the mounted one is life-personal
    assert [r["testo"] for r in _rows(via_host.home)] == ["idea del team"]


def test_host_failure_falls_back_to_the_mount_of_the_same_instance(via_host, monkeypatch):
    import instance_link

    monkeypatch.setattr(bot, "_binding_instance", lambda c, t: "life-personal")
    via_host.answer["res"] = instance_link.LinkResult(False, False, False, "ssh giu'")
    via_host.said["text"] = "idea personale"
    upd, replies = _update(-100, "supergroup")
    asyncio.run(bot.handle_voice(upd, None))
    assert [r["testo"] for r in _rows(via_host.instance)] == ["idea personale"]
