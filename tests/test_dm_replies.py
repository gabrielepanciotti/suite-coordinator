"""#55 — a private reply goes back to the node that asked.

The 2026-09-21 case: Health sent a member a `destination: dm` notification
(`health-member-raccolta-vincoli`), a member answered twice in the bot's private chat,
and both answers stopped in messages.jsonl. These tests pin the whole chain:
the send opens a question, the reply finds it, and the reply reaches the node
(bus) or at least an admin — never nobody.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import dm_replies
import notif_store
import scheduler
from notifications import Notification

T0 = datetime(2026, 9, 21, 20, 0, 0)


def _notif(key="health-member-raccolta-vincoli", params=None):
    return Notification(key, "Raccolta vincoli", "static", "0 8 * * *",
                        ("member",), True, params if params is not None else {"text": "Q?"})


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_replies, "PENDING_PATH", str(tmp_path / "dm_pending.json"))
    monkeypatch.setattr(dm_replies, "REPLIES_PATH", str(tmp_path / "dm_replies.jsonl"))
    return tmp_path


# --- which node owns a notification -----------------------------------------

def test_reply_node_from_key_prefix():
    assert dm_replies.reply_node(_notif()) == "health"


def test_reply_node_prefers_explicit_then_project():
    assert dm_replies.reply_node(_notif(params={"project": "scheduler"})) == "scheduler"
    assert dm_replies.reply_node(_notif(params={"project": "x", "reply_to": "cosmos"})) == "cosmos"


@pytest.mark.parametrize("off", [False, "", "false", "off"])
def test_reply_node_opt_out(off):
    assert dm_replies.reply_node(_notif(params={"reply_to": off})) is None


# --- open / find / expire ------------------------------------------------------

def test_sent_question_is_found_by_the_reply(store):
    assert dm_replies.record_sent(_notif(), 6986657990, "Quali vincoli?", is_admin=False, now=T0)
    e = dm_replies.pending_for(6986657990, T0 + timedelta(minutes=79))
    assert e["node"] == "health" and e["key"] == "health-member-raccolta-vincoli"
    assert e["question"] == "Quali vincoli?"


def test_admin_chat_is_not_hijacked_unless_declared(store):
    assert not dm_replies.record_sent(_notif(), 1, "x", is_admin=True, now=T0)
    assert dm_replies.pending_for(1, T0) is None
    assert dm_replies.record_sent(_notif(params={"reply_to": "health"}), 1, "x",
                                  is_admin=True, now=T0)


def test_question_expires_after_ttl(store):
    dm_replies.record_sent(_notif(), 5, "x", is_admin=False, now=T0)
    assert dm_replies.pending_for(5, T0 + timedelta(seconds=dm_replies.TTL_SEC + 1)) is None


def test_followups_keep_going_then_chat_is_released(store):
    # a member re-sent the same answer ~1h later: it must reach the node again.
    dm_replies.record_sent(_notif(), 5, "x", is_admin=False, now=T0)
    dm_replies.mark_forwarded(5, T0 + timedelta(minutes=10))
    assert dm_replies.pending_for(5, T0 + timedelta(minutes=10 + 59)) is not None
    assert dm_replies.pending_for(5, T0 + timedelta(minutes=10 + 61)) is None


def test_replies_ledger_filters_by_node(store):
    dm_replies.log_reply({"node": "health", "text": "a"})
    dm_replies.log_reply({"node": "cosmos", "text": "b"})
    assert [r["text"] for r in dm_replies.read_replies("health")] == ["a"]


# --- scheduler opens the question on a real DM send ----------------------------

def _fire_with(monkeypatch, notif, targets, admin_ids=()):
    monkeypatch.setattr(notif_store, "get", lambda k: notif)
    monkeypatch.setattr(notif_store, "record_run", lambda k, w: None)
    monkeypatch.setattr(notif_store, "record_outcome", lambda *a: None)
    monkeypatch.setattr(scheduler.notifications, "is_banned", lambda n: False)
    monkeypatch.setattr(scheduler.overview_store, "is_paused", lambda p: False)
    monkeypatch.setattr(scheduler.notifications, "resolve_targets", lambda n: targets)
    monkeypatch.setattr(
        scheduler.profiles, "profile_for",
        lambda uid: SimpleNamespace(is_admin=uid in admin_ids),
    )

    async def fake_provider(n, *, run_claude):
        return "Quali vincoli hai?"

    async def fake_send(chat_id, thread_id, text):
        return None

    monkeypatch.setattr(scheduler.providers, "run_provider", fake_provider)
    monkeypatch.setitem(scheduler._S, "send_target", fake_send)
    monkeypatch.setitem(scheduler._S, "run_claude", None)
    return asyncio.run(scheduler._fire(notif.key))


def test_dm_send_opens_question_for_non_admin_only(store, monkeypatch):
    out = _fire_with(monkeypatch, _notif(), [(6986657990, None), (1, None)], admin_ids={1})
    assert out == "inviata"
    now = datetime.now(scheduler._tzobj())
    assert dm_replies.pending_for(6986657990, now)["node"] == "health"
    assert dm_replies.pending_for(1, now) is None


def test_group_send_opens_no_question(store, monkeypatch):
    n = _notif()
    n.destination = "group"
    _fire_with(monkeypatch, n, [(-100123, None)])
    assert dm_replies.pending_for(-100123, datetime.now(scheduler._tzobj())) is None


# --- bot routes the reply ---------------------------------------------------------

class _Msg:
    def __init__(self):
        self.message_id = 601
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _update():
    return SimpleNamespace(effective_message=_Msg(),
                           effective_chat=SimpleNamespace(id=6986657990, type="private"))


def _ctx(sent):
    async def send_message(chat_id, text, **kw):
        sent.append((chat_id, text))
    return SimpleNamespace(bot=SimpleNamespace(send_message=send_message))


def _entry():
    return {"key": "health-member-raccolta-vincoli", "title": "Raccolta vincoli",
            "node": "health", "sent_at": T0.isoformat(), "question": "Q?",
            "forwarded_at": None}


def test_bot_routes_reply_to_bus(store, monkeypatch):
    import bot

    calls = []

    async def fake_forward(agent, subject, body, context):
        calls.append((agent, body, context))
        return {"id": 42}

    monkeypatch.setattr(bot.hub, "forward_dm_reply", fake_forward)
    up, admin_dms = _update(), []
    profile = SimpleNamespace(key="member", label="a member")
    asyncio.run(bot._route_dm_reply(up, _ctx(admin_dms), profile, _entry(), "Niente glutine"))
    agent, body, ctx = calls[0]
    assert agent == "health" and "Niente glutine" in body and "Q?" in body
    assert ctx["notification"] == "health-member-raccolta-vincoli"
    assert admin_dms == []
    assert up.effective_message.replies and "lo passo a" in up.effective_message.replies[0]
    assert dm_replies.read_replies("health")[0]["routed"] == "bus"


def test_bot_falls_back_to_admin_when_bus_refuses(store, monkeypatch):
    import bot

    async def refuse(*a, **k):
        raise RuntimeError("403 unknown agent")

    monkeypatch.setattr(bot.hub, "forward_dm_reply", refuse)
    monkeypatch.setattr(bot.profiles, "admin_user_ids", lambda: [1])
    up, admin_dms = _update(), []
    profile = SimpleNamespace(key="member", label="a member")
    asyncio.run(bot._route_dm_reply(up, _ctx(admin_dms), profile, _entry(), "Niente glutine"))
    assert admin_dms and admin_dms[0][0] == 1 and "Niente glutine" in admin_dms[0][1]
    assert up.effective_message.replies  # the person is still acknowledged
    row = dm_replies.read_replies("health")[0]
    assert row["routed"] == "admin" and row["admins_alerted"] == 1


def test_handle_message_checks_pending_before_topic_binding():
    """Wiring: the check sits before the "not bound to a project" refusal,
    or a private chat with no binding would still swallow the answer."""
    import inspect

    import bot

    src = inspect.getsource(bot.handle_message)
    assert src.index("dm_replies.pending_for(") < src.index("_topic_binding(")
