# tests/test_seed_routing.py
"""A functionality's opening ``seed`` must run in the *spawned* topic, not in
the General/dashboard message it was triggered from. This is what keeps the
group's General clean and the session bound to the right (chat, topic) key —
and what makes the group behave exactly like the private chat (where topic 0
is implicit)."""
import asyncio
import textwrap
import types

import bot
import functionalities
import projects


# --- Minimal Telegram stubs (record every outgoing send + its topic) ---------
class _Msg:
    """A message we can reply to / edit / delete; records replies into ``sink``."""

    def __init__(self, sink, thread=None):
        self._sink = sink
        self.message_thread_id = thread

    async def reply_text(self, text, **kw):
        self._sink.append({"text": text, "thread": self.message_thread_id, "via": "reply"})
        return _Msg(self._sink, self.message_thread_id)

    async def edit_text(self, text, **kw):
        return None

    async def delete(self):
        return None


class _Bot:
    def __init__(self, topic_id):
        self.sent: list[dict] = []
        self._tid = topic_id

    async def create_forum_topic(self, chat_id, name):
        return types.SimpleNamespace(message_thread_id=self._tid)

    async def send_message(self, chat_id, text, message_thread_id=None, **kw):
        self.sent.append({"text": text, "thread": message_thread_id, "via": "send"})
        return _Msg(self.sent, message_thread_id)

    async def send_chat_action(self, chat_id, action, message_thread_id=None, **kw):
        return None


class _Update:
    def __init__(self, chat_id, chat_type, thread_id, sink):
        self.effective_chat = types.SimpleNamespace(id=chat_id, type=chat_type)
        self.effective_message = _Msg(sink, thread_id)

    def get_bot(self):
        return None


def _project_with_seed(tmp_path):
    d = tmp_path / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "menu-planner.md").write_text(
        "---\nname: menu-planner\ndescription: Plans the menu.\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / ".claude" / "functionalities.toml").write_text(
        textwrap.dedent(
            """
            [[functionality]]
            key = "menu"
            label = "Menu della settimana"
            agent = "menu-planner"
            seed = "[CMD:/menu]"
            """
        ),
        encoding="utf-8",
    )
    return projects.Project("x", "\U0001F373 X", "\U0001F373", "", str(tmp_path))


SEED_TEXTS = {"⏳ sto pensando…", "SEED REPLY"}  # the seed turn's UI + reply


def _run_spawn(monkeypatch, tmp_path, *, chat_type, chat_id, topic_id):
    """Spawn from a seeded functionality with run_claude mocked; return the bot."""
    p = _project_with_seed(tmp_path)
    functionalities._CACHE.clear()
    f = bot.functionalities_of(p)[0]
    calls = {}

    async def fake_run_claude(
        prompt, *, workspace, persona, resume_id=None, on_progress=None, **kw
    ):
        # **kw absorbs the per-turn knobs (model/effort) this test doesn't assert on.
        calls["prompt"] = prompt
        calls.update(kw)
        return "SEED REPLY", "sid-1"

    monkeypatch.setattr(bot, "run_claude", fake_run_claude)
    async def _admin(update):
        return True
    monkeypatch.setattr(bot, "_session_full", _admin)
    tbot = _Bot(topic_id=topic_id)
    update = _Update(chat_id, chat_type, None, tbot.sent)
    context = types.SimpleNamespace(bot=tbot)
    asyncio.run(bot._spawn_from_functionality(update, context, p, f))
    return tbot, calls


def test_seed_routes_into_new_topic_in_group(monkeypatch, tmp_path):
    chat_id, new_topic = -100123, 4242
    tbot, calls = _run_spawn(
        monkeypatch, tmp_path, chat_type="supergroup", chat_id=chat_id, topic_id=new_topic
    )
    # The seed turn (thinking note + reply) must land in the NEW topic — never
    # in the General (thread None).
    seed_sends = [s for s in tbot.sent if s["text"] in SEED_TEXTS]
    assert seed_sends, f"seed never sent: {tbot.sent}"
    assert all(s["thread"] == new_topic for s in seed_sends), tbot.sent
    assert calls["prompt"] == "[CMD:/menu]"
    # Session bound to the new topic, not the General.
    assert bot._get_session(chat_id, new_topic) == "sid-1"
    assert bot._get_session(chat_id, 0) is None


def test_seed_runs_in_chat_in_private(monkeypatch, tmp_path):
    chat_id = 555
    tbot, calls = _run_spawn(
        monkeypatch, tmp_path, chat_type="private", chat_id=chat_id, topic_id=0
    )
    # Private chat: no forum topic; seed runs inline (thread None == topic 0).
    seed_sends = [s for s in tbot.sent if s["text"] in SEED_TEXTS]
    assert seed_sends, f"seed never sent: {tbot.sent}"
    assert all(s["thread"] is None for s in seed_sends), tbot.sent
    assert bot._get_session(chat_id, 0) == "sid-1"
