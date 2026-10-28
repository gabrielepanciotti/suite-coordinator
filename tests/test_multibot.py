"""Multi-bot support: zero-regression contract, bot registry, manifest fields.

The first test in this file is the acceptance bar for the whole feature: with
no extra bots configured, ``main()`` must build ONE Application whose handlers
are, in order and filter-for-filter, the ones the single-bot Coordinator has
always registered. GOLDEN below was captured by running this same harness
against the pre-multi-bot ``bot.py``; every id in it is synthetic.
"""
from __future__ import annotations

import functools

import pytest

import bot
import bot_registry
import manifest
import ownership
import projects

# A chat id that cannot be anyone's real group: the signature must not depend on
# (nor leak) the device's actual Telegram configuration.
FAKE_CHAT = -1009990001
FAKE_PROJECT = projects.Project(
    key="zzz_fake",
    label="Fake",
    emoji="🧪",
    host_path="/hosts/zzz_fake",
    workspace="/workspaces/zzz_fake",
)

# Captured from bot.py BEFORE multi-bot support existed. Any drift here is a
# behaviour change on the live Coordinator bot and must be deliberate.
GOLDEN = [
    # Per-instance access through Life (#83): first on every bot.
    "-3|TypeHandler|life_access_gate",
    "0|CommandHandler|start|filters.UpdateType.MESSAGES|cmd_help",
    "0|CommandHandler|help|filters.UpdateType.MESSAGES|cmd_help",
    "0|CommandHandler|dashboard,menu,progetti|filters.UpdateType.MESSAGES|cmd_progetti",
    "0|CommandHandler|nuovo|filters.UpdateType.MESSAGES|cmd_nuovo",
    "0|CommandHandler|riprendi|filters.UpdateType.MESSAGES|cmd_riprendi",
    "0|CommandHandler|model,modello|filters.UpdateType.MESSAGES|cmd_modello",
    "0|CommandHandler|compara,compare|filters.UpdateType.MESSAGES|cmd_compara",
    "0|CommandHandler|opencodemodel|filters.UpdateType.MESSAGES|cmd_opencodemodel",
    "0|CommandHandler|chiudi|filters.UpdateType.MESSAGES|cmd_chiudi",
    "0|CommandHandler|reset|filters.UpdateType.MESSAGES|cmd_reset",
    # 2026-10-09: /new (fresh conversation), /argomenti (forum topics), the
    # drill-down menu taps and the topic-name learner.
    "0|CommandHandler|new|filters.UpdateType.MESSAGES|cmd_new",
    "0|CommandHandler|argomenti|filters.UpdateType.MESSAGES|cmd_argomenti",
    "0|CommandHandler|elabora|filters.UpdateType.MESSAGES|cmd_elabora",
    "0|CallbackQueryHandler|^lz:ora$|on_lesson_now_callback",
    "0|CallbackQueryHandler|^gx:|on_drill_callback",
    "-2|MessageHandler|<filters.StatusUpdate.FORUM_TOPIC_CREATED or "
    "filters.StatusUpdate.FORUM_TOPIC_EDITED>|handle_topic_service",
    "0|CommandHandler|notifiche,notify|filters.UpdateType.MESSAGES|cmd_notifiche",
    "0|CommandHandler|pin|filters.UpdateType.MESSAGES|cmd_pin",
    "0|CommandHandler|ricorda|filters.UpdateType.MESSAGES|cmd_ricorda",
    "0|CommandHandler|proponi|filters.UpdateType.MESSAGES|cmd_proponi",
    "0|CommandHandler|hub|filters.UpdateType.MESSAGES|cmd_hub",
    "0|CommandHandler|dispatch|filters.UpdateType.MESSAGES|cmd_dispatch",
    "0|CommandHandler|handoff|filters.UpdateType.MESSAGES|cmd_handoff",
    "0|CommandHandler|pipeline|filters.UpdateType.MESSAGES|cmd_pipeline",
    "0|CommandHandler|migrate|filters.UpdateType.MESSAGES|cmd_migrate",
    "0|CommandHandler|restructure|filters.UpdateType.MESSAGES|cmd_restructure",
    "0|CommandHandler|riorganizza|filters.UpdateType.MESSAGES|cmd_riorganizza",
    "0|CommandHandler|prepara|filters.UpdateType.MESSAGES|cmd_prepara",
    "0|CommandHandler|pulisci|filters.UpdateType.MESSAGES|cmd_pulisci",
    "0|CommandHandler|sospeso|filters.UpdateType.MESSAGES|cmd_sospeso",
    "0|CommandHandler|stato|filters.UpdateType.MESSAGES|cmd_stato",
    "0|CallbackQueryHandler|^pu:|_on_pulisci_cb",
    "0|CommandHandler|azzera|filters.UpdateType.MESSAGES|cmd_azzera",
    "0|CallbackQueryHandler|^az:|_on_azzera_cb",
    "0|CallbackQueryHandler|^notif:|on_notif_callback",
    "0|CallbackQueryHandler|^pl:|on_pipeline_callback",
    "0|CallbackQueryHandler|^cf:|on_confirm_callback",
    "0|CallbackQueryHandler|^tk:|_on_task_cb",
    "0|CallbackQueryHandler|^tg:|_on_taskgen_cancel_cb",
    "0|CallbackQueryHandler|^rg:|_on_reorg_cb",
    "0|CallbackQueryHandler|^pr:|_on_prep_cb",
    "0|CallbackQueryHandler|^pa:|_on_prep_action_cb",
    # Generic remote card protocol (Coordinator#45): a Life node's card taps.
    "0|CallbackQueryHandler|^cx:|_on_remote_card_cb",
    "0|CallbackQueryHandler|^rt:|on_rating_callback",
    # Legatura di un gruppo a un progetto: il menu che arriva da solo quando
    # il bot entra in un gruppo, e la callback che lo lega.
    "0|CommandHandler|lega|filters.UpdateType.MESSAGES|cmd_lega",
    "0|CallbackQueryHandler|^gb:|on_group_bind_cb",
    "0|CallbackQueryHandler|^dmr:|on_dm_route_cb",
    # Button menu of project groups declaring [[menu]] (Task 10).
    "0|CallbackQueryHandler|^gm:|on_group_menu_callback",
    "0|ChatMemberHandler|on_my_chat_member",
    "0|CallbackQueryHandler|None|on_callback",
    "0|CommandHandler|do_thing|filters.Chat(-1009990001)|"
    "_make_capability_handler.<locals>._handler",
    "0|CommandHandler|do_thing|filters.UpdateType.MESSAGES|"
    "_make_capability_handler.<locals>._handler",
    "0|CommandHandler|only_room|filters.UpdateType.MESSAGES|"
    "_make_capability_handler.<locals>._handler",
    # #56: voice notes BEFORE the dedicated-group catch-all, which would
    # otherwise swallow them (it matches every non-command message).
    "0|MessageHandler|filters.VOICE|handle_voice",
    # Lesson recordings (a member, 2026-10-08): matches only chats whose manifest
    # declares telegram.audio_command, so every other group is unchanged.
    "0|MessageHandler|<<<<<filters.AUDIO or filters.VIDEO> or filters.VIDEO_NOTE> or "
    "filters.Document.Category('audio/')> or filters.Document.Category('video/')> and "
    "LessonAudioChat>|handle_project_audio",
    "0|MessageHandler|<filters.Chat(-1009990001) and <inverted filters.COMMAND>>|"
    "partial(handle_project_group_message,project_key='zzz_fake')",
    # group=-1: gira prima di ogni altro handler e non consuma l'update, cosi'
    # il messaggio prosegue verso chi lo avrebbe gestito comunque. La posizione
    # in questa lista e' l'ordine di REGISTRAZIONE; a runtime PTB ordina per
    # gruppo, quindi -1 resta comunque il primo a girare (vedi archive_message).
    "-1|MessageHandler|filters.ALL|archive_message",
    # Before the catch-alls on purpose: a promotion to supergroup is a service
    # message delivered exactly once, and losing it strands every notification
    # aimed at the old chat id (see handle_chat_migration).
    "0|MessageHandler|filters.StatusUpdate.MIGRATE|handle_chat_migration",
    "0|MessageHandler|filters.PHOTO|handle_photo",
    "0|MessageHandler|<filters.TEXT and <inverted filters.COMMAND>>|handle_message",
]


# --- recording harness -------------------------------------------------------
class _FakeUpdater:
    running = False


class _FakeApp:
    """Records handler registration instead of talking to Telegram."""

    def __init__(self, token: str = ""):
        self.token = token
        self.handlers: list[tuple[int, object]] = []
        self.polled: list[dict] = []
        self.post_init = None
        self.post_shutdown = None
        self.updater = _FakeUpdater()
        self.bot = object()

    def add_handler(self, handler, group=0):
        self.handlers.append((group, handler))

    def run_polling(self, **kw):
        self.polled.append(kw)


class _FakeBuilder:
    def __init__(self, sink: list[_FakeApp]):
        self._sink = sink
        self._app = _FakeApp()

    def token(self, value):
        self._app.token = value
        return self

    def concurrent_updates(self, *_a, **_k):
        return self

    def post_init(self, cb):
        self._app.post_init = cb
        return self

    def post_shutdown(self, cb):
        self._app.post_shutdown = cb
        return self

    def build(self):
        self._sink.append(self._app)
        return self._app


def _callback_name(cb) -> str:
    if isinstance(cb, functools.partial):
        kw = ",".join(f"{k}={v!r}" for k, v in sorted(cb.keywords.items()))
        return f"partial({getattr(cb.func, '__name__', cb.func)},{kw})"
    return getattr(cb, "__qualname__", repr(cb))


def handler_signature(handlers) -> list[str]:
    """A stable, human-readable fingerprint of a registration sequence."""
    out = []
    for group, h in handlers:
        cls = type(h).__name__
        if cls == "CommandHandler":
            cmds = ",".join(sorted(h.commands))
            out.append(f"{group}|CommandHandler|{cmds}|{h.filters}|{_callback_name(h.callback)}")
        elif cls == "CallbackQueryHandler":
            pat = h.pattern.pattern if getattr(h, "pattern", None) is not None else None
            out.append(f"{group}|CallbackQueryHandler|{pat}|{_callback_name(h.callback)}")
        elif cls == "MessageHandler":
            out.append(f"{group}|MessageHandler|{h.filters}|{_callback_name(h.callback)}")
        else:  # TypeHandler: the access gate (#83)
            out.append(f"{group}|{cls}|{_callback_name(h.callback)}")
    return out


@pytest.fixture
def wiring(monkeypatch):
    """Deterministic, device-independent inputs for ``bot.main()``.

    Pins one synthetic project with one group + two capabilities so every
    registration branch (system commands, callbacks, group/command-room caps,
    the group MessageHandler loop, the catch-alls) is exercised.
    """
    built: list[_FakeApp] = []

    class _App:
        @staticmethod
        def builder():
            return _FakeBuilder(built)

    fake_manifest = manifest.Manifest(
        telegram=manifest.TelegramConf(group_chat_id=FAKE_CHAT, agent="generale"),
        capabilities=[
            manifest.Capability(key="do-thing", label="Do", cli="echo", scope="both", hint="h"),
            manifest.Capability(key="only_room", label="R", cli="echo", scope="command-room"),
        ],
    )

    monkeypatch.setattr(bot, "Application", _App)
    monkeypatch.setattr(bot, "list_projects", lambda: [FAKE_PROJECT])
    monkeypatch.setattr(manifest, "load", lambda p: fake_manifest)
    monkeypatch.setattr(projects, "list_projects", lambda: [FAKE_PROJECT])
    monkeypatch.setattr(bot, "_load_state", lambda: None)
    monkeypatch.setattr(bot, "seed_group_bindings", lambda: None)
    monkeypatch.setattr(bot.projects, "life_graph_drift", lambda *a, **k: None)
    monkeypatch.setattr(
        ownership,
        "load_config",
        lambda: ownership.OwnershipConfig(
            device="test",
            priority=1,
            port=0,
            peer_url="",
            probe_interval=1.0,
            fail_threshold=1,
            state_dir=".",
        ),
    )
    # Never actually spin an event loop; close the coroutine so Python does not
    # warn about it being un-awaited. Stubbed runners hand back a plain value.
    monkeypatch.setattr(
        bot.asyncio, "run", lambda coro: coro.close() if hasattr(coro, "close") else None
    )
    return built


def _no_extra_bots(monkeypatch):
    monkeypatch.setattr(bot_registry, "load_extra_bots", lambda env=None: [])


# --- THE zero-regression test ------------------------------------------------
def test_no_extra_bots_registers_exactly_todays_handlers(wiring, monkeypatch):
    """With an empty bot registry, main() must reproduce the single-bot bot.py."""
    _no_extra_bots(monkeypatch)

    bot.main()

    assert len(wiring) == 1, "an unconfigured Coordinator must build exactly one Application"
    assert handler_signature(wiring[0].handlers) == GOLDEN


def test_no_extra_bots_keeps_the_blocking_run_polling_path(wiring, monkeypatch):
    """The single-bot deployment must still go through app.run_polling()."""
    _no_extra_bots(monkeypatch)
    ran_multi = []
    monkeypatch.setattr(bot, "_run_multi", lambda *a: ran_multi.append(a))

    bot.main()

    assert len(wiring[0].polled) == 1
    assert not ran_multi


def test_default_bot_keeps_full_surface_when_extra_bots_exist(wiring, monkeypatch):
    """An extra bot must not strip the Coordinator's own commands/callbacks.

    Only the surface of the project that explicitly moved (its group handler and
    its group-scoped capability) leaves the default bot; nothing else does.

    The two catch-alls are the deliberate exception: they stay registered but
    narrow to exclude the moved chat, so the Coordinator does not double-process
    a group another bot now owns. Their scoping is pinned behaviourally by
    ``test_a_reassigned_group_is_deaf_on_the_default_bot``; here we only require
    that they survive with their callbacks intact.

    The unfiltered capability handler of the moved project narrows for the same
    reason and on the same terms — see
    ``test_a_reassigned_capability_is_deaf_in_that_group``.
    """
    monkeypatch.setattr(
        bot_registry,
        "load_extra_bots",
        lambda env=None: [bot_registry.BotConf(name="chef", token="1:x", node="zzz_fake")],
    )
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: [FAKE_PROJECT.key])
    monkeypatch.setattr(bot, "build_node_application", lambda conf: _FakeApp(conf.token))

    bot.main()

    sig = handler_signature(wiring[0].handlers)
    moved = [
        line
        for line in GOLDEN
        if "partial(handle_project_group_message" in line or "filters.Chat(" in line
    ]
    assert moved, "fixture must contain a reassignable group surface"
    for line in moved:
        assert line not in sig, "a reassigned group must not stay on the default bot too"

    narrowed = {"handle_photo", "handle_message", "handle_voice", "handle_project_audio"}
    # The moved project's unfiltered capabilities: they narrow, never disappear.
    # Both scopes are affected — the dedicated bot registers `both` AND
    # `command-room` caps with no chat filter, so either would answer twice.
    narrowing_caps = {
        f"0|CommandHandler|{c}|filters.UpdateType.MESSAGES|"
        "_make_capability_handler.<locals>._handler"
        for c in ("do_thing", "only_room")
    }
    for line in GOLDEN:
        if line in moved:
            continue
        if line in narrowing_caps:
            cmd = line.split("|")[2]
            assert any(s.startswith(f"0|CommandHandler|{cmd}|") for s in sig), (
                "a capability may narrow, never disappear"
            )
            continue
        if line.rsplit("|", 1)[-1] in narrowed:
            assert any(
                s.startswith("0|MessageHandler|") and s.endswith(line.rsplit("|", 1)[-1])
                for s in sig
            ), "a catch-all may narrow, never disappear"
            continue
        assert line in sig, "the Coordinator's own surface must survive untouched"


def test_reassignment_only_applies_to_bots_that_actually_loaded(wiring, monkeypatch):
    """A manifest naming a bot with no token keeps its current surface."""
    _no_extra_bots(monkeypatch)
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: [FAKE_PROJECT.key])

    bot.main()

    assert handler_signature(wiring[0].handlers) == GOLDEN


# --- The reassigned group must be deaf on the default bot ---------------------
# Dropping the project's chat-scoped handler is NOT enough. The Coordinator's
# token has privacy mode OFF, so while it sits in the group Telegram delivers it
# every message too; its catch-all then finds the (chat, 0) binding that
# seed_group_bindings() wrote — which is bot-agnostic — and runs a SECOND claude
# session on the same node for the same message. Reassignment has to silence the
# catch-alls in that chat, not just move one handler.
def _reassign_fake_project_to_chef(monkeypatch):
    monkeypatch.setattr(
        bot_registry,
        "load_extra_bots",
        lambda env=None: [bot_registry.BotConf(name="chef", token="1:x", node="zzz_fake")],
    )
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: [FAKE_PROJECT.key])
    monkeypatch.setattr(bot, "build_node_application", lambda conf: _FakeApp(conf.token))


def _catch_alls(app):
    """The default bot's two chat-wide handlers, by callback identity."""
    from telegram.ext import MessageHandler

    return [
        h
        for _g, h in app.handlers
        if isinstance(h, MessageHandler)
        and getattr(h.callback, "__name__", "") in ("handle_message", "handle_photo")
    ]


def _text_update(chat_id: int):
    import datetime as _dt

    from telegram import Chat, Message, Update, User

    chat = Chat(id=chat_id, type=Chat.SUPERGROUP if chat_id < 0 else Chat.PRIVATE)
    msg = Message(
        message_id=1,
        date=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        chat=chat,
        from_user=User(id=111, first_name="G", is_bot=False),
        text="ciao",
    )
    return Update(update_id=1, message=msg)


def test_a_reassigned_group_is_deaf_on_the_default_bot(wiring, monkeypatch):
    """The Coordinator must ignore a chat another bot now owns.

    Otherwise every message in that group is processed twice: once by the
    dedicated bot's group ingest, once by the Coordinator's catch-all.
    """
    _reassign_fake_project_to_chef(monkeypatch)

    bot.main()

    handlers = _catch_alls(wiring[0])
    assert handlers, "the default bot must still have its catch-alls"
    for h in handlers:
        assert not h.check_update(_text_update(FAKE_CHAT)), (
            f"{h.callback.__name__} still accepts messages from the reassigned group"
        )


def _command_update(chat_id: int, text: str):
    import datetime as _dt

    from telegram import Chat, Message, MessageEntity, Update, User

    chat = Chat(id=chat_id, type=Chat.SUPERGROUP if chat_id < 0 else Chat.PRIVATE)
    msg = Message(
        message_id=2,
        date=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        chat=chat,
        from_user=User(id=111, first_name="G", is_bot=False),
        text=text,
        entities=(
            MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text.split()[0])),
        ),
    )
    # CommandHandler resolves "/cmd@botname" against the bot's own username, so
    # the message must carry a bot. A stub keeps the test offline.
    class _StubBot:
        username = "coordinator_bot"

    msg.set_bot(_StubBot())
    update = Update(update_id=2, message=msg)
    update.set_bot(_StubBot())
    return update


def _accepting(app, cmd: str, chat_id: int):
    """Default-bot CommandHandlers for ``cmd`` that would answer in ``chat_id``."""
    from telegram.ext import CommandHandler

    update = _command_update(chat_id, f"/{cmd}")
    return [
        h
        for _g, h in app.handlers
        if isinstance(h, CommandHandler) and cmd in h.commands and h.check_update(update)
    ]


def test_a_reassigned_capability_is_deaf_in_that_group(wiring, monkeypatch):
    """One ``/cmd`` in a reassigned group must produce ONE answer, not two.

    The chat-scoped branch of the capability loop already stood down for a
    reassigned project, but the ``command-room``/``both`` branch registered with
    no chat filter at all — so the dedicated bot and the Coordinator both
    answered the same ``/contatta`` in the same room. Seen live in "Casa 🕌".
    """
    _reassign_fake_project_to_chef(monkeypatch)

    bot.main()

    assert not _accepting(wiring[0], "do_thing", FAKE_CHAT), (
        "the Coordinator still answers a reassigned capability inside that group"
    )


def test_a_reassigned_capability_survives_outside_that_group(wiring, monkeypatch):
    """Silencing is scoped to the moved room: the command room keeps working."""
    _reassign_fake_project_to_chef(monkeypatch)

    bot.main()

    assert _accepting(wiring[0], "do_thing", 555), "a private chat must still reach the capability"
    assert _accepting(wiring[0], "only_room", 555), "a command-room capability must survive"


def test_without_reassignment_a_capability_answers_everywhere(wiring, monkeypatch):
    """Zero-regression: with no extra bots nothing narrows."""
    _no_extra_bots(monkeypatch)

    bot.main()

    assert _accepting(wiring[0], "do_thing", FAKE_CHAT)
    assert _accepting(wiring[0], "do_thing", 555)


def test_the_default_bot_still_hears_every_chat_it_owns(wiring, monkeypatch):
    """The silencing must be scoped to the reassigned chat and nothing else."""
    _reassign_fake_project_to_chef(monkeypatch)

    bot.main()

    text_handler = [h for h in _catch_alls(wiring[0]) if h.callback.__name__ == "handle_message"][0]
    assert text_handler.check_update(_text_update(555)), "a private chat must still be heard"
    assert text_handler.check_update(_text_update(-1009990002)), "an unrelated group too"


def test_without_reassignment_the_catch_all_is_unfiltered(wiring, monkeypatch):
    """Zero-regression: with no extra bots the catch-all keeps hearing everything."""
    _no_extra_bots(monkeypatch)

    bot.main()

    text_handler = [h for h in _catch_alls(wiring[0]) if h.callback.__name__ == "handle_message"][0]
    assert text_handler.check_update(_text_update(FAKE_CHAT))


def test_extra_bots_switch_to_the_multi_poller_path(wiring, monkeypatch):
    """N tokens cannot share run_polling(): the manual N-app lifecycle takes over."""
    monkeypatch.setattr(
        bot_registry,
        "load_extra_bots",
        lambda env=None: [bot_registry.BotConf(name="chef", token="1:x", node="zzz_fake")],
    )
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: [])
    node_apps = [_FakeApp("1:x")]
    monkeypatch.setattr(bot, "build_node_application", lambda conf: node_apps[0])
    calls: list[tuple] = []
    monkeypatch.setattr(bot, "_run_multi", lambda *a: calls.append(a))

    bot.main()

    assert wiring[0].polled == [], "the blocking single-bot path must not run"
    assert len(calls) == 1
    default_app, extras = calls[0]
    assert default_app is wiring[0]
    assert extras == node_apps


def test_a_bot_that_fails_to_build_does_not_stop_the_coordinator(wiring, monkeypatch):
    """Isolation at the Application level, not just at config parsing."""
    confs = [
        bot_registry.BotConf(name="broken", token="1:x", node="nope"),
        bot_registry.BotConf(name="chef", token="2:y", node="zzz_fake"),
    ]
    monkeypatch.setattr(bot_registry, "load_extra_bots", lambda env=None: confs)
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: [])
    good = _FakeApp("2:y")

    def _build(conf):
        if conf.name == "broken":
            raise RuntimeError("bad token rejected by PTB")
        return good

    monkeypatch.setattr(bot, "build_node_application", _build)
    calls: list[tuple] = []
    monkeypatch.setattr(bot, "_run_multi", lambda *a: calls.append(a))

    bot.main()

    assert calls[0][1] == [good], "the healthy bot and the Coordinator must still come up"


class _LifecycleApp(_FakeApp):
    """Records the PTB lifecycle calls _run_multi is supposed to drive."""

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.events: list[str] = []
        outer = self

        class _Upd:
            running = False

            async def start_polling(self, **kw):
                outer.events.append("poll")
                self.running = True

            async def stop(self):
                outer.events.append("unpoll")
                self.running = False

        self.updater = _Upd()

    async def initialize(self):
        self.events.append("init")

    async def start(self):
        self.events.append("start")

    async def stop(self):
        self.events.append("stop")

    async def shutdown(self):
        self.events.append("shutdown")


@pytest.mark.asyncio
async def test_run_multi_starts_and_tears_down_every_app(monkeypatch):
    """Each token gets its own initialize -> post_init -> start -> poll, and a
    clean teardown — the manual equivalent of what run_polling does for one."""
    import asyncio

    default = _LifecycleApp("default")
    extra = _LifecycleApp("chef")
    inits: list[str] = []

    async def _post_init(app):
        inits.append(app.name)

    default.post_init = _post_init
    extra.post_init = _post_init

    # _run_multi idles on an Event fed by SIGINT/SIGTERM; fire it as soon as
    # every updater is polling so the test exercises the teardown too.
    real_event = asyncio.Event

    class _AutoStop(real_event):
        async def wait(self):
            while not (default.updater.running and extra.updater.running):
                await asyncio.sleep(0)
            return True

    monkeypatch.setattr(bot.asyncio, "Event", _AutoStop)

    await bot._run_multi(default, [extra])

    assert inits == ["chef", "default"], "extras come up before the Coordinator polls"
    assert default.events == ["init", "start", "poll", "unpoll", "stop", "shutdown"]
    assert extra.events == ["init", "start", "poll", "unpoll", "stop", "shutdown"]


# --- Task 2: the bot registry ------------------------------------------------
def test_empty_registry_means_no_extra_bots():
    assert bot_registry.load_extra_bots({}) == []
    assert bot_registry.load_extra_bots({"COORDINATOR_BOTS": "   "}) == []


def test_registry_reads_names_tokens_and_nodes():
    env = {
        "TELEGRAM_BOT_TOKEN": "111111:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "COORDINATOR_BOTS": "chef, travel",
        "TELEGRAM_BOT_TOKEN_CHEF": "222222:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "TELEGRAM_BOT_TOKEN_TRAVEL": "333333:ccccccccccccccccccccccccccccccccccc",
        "COORDINATOR_BOT_NODE_TRAVEL": "travel",
        "COORDINATOR_BOT_LABEL_CHEF": "Cucina",
    }
    bots = bot_registry.load_extra_bots(env)

    assert [b.name for b in bots] == ["chef", "travel"]
    assert bots[0].node == "chef"  # defaults to the bot name
    assert bots[0].label == "Cucina"
    assert bots[1].node == "travel"
    assert all(not b.is_default for b in bots)


def test_registry_never_leaks_a_token_in_repr():
    conf = bot_registry.BotConf(name="chef", token="222222:secret-value", node="chef")
    assert "secret-value" not in repr(conf)


@pytest.mark.parametrize(
    "env,reason",
    [
        ({"COORDINATOR_BOTS": "chef"}, "token env var missing"),
        ({"COORDINATOR_BOTS": "chef", "TELEGRAM_BOT_TOKEN_CHEF": "   "}, "token blank"),
        ({"COORDINATOR_BOTS": "chef", "TELEGRAM_BOT_TOKEN_CHEF": "not-a-token"}, "malformed"),
        ({"COORDINATOR_BOTS": "default", "TELEGRAM_BOT_TOKEN_DEFAULT": "1:x"}, "reserved name"),
        ({"COORDINATOR_BOTS": "!!", "TELEGRAM_BOT_TOKEN___": "1:x"}, "invalid name"),
    ],
)
def test_registry_skips_unusable_bots(env, reason):
    assert bot_registry.load_extra_bots(env) == [], reason


def test_a_broken_bot_does_not_affect_the_others():
    """Failure isolation: one bad entry must not disqualify its neighbours."""
    good = "444444:ddddddddddddddddddddddddddddddddddd"
    env = {
        "COORDINATOR_BOTS": "chef,broken,travel",
        "TELEGRAM_BOT_TOKEN_CHEF": "222222:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        # BROKEN has no token at all
        "TELEGRAM_BOT_TOKEN_TRAVEL": good,
    }
    bots = bot_registry.load_extra_bots(env)

    assert [b.name for b in bots] == ["chef", "travel"]
    assert bots[1].token == good


def test_registry_rejects_a_token_already_polled(caplog):
    """Two pollers on one token is a guaranteed 409 — reject, don't hope."""
    shared = "222222:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    env = {
        "TELEGRAM_BOT_TOKEN": shared,  # the default bot already polls it
        "COORDINATOR_BOTS": "chef,travel",
        "TELEGRAM_BOT_TOKEN_CHEF": shared,
        "TELEGRAM_BOT_TOKEN_TRAVEL": "333333:ccccccccccccccccccccccccccccccccccc",
    }
    bots = bot_registry.load_extra_bots(env)
    assert [b.name for b in bots] == ["travel"]

    dup = {
        "COORDINATOR_BOTS": "chef,travel",
        "TELEGRAM_BOT_TOKEN_CHEF": shared,
        "TELEGRAM_BOT_TOKEN_TRAVEL": shared,
    }
    assert [b.name for b in bot_registry.load_extra_bots(dup)] == ["chef"]


def test_registry_skips_duplicate_names():
    env = {
        "COORDINATOR_BOTS": "chef,chef",
        "TELEGRAM_BOT_TOKEN_CHEF": "222222:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }
    assert [b.name for b in bot_registry.load_extra_bots(env)] == ["chef"]


# --- Task 1: the manifest data field -----------------------------------------
def test_telegram_conf_defaults_are_backward_compatible():
    tg = manifest.TelegramConf()
    assert tg.bot == "default"
    assert tg.mode == ""
    assert tg.every_message is None


@pytest.mark.parametrize(
    "kwargs,mode,every",
    [
        ({}, "dm", False),
        ({"group_chat_id": -100123}, "group", True),
        ({"group_chat_id": -100123, "group_thread_id": 7}, "topic", False),
    ],
)
def test_mode_and_every_message_derive_todays_behaviour(kwargs, mode, every):
    """Absent declarations must reproduce main()'s historical rules exactly."""
    tg = manifest.TelegramConf(**kwargs)
    assert tg.effective_mode == mode
    assert tg.wants_every_message is every


def test_declared_mode_and_every_message_win():
    tg = manifest.TelegramConf(group_chat_id=-1, group_thread_id=9, mode="group")
    assert tg.effective_mode == "group"
    assert manifest.TelegramConf(group_chat_id=-1, every_message=False).wants_every_message is False
    assert manifest.TelegramConf(every_message=True).wants_every_message is True


def _write_manifest(tmp_path, body: str):
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "coordinator.toml").write_text(body, encoding="utf-8")
    return projects.Project(
        key="p", label="P", emoji="x", host_path=str(ws), workspace=str(ws)
    )


def test_parses_bot_mode_and_every_message(tmp_path):
    project = _write_manifest(
        tmp_path,
        """
[telegram]
group_chat_id = -100999
bot = "Chef"
mode = "group"
every_message = true
""",
    )
    tg = manifest.load(project).telegram
    assert tg.bot == "chef"  # normalised
    assert tg.mode == "group"
    assert tg.every_message is True


def test_manifest_without_the_new_keys_is_unchanged(tmp_path):
    project = _write_manifest(
        tmp_path,
        """
[telegram]
group_chat_id = -100999
agent = "cuoco"
""",
    )
    tg = manifest.load(project).telegram
    assert tg.bot == "default"
    assert tg.agent == "cuoco"
    assert tg.effective_mode == "group"
    assert tg.wants_every_message is True


@pytest.mark.parametrize(
    "line,field,expected",
    [
        ('bot = "!!bad"', "bot", "default"),
        ('mode = "carrier-pigeon"', "mode", ""),
        ('every_message = "maybe"', "every_message", None),
    ],
)
def test_invalid_new_keys_degrade_to_the_default(tmp_path, line, field, expected):
    project = _write_manifest(tmp_path, f"[telegram]\ngroup_chat_id = -1\n{line}\n")
    assert getattr(manifest.load(project).telegram, field) == expected


def test_bot_bindings_carries_the_bot_name(monkeypatch, tmp_path):
    project = _write_manifest(tmp_path, '[telegram]\ngroup_chat_id = -100999\nbot = "chef"\n')
    monkeypatch.setattr(projects, "list_projects", lambda: [project])

    assert manifest.group_bindings() == [(-100999, 0, "p", "generale")]
    assert manifest.bot_bindings() == [(-100999, 0, "p", "generale", "chef")]
    assert manifest.projects_for_bot("chef") == ["p"]
    assert manifest.projects_for_bot("default") == []


# --- Task 3: the extra bot's restricted surface ------------------------------
def _node_app(monkeypatch, conf, bindings=()):
    built: list[_FakeApp] = []

    class _App:
        @staticmethod
        def builder():
            return _FakeBuilder(built)

    monkeypatch.setattr(bot, "Application", _App)
    monkeypatch.setattr(manifest, "bot_bindings", lambda: list(bindings))
    monkeypatch.setattr(manifest, "telegram_of", lambda p: manifest.TelegramConf(group_chat_id=-1))
    monkeypatch.setattr(bot, "resolve_project", lambda k: FAKE_PROJECT)
    bot.build_node_application(conf)
    return built[0]


def test_extra_bot_surface_is_small_and_node_scoped(monkeypatch):
    conf = bot_registry.BotConf(name="chef", token="1:x", node="personalChef")
    app = _node_app(monkeypatch, conf)

    sig = handler_signature(app.handlers)
    assert sig == [
        "-3|TypeHandler|life_access_gate",
        "-1|MessageHandler|filters.ALL|archive_message",
        "-2|MessageHandler|<filters.StatusUpdate.FORUM_TOPIC_CREATED or "
        "filters.StatusUpdate.FORUM_TOPIC_EDITED>|handle_topic_service",
        "0|CommandHandler|menu,start|filters.UpdateType.MESSAGES|node_start[chef]",
        "0|CommandHandler|reset|filters.UpdateType.MESSAGES|cmd_reset",
        "0|CommandHandler|new|filters.UpdateType.MESSAGES|cmd_new",
        "0|CommandHandler|argomenti|filters.UpdateType.MESSAGES|cmd_argomenti",
        "0|CallbackQueryHandler|^gx:|on_drill_callback",
        "0|CallbackQueryHandler|^nm:|node_menu_cb[chef]",
        # ^nr: answers "which node did you mean?" — the buttons that replace a
        # guess when the routing signal is ambiguous. Deliberately a separate
        # pattern from ^nm:, so a routing choice can never be confused with a
        # menu navigation.
        "0|CallbackQueryHandler|^nr:|node_route_cb[chef]",
        "0|CallbackQueryHandler|^gm:|on_group_menu_callback",
        "0|MessageHandler|filters.VOICE|handle_voice",
        "0|MessageHandler|<<<<<filters.AUDIO or filters.VIDEO> or filters.VIDEO_NOTE> or "
        "filters.Document.Category('audio/')> or filters.Document.Category('video/')> and "
        "LessonAudioChat>|handle_project_audio",
        "0|MessageHandler|<filters.TEXT and <inverted filters.COMMAND>>|node_text[chef]",
    ]
    # None of the Coordinator's privileged surface leaks onto a node bot.
    joined = "\n".join(sig)
    for forbidden in ("cmd_progetti", "cmd_notifiche", "cmd_hub", "cmd_pipeline", "handle_photo"):
        assert forbidden not in joined


def test_extra_bot_reuses_the_generic_group_ingest(monkeypatch):
    """Human groups must go through handle_project_group_message, not a fork."""
    conf = bot_registry.BotConf(name="chef", token="1:x", node="personalChef")
    bindings = [
        (-100777, 0, "personalChef", "cuoco", "chef"),
        (-100888, 0, "other", "generale", "default"),  # not this bot's group
        (-100999, 5, "forum", "generale", "chef"),  # a forum topic: never chat-wide
    ]
    app = _node_app(monkeypatch, conf, bindings)

    group_handlers = [
        line for line in handler_signature(app.handlers) if "handle_project_group_message" in line
    ]
    assert group_handlers == [
        "0|MessageHandler|<filters.Chat(-100777) and <inverted filters.COMMAND>>|"
        "partial(handle_project_group_message,project_key='personalChef')"
    ]


def test_bot_for_falls_back_to_the_default_bot(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(bot, "_BOT", sentinel)
    monkeypatch.setattr(bot, "_BOTS", {})
    assert bot.bot_for() is sentinel
    assert bot.bot_for("chef") is sentinel

    chef = object()
    monkeypatch.setattr(bot, "_BOTS", {"chef": chef})
    assert bot.bot_for("chef") is chef
    assert bot.bot_for() is sentinel


# --- Task 4: menu navigation from the graph, exercised through the handlers --
def _graph_node(node_id, label, kind, children=(), exists=True):
    return {
        "id": node_id,
        "label": label,
        "kind": kind,
        "children": list(children),
        "exists": exists,
        "commercial": False,
    }


HEALTH_GRAPH = {
    "health": _graph_node("health", "Health", "cluster", ["fitness", "phyto"]),
    "fitness": _graph_node("fitness", "Fitness", "cluster", ["hiit"]),
    "hiit": _graph_node("hiit", "HIIT", "app"),
    "phyto": _graph_node("phyto", "Fitoterapia", "app"),
}


class _FakeMessage:
    def __init__(self, text: str = ""):
        self.replies: list[dict] = []
        self.edits: list[dict] = []
        self.message_thread_id = None
        self.text = text
        self.caption = None

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})

    async def edit_text(self, text, **kw):
        self.edits.append({"text": text, **kw})


class _FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers: list[tuple] = []

    async def answer(self, *a, **kw):
        self.answers.append((a, kw))

    async def edit_message_text(self, text, **kw):
        self.message.edits.append({"text": text, **kw})


class _FakeUpdate:
    def __init__(self, message, chat_id=555, query=None):
        self.effective_message = message
        self.effective_chat = type("C", (), {"id": chat_id})()
        self.effective_user = type("U", (), {"id": 111})()
        self.callback_query = query


@pytest.fixture
def node_bot(monkeypatch):
    """A Health bot with a stubbed graph and an authorised user."""
    monkeypatch.setattr(bot, "_node_graph", lambda force=False: HEALTH_GRAPH)
    monkeypatch.setattr(bot, "_profile", lambda update: "owner")
    monkeypatch.setattr(bot, "can_access", lambda profile, key: True)
    monkeypatch.setattr(bot, "resolve_project", lambda k: FAKE_PROJECT)
    monkeypatch.setattr(manifest, "telegram_of", lambda p: manifest.TelegramConf())
    monkeypatch.setattr(bot, "resolve_agent", lambda p, name: projects.GENERALE)
    bound: list[tuple] = []
    monkeypatch.setattr(bot, "_bind_topic", lambda *a: bound.append(a))
    return bot_registry.BotConf(name="health", token="1:x", node="health"), bound


@pytest.mark.asyncio
async def test_start_on_an_extra_bot_shows_the_nodes_children(node_bot):
    conf, _ = node_bot
    msg = _FakeMessage()

    await bot._make_node_start_handler(conf)(_FakeUpdate(msg), None)

    assert len(msg.replies) == 1
    labels = [
        b.text
        for row in msg.replies[0]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert labels == ["📂 Fitness", "▶️ Fitoterapia"], "no Back at the bot's own root"


@pytest.mark.asyncio
async def test_entering_a_cluster_offers_its_children_and_a_back(node_bot):
    conf, _ = node_bot
    msg = _FakeMessage()
    query = _FakeQuery("nm:go:fitness", msg)

    await bot._make_node_menu_callback(conf)(_FakeUpdate(msg, query=query), None)

    assert msg.edits, "navigating deeper edits the open menu instead of spamming"
    buttons = [b for row in msg.edits[0]["reply_markup"].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["▶️ HIIT", "⬅️ Indietro"]
    assert buttons[-1].callback_data == "nm:go:health", "Back climbs exactly one level"
    assert "Health › Fitness" in msg.edits[0]["text"]


@pytest.mark.asyncio
async def test_choosing_a_leaf_opens_a_session_in_that_node(node_bot, monkeypatch):
    """Depth 3: Health -> Fitness -> HIIT must open the session in HIIT itself."""
    conf, bound = node_bot
    asked: list[str] = []
    hiit = projects.Project(
        key="hiit", label="HIIT", emoji="🏃", host_path="/h/hiit", workspace="/workspaces/hiit"
    )

    def _resolve(key):
        asked.append(key)
        return hiit

    monkeypatch.setattr(bot, "resolve_project", _resolve)
    msg = _FakeMessage()
    query = _FakeQuery("nm:go:hiit", msg)

    await bot._make_node_menu_callback(conf)(_FakeUpdate(msg, chat_id=777, query=query), None)

    assert asked == ["hiit"], "the session opens in the CHILD's workspace, not the bot's root node"
    assert bound == [(777, 0, "hiit", projects.GENERALE.name)], (
        "the session must be keyed by (chat_id, topic_id) like every other session"
    )
    assert "sessione aperta" in msg.replies[0]["text"]


@pytest.mark.asyncio
async def test_a_node_missing_from_the_registry_says_so(node_bot, monkeypatch):
    conf, bound = node_bot
    monkeypatch.setattr(bot, "resolve_project", lambda k: None)
    msg = _FakeMessage()
    query = _FakeQuery("nm:go:hiit", msg)

    await bot._make_node_menu_callback(conf)(_FakeUpdate(msg, query=query), None)

    assert bound == []
    assert "non è registrato" in msg.replies[0]["text"]


@pytest.mark.asyncio
async def test_menu_callback_rejects_an_unauthorised_user(node_bot, monkeypatch):
    conf, bound = node_bot
    monkeypatch.setattr(bot, "_profile", lambda update: None)
    msg = _FakeMessage()
    query = _FakeQuery("nm:go:hiit", msg)

    await bot._make_node_menu_callback(conf)(_FakeUpdate(msg, query=query), None)

    assert bound == []
    assert msg.replies == [] and msg.edits == []
    assert query.answers and query.answers[0][1].get("show_alert") is True


@pytest.mark.asyncio
async def test_unreachable_graph_degrades_with_a_message(node_bot, monkeypatch):
    conf, _ = node_bot
    monkeypatch.setattr(bot, "_node_graph", lambda force=False: {})
    msg = _FakeMessage()

    await bot._make_node_start_handler(conf)(_FakeUpdate(msg), None)

    assert "life_backend irraggiungibile" in msg.replies[0]["text"]


@pytest.mark.asyncio
async def test_free_text_binds_the_bots_node_then_reuses_handle_message(node_bot, monkeypatch):
    conf, bound = node_bot
    seen: list = []

    async def _fake_handle_message(update, context):
        seen.append(update)

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)
    monkeypatch.setattr(bot, "TOPIC_BINDING", {})
    msg = _FakeMessage()
    update = _FakeUpdate(msg, chat_id=42)

    await bot._make_node_text_handler(conf)(update, None)

    assert bound == [(42, 0, FAKE_PROJECT.key, projects.GENERALE.name)]
    assert seen == [update], "the turn itself must go through the untouched handle_message"


@pytest.mark.asyncio
async def test_free_text_does_not_steal_an_existing_binding(node_bot, monkeypatch):
    conf, bound = node_bot
    monkeypatch.setattr(bot, "TOPIC_BINDING", {(42, 0): ("other", "generale")})

    async def _fake_handle_message(update, context):
        pass

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    await bot._make_node_text_handler(conf)(_FakeUpdate(_FakeMessage(), chat_id=42), None)

    assert bound == [], "a topic already bound elsewhere must keep its binding"


@pytest.mark.asyncio
async def test_node_post_init_registers_its_own_command_set(monkeypatch):
    """set_my_commands is per-token: each bot advertises only its own menu."""
    conf = bot_registry.BotConf(name="chef", token="1:x", node="personalChef")
    sent: list[list] = []

    class _Bot:
        async def set_my_commands(self, commands):
            sent.append([c.command for c in commands])

    app = _FakeApp()
    app.bot = _Bot()
    monkeypatch.setattr(bot, "_BOTS", {})

    await bot._make_node_post_init(conf)(app)

    assert bot._BOTS["chef"] is app.bot
    assert sent == [["start", "menu", "reset"]]
    assert "progetti" not in sent[0]


# --- The dedicated bot must carry its own node's capabilities ----------------
# Found by answering "can I remove the Coordinator from a group that has its own
# bot?". Today the answer is NO, and not by design: capability commands are
# registered only in the Coordinator's setup_handlers. A reassigned node's bot
# knows /start /menu /reset and nothing else, so /cena — advertised in the group's
# own pinned message — would die with the Coordinator's departure. A node's door
# must open everything that node can do.
_CAPS = (
    manifest.Capability(key="cena", label="Dinner", cli="x", scope="both"),
    manifest.Capability(key="menu_settimana", label="Week", cli="x", scope="group"),
    manifest.Capability(key="ordina-amoenus", label="Order", cli="x", scope="command-room"),
)


def _node_app_with_caps(monkeypatch, conf, caps=_CAPS, served=("zzz_fake",), chat=-100777):
    """Builds the node app itself: _node_app pins telegram_of to a throwaway chat
    id, and these tests are precisely about which chat a capability is bound to."""
    built: list[_FakeApp] = []

    class _App:
        @staticmethod
        def builder():
            return _FakeBuilder(built)

    monkeypatch.setattr(bot, "Application", _App)
    monkeypatch.setattr(bot, "resolve_project", lambda k: FAKE_PROJECT)
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: list(served))
    monkeypatch.setattr(manifest, "capabilities_of", lambda p: list(caps))
    monkeypatch.setattr(
        manifest, "telegram_of", lambda p: manifest.TelegramConf(group_chat_id=chat)
    )
    monkeypatch.setattr(
        manifest, "bot_bindings", lambda: [(chat, 0, "zzz_fake", "generale", conf.name)]
    )
    bot.build_node_application(conf)
    return built[0]


def _command_filters(app, cmd):
    return [
        line.split("|")[3]
        for line in handler_signature(app.handlers)
        if line.split("|")[1] == "CommandHandler" and line.split("|")[2] == cmd
    ]


def test_node_bot_registers_the_capabilities_of_the_node_it_serves(monkeypatch):
    conf = bot_registry.BotConf(name="chef", token="1:x", node="personalChef")
    app = _node_app_with_caps(monkeypatch, conf)

    sig = "\n".join(handler_signature(app.handlers))
    for cmd in ("cena", "menu_settimana", "ordina_amoenus"):
        assert f"|{cmd}|" in sig, f"/{cmd} is unreachable on the node's own bot:\n{sig}"


def test_node_bot_keeps_the_group_only_trust_boundary(monkeypatch):
    """scope=group stays reachable only from that project's group, exactly as on
    the Coordinator. The node bot widens WHO answers, never WHERE."""
    conf = bot_registry.BotConf(name="chef", token="1:x", node="personalChef")
    app = _node_app_with_caps(monkeypatch, conf)

    # group: one handler, chat-locked. command-room: one, unfiltered.
    assert _command_filters(app, "menu_settimana") == ["filters.Chat(-100777)"]
    assert _command_filters(app, "ordina_amoenus") == ["filters.UpdateType.MESSAGES"]
    # both: reachable from the group AND from the command room — two handlers,
    # same split the Coordinator makes.
    assert _command_filters(app, "cena") == [
        "filters.Chat(-100777)",
        "filters.UpdateType.MESSAGES",
    ]


def test_a_group_capability_is_dropped_when_the_node_has_no_group(monkeypatch):
    """No group id means filters.Chat cannot be built; registering it unfiltered
    would silently promote a group-only command to everyone's DM."""
    conf = bot_registry.BotConf(name="chef", token="1:x", node="personalChef")
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: ["zzz_fake"])
    monkeypatch.setattr(manifest, "capabilities_of", lambda p: list(_CAPS))
    monkeypatch.setattr(manifest, "telegram_of", lambda p: manifest.TelegramConf())
    monkeypatch.setattr(bot, "resolve_project", lambda k: FAKE_PROJECT)
    built: list[_FakeApp] = []

    class _App:
        @staticmethod
        def builder():
            return _FakeBuilder(built)

    monkeypatch.setattr(bot, "Application", _App)
    monkeypatch.setattr(manifest, "bot_bindings", lambda: [])
    bot.build_node_application(conf)

    assert _command_filters(built[0], "menu_settimana") == []
    assert _command_filters(built[0], "cena") == ["filters.UpdateType.MESSAGES"]


def test_a_capability_never_shadows_a_built_in_command(monkeypatch):
    """A node declaring key="reset" must not steal /reset from cmd_reset."""
    conf = bot_registry.BotConf(name="chef", token="1:x", node="personalChef")
    caps = (manifest.Capability(key="reset", label="X", cli="x", scope="both"),)
    app = _node_app_with_caps(monkeypatch, conf, caps=caps)

    resets = [
        line
        for line in handler_signature(app.handlers)
        if line.split("|")[1] == "CommandHandler" and line.split("|")[2] == "reset"
    ]
    assert len(resets) == 1 and "cmd_reset" in resets[0]


@pytest.mark.asyncio
async def test_node_menu_advertises_its_capabilities(monkeypatch):
    conf = bot_registry.BotConf(name="chef", token="1:x", node="personalChef")
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: ["zzz_fake"])
    monkeypatch.setattr(manifest, "capabilities_of", lambda p: list(_CAPS))
    monkeypatch.setattr(bot, "resolve_project", lambda k: FAKE_PROJECT)
    sent: list[list] = []

    class _Bot:
        async def set_my_commands(self, commands):
            sent.append([c.command for c in commands])

    app = _FakeApp()
    app.bot = _Bot()
    monkeypatch.setattr(bot, "_BOTS", {})
    await bot._make_node_post_init(conf)(app)

    assert sent[0][:3] == ["start", "menu", "reset"]
    assert sent[0][3:] == ["cena", "menu_settimana", "ordina_amoenus"]


# --- One room, one bot: the guard that does not depend on a declared group ----
def _tg(bot_name):
    return manifest.TelegramConf(bot=bot_name)


def test_the_coordinator_stands_down_for_a_project_owned_by_another_bot(monkeypatch):
    monkeypatch.setattr(manifest, "telegram_of", lambda p: _tg("chef"))
    monkeypatch.setattr(bot_registry, "configured_names", lambda: {"default", "chef"})
    assert bot._served_by_another_bot(FAKE_PROJECT) == "chef"


def test_the_coordinator_answers_for_its_own_projects(monkeypatch):
    monkeypatch.setattr(bot_registry, "configured_names", lambda: {"default", "chef"})
    monkeypatch.setattr(manifest, "telegram_of", lambda p: _tg(bot_registry.DEFAULT_BOT))
    assert bot._served_by_another_bot(FAKE_PROJECT) is None
    # No [telegram] section at all is the commonest case and must stay ours.
    monkeypatch.setattr(manifest, "telegram_of", lambda p: None)
    assert bot._served_by_another_bot(FAKE_PROJECT) is None


def test_the_owner_bot_never_stands_down_for_itself(monkeypatch):
    """``handle_message`` runs on EVERY app, so "another bot" is relative.

    The helper is shared: the Coordinator's app calls it, and so does each node
    bot (``_make_node_text_handler`` delegates to ``handle_message`` for an
    already-bound chat). Without knowing who is asking, the owner reads its own
    name, concludes somebody else is answering, and goes quiet — so nobody
    answers at all. Seen live: writing to @home_life_ai_bot in DM produced
    silence and one log line, ``chat=… proj=homeresearcher -> bot 'home' owns
    it``, written by the 'home' bot about itself.
    """
    monkeypatch.setattr(manifest, "telegram_of", lambda p: _tg("home"))
    monkeypatch.setattr(bot_registry, "configured_names", lambda: {"default", "home"})

    assert bot._served_by_another_bot(FAKE_PROJECT, "home") is None, (
        "the owner silenced itself; the room is left with nobody answering"
    )
    # ...while everyone else still stands down for it.
    assert bot._served_by_another_bot(FAKE_PROJECT, "chef") == "home"
    assert bot._served_by_another_bot(FAKE_PROJECT) == "home"  # default bot


def test_a_declaration_naming_an_unconfigured_bot_leaves_the_project_ours(monkeypatch):
    """Mirrors _reassigned_projects: an unknown name means the declaration was
    ignored, so the Coordinator is still the only one who can answer. Standing
    down on it would make the node unreachable from every bot."""
    monkeypatch.setattr(manifest, "telegram_of", lambda p: _tg("ghost"))
    monkeypatch.setattr(bot_registry, "configured_names", lambda: {"default", "chef"})
    assert bot._served_by_another_bot(FAKE_PROJECT) is None


# --- A handover only counts when the owner is actually in the room -----------
#
# Declaring `bot = "casa"` on a node whose group that bot never joined does not
# move the answer to it: it only silences the Coordinator. The room then has
# nobody at all, and the logs say nothing, because staying quiet is exactly what
# the code was told to do. This happened live.

_CHAT = -1003455335251


def _tg_chat(bot_name, chat_id=_CHAT):
    return manifest.TelegramConf(bot=bot_name, group_chat_id=chat_id)


def test_a_declared_owner_confirmed_absent_does_not_take_the_room(monkeypatch):
    monkeypatch.setattr(manifest, "telegram_of", lambda p: _tg_chat("casa"))
    monkeypatch.setattr(bot_registry, "configured_names", lambda: {"default", "casa"})
    monkeypatch.setattr(bot, "_ABSENT_OWNERS", {("casa", _CHAT)})
    assert bot._served_by_another_bot(FAKE_PROJECT) is None


def test_absence_is_scoped_to_the_pair_not_to_the_bot(monkeypatch):
    """casa being absent from one chat says nothing about another chat it owns."""
    monkeypatch.setattr(manifest, "telegram_of", lambda p: _tg_chat("casa", -1003769047395))
    monkeypatch.setattr(bot_registry, "configured_names", lambda: {"default", "casa"})
    monkeypatch.setattr(bot, "_ABSENT_OWNERS", {("casa", _CHAT)})
    assert bot._served_by_another_bot(FAKE_PROJECT) == "casa"


def test_a_confirmed_member_still_takes_the_room(monkeypatch):
    monkeypatch.setattr(manifest, "telegram_of", lambda p: _tg_chat("casa"))
    monkeypatch.setattr(bot_registry, "configured_names", lambda: {"default", "casa"})
    monkeypatch.setattr(bot, "_ABSENT_OWNERS", set())
    assert bot._served_by_another_bot(FAKE_PROJECT) == "casa"


def _audit_with(monkeypatch, opener, *, chat_id=_CHAT, bot_name="casa"):
    import urllib.request

    monkeypatch.setattr(bot, "list_projects", lambda: [FAKE_PROJECT])
    monkeypatch.setattr(manifest, "telegram_of", lambda p: _tg_chat(bot_name, chat_id))
    monkeypatch.setattr(
        bot_registry,
        "load_extra_bots",
        lambda *a, **k: [bot_registry.BotConf(name=bot_name, token="1:tok", node=bot_name)],
    )
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    return bot.ownership_audit(timeout=0.1)


def test_the_audit_records_a_telegram_refusal_as_absent(monkeypatch):
    """Telegram answering 400 IS an answer: that bot cannot see that chat."""
    import urllib.error

    def refuse(*a, **k):
        raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)

    assert _audit_with(monkeypatch, refuse) == [(FAKE_PROJECT.key, "casa", _CHAT, "absent")]
    assert ("casa", _CHAT) in bot._ABSENT_OWNERS


def test_a_failed_check_is_unknown_and_never_flips_the_routing(monkeypatch):
    """A boot-time network hiccup must not hand every dedicated room back to the
    Coordinator — that would double every reply across the whole fleet."""

    def explode(*a, **k):
        raise OSError("dns went away")

    assert _audit_with(monkeypatch, explode) == [(FAKE_PROJECT.key, "casa", _CHAT, "unknown")]
    assert bot._ABSENT_OWNERS == set()


def test_a_reachable_chat_is_recorded_as_member(monkeypatch):
    import contextlib
    import io as _io

    @contextlib.contextmanager
    def ok(*a, **k):
        yield _io.BytesIO(b'{"ok": true, "result": {"title": "Casa"}}')

    assert _audit_with(monkeypatch, ok) == [(FAKE_PROJECT.key, "casa", _CHAT, "member")]
    assert bot._ABSENT_OWNERS == set()


def test_the_audit_skips_what_it_cannot_judge(monkeypatch):
    """No declared chat id, or a bot this process does not run: nothing to check,
    and crucially nothing recorded as absent."""
    import urllib.request

    def boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("no network call expected")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(bot, "list_projects", lambda: [FAKE_PROJECT])
    monkeypatch.setattr(bot_registry, "load_extra_bots", lambda *a, **k: [])
    monkeypatch.setattr(manifest, "telegram_of", lambda p: _tg_chat("casa"))
    assert bot.ownership_audit(timeout=0.1) == []

    monkeypatch.setattr(
        bot_registry,
        "load_extra_bots",
        lambda *a, **k: [bot_registry.BotConf(name="casa", token="1:tok", node="casa")],
    )
    monkeypatch.setattr(manifest, "telegram_of", lambda p: manifest.TelegramConf(bot="casa"))
    assert bot.ownership_audit(timeout=0.1) == []
    assert bot._ABSENT_OWNERS == set()


# --- A misconfigured bot must say so, in the log and in the room -------------
# Live incident, 2026-08-07: bot 'casa' was declared with no
# COORDINATOR_BOT_NODE_CASA, so its node defaulted to its own name, "casa" —
# which is not a project key (the node is "homeresearcher"). Startup logged
# "bot 'casa' ready | node=casa" and every message in the room was dropped by a
# `return` with no reply. Two symptoms, one silence: the log looked healthy and
# the room looked dead.


def _misconfigured(monkeypatch):
    """A bot whose node does not resolve, everything else nominal."""
    monkeypatch.setattr(bot, "resolve_project", lambda k: None)
    monkeypatch.setattr(bot, "TOPIC_BINDING", {})
    return bot_registry.BotConf(name="casa", token="1:x", node="casa")


@pytest.mark.asyncio
async def test_a_bot_bound_to_an_unknown_node_answers_instead_of_dropping(monkeypatch):
    conf = _misconfigured(monkeypatch)
    called: list = []

    async def _fake_handle_message(update, context):
        called.append(update)

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)
    msg = _FakeMessage()

    await bot._make_node_text_handler(conf)(_FakeUpdate(msg, chat_id=42), None)

    assert called == [], "an unroutable message must not reach handle_message"
    assert len(msg.replies) == 1, "silence is the one answer that must not happen"
    assert "casa" in msg.replies[0]["text"]


@pytest.mark.asyncio
async def test_post_init_never_calls_a_misconfigured_bot_ready(monkeypatch, caplog):
    conf = _misconfigured(monkeypatch)
    app = _FakeApp()

    class _Bot:
        async def set_my_commands(self, commands):
            pass

    app.bot = _Bot()
    monkeypatch.setattr(bot, "_BOTS", {})

    with caplog.at_level("INFO", logger="coordinator"):
        await bot._make_node_post_init(conf)(app)

    text = caplog.text
    assert "ready" not in text, "'ready' must be earned, not announced"
    assert "NON operativo" in text and "COORDINATOR_BOT_NODE_CASA" in text, (
        "the log must name the fix, not just the symptom"
    )


@pytest.mark.asyncio
async def test_post_init_names_a_bot_that_presides_over_no_group(monkeypatch, caplog):
    """Legitimate (private chat still works) but almost always a missing `bot =`."""
    conf = bot_registry.BotConf(name="scheduler", token="1:x", node="scheduler")
    monkeypatch.setattr(bot, "resolve_project", lambda k: FAKE_PROJECT)
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: [])
    app = _FakeApp()

    class _Bot:
        async def set_my_commands(self, commands):
            pass

    app.bot = _Bot()
    monkeypatch.setattr(bot, "_BOTS", {})

    with caplog.at_level("INFO", logger="coordinator"):
        await bot._make_node_post_init(conf)(app)

    assert "solo in chat privata" in caplog.text
    assert "ready" in caplog.text, "it is degraded, not broken — it must still come up"


@pytest.mark.asyncio
async def test_a_correctly_bound_bot_comes_up_quietly(monkeypatch, caplog):
    conf = bot_registry.BotConf(name="health", token="1:x", node="health")
    monkeypatch.setattr(bot, "resolve_project", lambda k: FAKE_PROJECT)
    monkeypatch.setattr(manifest, "projects_for_bot", lambda name: [FAKE_PROJECT.key])
    app = _FakeApp()

    class _Bot:
        async def set_my_commands(self, commands):
            pass

    app.bot = _Bot()
    monkeypatch.setattr(bot, "_BOTS", {})

    with caplog.at_level("INFO", logger="coordinator"):
        await bot._make_node_post_init(conf)(app)

    assert "ready" in caplog.text
    assert "NON operativo" not in caplog.text and "solo in chat privata" not in caplog.text


# --- A bot rooted at a cluster routes by content ------------------------------
# The cluster bot reads one room for a whole subtree. Which node a message binds
# to therefore comes from the message itself — and when the message does not say,
# the bot asks instead of picking.

CLUSTER_GRAPH = {
    "home": {"label": "Home", "exists": True, "children": ["listings", "products"]},
    "listings": {"label": "Listings", "exists": True, "children": []},
    "products": {"label": "Products", "exists": True, "children": []},
}
CLUSTER_RULES = {
    "listings": manifest.RoutingDecl(domains=("portal.example",), keywords=("annuncio",)),
    "products": manifest.RoutingDecl(domains=("shop.example",), keywords=("prodotto",)),
}


@pytest.fixture
def cluster_bot(monkeypatch):
    """A bot whose menu root is a cluster, with two routable children."""
    monkeypatch.setattr(bot, "_node_graph", lambda force=False: CLUSTER_GRAPH)
    monkeypatch.setattr(bot, "_profile", lambda update: "owner")

    def _resolve(k):
        if k in CLUSTER_GRAPH:
            return projects.Project(k, k, "", f"/hosts/{k}", f"/workspaces/{k}")
        return None

    # Both names: bot.py resolves the destination, node_routing resolves each
    # candidate to reach its manifest. They are the same function, imported twice.
    monkeypatch.setattr(bot, "resolve_project", _resolve)
    monkeypatch.setattr(projects, "resolve_project", _resolve)
    monkeypatch.setattr(manifest, "telegram_of", lambda p: manifest.TelegramConf())
    monkeypatch.setattr(
        manifest, "routing_of",
        lambda p: CLUSTER_RULES.get(p.key, manifest.RoutingDecl()),
    )
    monkeypatch.setattr(bot, "resolve_agent", lambda p, name: projects.GENERALE)
    monkeypatch.setattr(bot, "TOPIC_BINDING", {})
    monkeypatch.setattr(bot, "_PENDING_ROUTE", {})
    bound: list[tuple] = []
    monkeypatch.setattr(bot, "_bind_topic", lambda *a: bound.append(a))
    seen: list = []

    async def _fake_handle_message(update, context):
        seen.append(update)

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)
    conf = bot_registry.BotConf(name="home", token="1:x", node="home", root="home")
    return conf, bound, seen


def test_menu_root_falls_back_to_the_project_when_unset():
    plain = bot_registry.BotConf(name="health", token="1:x", node="health")
    assert plain.menu_root == "health", "a bot that is its own root must not change"
    rooted = bot_registry.BotConf(name="home", token="1:x", node="home", root="cluster")
    assert rooted.menu_root == "cluster"


@pytest.mark.asyncio
async def test_a_link_binds_the_thread_to_the_node_that_claims_it(cluster_bot):
    conf, bound, seen = cluster_bot

    await bot._make_node_text_handler(conf)(
        _FakeUpdate(_FakeMessage("guarda portal.example/annunci/7"), chat_id=42), None
    )

    assert bound == [(42, 0, "listings", projects.GENERALE.name)]
    assert len(seen) == 1, "the turn itself still goes through handle_message"


@pytest.mark.asyncio
async def test_a_second_link_elsewhere_redirects_an_already_bound_thread(cluster_bot):
    """The whole point: two topics in one room must not pile onto the first node."""
    conf, bound, seen = cluster_bot
    bot.TOPIC_BINDING[(42, 0)] = ("listings", "generale")
    msg = _FakeMessage("questa lampada? shop.example/p/3")

    await bot._make_node_text_handler(conf)(_FakeUpdate(msg, chat_id=42), None)

    assert bound == [(42, 0, "products", projects.GENERALE.name)]
    assert msg.replies and "Products" in msg.replies[0]["text"], "the switch must be visible"


@pytest.mark.asyncio
async def test_words_alone_never_hijack_an_established_thread(cluster_bot):
    """A weak signal may open a thread but must not steal one."""
    conf, bound, seen = cluster_bot
    bot.TOPIC_BINDING[(42, 0)] = ("listings", "generale")

    await bot._make_node_text_handler(conf)(
        _FakeUpdate(_FakeMessage("e comunque quel prodotto"), chat_id=42), None
    )

    assert bound == [], "no re-binding without hard evidence"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_an_ambiguous_message_becomes_buttons_and_binds_nothing(cluster_bot):
    conf, bound, seen = cluster_bot
    msg = _FakeMessage("un annuncio e un prodotto")

    await bot._make_node_text_handler(conf)(_FakeUpdate(msg, chat_id=42), None)

    assert bound == [] and seen == [], "a guess is worse than a question"
    labels = [b.text for row in msg.replies[0]["reply_markup"].inline_keyboard for b in row]
    assert sorted(labels) == ["Listings", "Products"]
    assert bot._PENDING_ROUTE[(42, 0)] is not None, "the message must be kept for the answer"


@pytest.mark.asyncio
async def test_answering_the_question_binds_and_replays_the_parked_message(cluster_bot):
    conf, bound, seen = cluster_bot
    parked_msg = _FakeMessage("un annuncio e un prodotto")
    parked = _FakeUpdate(parked_msg, chat_id=42)
    await bot._make_node_text_handler(conf)(parked, None)

    reply = _FakeMessage()
    query = _FakeQuery("nr:products", reply)
    await bot._make_node_route_callback(conf)(_FakeUpdate(reply, chat_id=42, query=query), None)

    assert bound == [(42, 0, "products", projects.GENERALE.name)]
    assert seen == [parked], "the original message must be handled, not lost"
    assert bot._PENDING_ROUTE == {}, "an answered question must not linger"


@pytest.mark.asyncio
async def test_a_choice_whose_message_is_gone_says_so_instead_of_pretending(cluster_bot):
    conf, bound, seen = cluster_bot
    reply = _FakeMessage()
    query = _FakeQuery("nr:products", reply)

    await bot._make_node_route_callback(conf)(_FakeUpdate(reply, chat_id=42, query=query), None)

    assert bound == [(42, 0, "products", projects.GENERALE.name)]
    assert seen == [], "nothing to replay"
    assert "riscrivilo" in reply.edits[0]["text"]


@pytest.mark.asyncio
async def test_an_unreadable_graph_leaves_the_message_on_the_bots_own_project(cluster_bot):
    """Rule: 'I could not look' must degrade to the old behaviour, not to silence."""
    conf, bound, seen = cluster_bot
    bot._node_graph = lambda force=False: {}

    await bot._make_node_text_handler(conf)(
        _FakeUpdate(_FakeMessage("portal.example/annunci/7"), chat_id=42), None
    )

    assert bound == [(42, 0, "home", projects.GENERALE.name)]
    assert len(seen) == 1


def test_token_dal_segreto_del_device(tmp_path):
    """#83 F5: il token del bot unico si legge dal file 0600, non dal .env."""
    import bot_registry

    f = tmp_path / "telegram-coordinator-bot.token"
    f.write_text("123:abc\n")
    env = {"TELEGRAM_BOT_TOKEN": "999:old", "TELEGRAM_BOT_TOKEN_FILE": str(f)}
    assert bot_registry.default_token(env) == "123:abc"
    # file nominato ma illeggibile: vuoto, mai il token vecchio del .env
    assert bot_registry.default_token({**env, "TELEGRAM_BOT_TOKEN_FILE": str(tmp_path / "x")}) == ""
    assert bot_registry.default_token({"TELEGRAM_BOT_TOKEN": "999:old"}) == "999:old"


# --- per-instance node bots (#78) ---------------------------------------------


def test_a_bot_graph_reads_the_bots_own_instance(monkeypatch):
    conf = bot_registry.BotConf(
        name="comunity", token="1:x", node="comunity", instance="comunity"
    )
    monkeypatch.setattr(
        bot,
        "_instance_graph",
        lambda iid, force=False: {"comunity": {"id": "comunity", "children": []}}
        if iid == "comunity"
        else {},
    )
    assert set(bot._graph_for(conf)) == {"comunity"}


def test_resolve_bot_instances_derives_from_the_menu_root(monkeypatch):
    import bus_agents

    owners = {"comunity": "comunity", "personalChef": "personal"}
    monkeypatch.setattr(
        bus_agents, "instance_for_node", lambda node, env=None: owners.get(node)
    )
    confs = [
        bot_registry.BotConf(name="comunity", token="1:x", node="comunity"),
        bot_registry.BotConf(name="chef", token="2:y", node="personalChef"),
        bot_registry.BotConf(name="pinned", token="3:z", node="comunity",
                             instance="personal"),
        bot_registry.BotConf(name="unknown", token="4:w", node="zzz_fake"),
    ]
    out = bot._resolve_bot_instances(confs)
    assert [c.instance for c in out] == ["comunity", "personal", "personal", ""]
