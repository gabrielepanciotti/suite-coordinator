"""#66: after the human answers from Telegram, the agents' replies and the
closing of the conversation reach the topic, each with a link to the Network.
Since the "one message per conversation" rework they land in a single card,
edited in place; only a new ask posts a new message (and deletes the old one)."""
import asyncio
import importlib

import pytest

import hub

UI = "http://100.101.15.47:3336"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOPICS_PATH", str(tmp_path / "h.json"))
    hub_store = importlib.reload(__import__("hub_store"))
    monkeypatch.setattr(hub, "hub_store", hub_store)
    hub_store.upsert_conv(338, tenant="acme", chat_id=-100, topic_id=77,
                          counterpart="portale-dev-linux")
    tg = {"posted": [], "edited": [], "deleted": [], "closed": [], "fail_edit": False}
    bus = []

    class FakeBot:
        def __init__(self):
            self.next_id = 500

        async def close_forum_topic(self, chat_id, message_thread_id):
            tg["closed"].append((chat_id, message_thread_id))

        async def send_message(self, chat_id, text, message_thread_id=None, **kw):
            self.next_id += 1
            tg["posted"].append((chat_id, message_thread_id, text))
            return type("M", (), {"message_id": self.next_id})()

        async def edit_message_text(self, text, chat_id, message_id, **kw):
            if tg["fail_edit"]:
                raise RuntimeError("Bad Request: message to edit not found")
            tg["edited"].append((chat_id, message_id, text))

        async def delete_message(self, chat_id, message_id):
            tg["deleted"].append((chat_id, message_id))

    class FakeClient:
        def __init__(self, *a):
            pass

        async def send_message(self, conv_id, body, needs_human=False, context=None):
            bus.append((conv_id, body))

    monkeypatch.setattr(hub.hub_client, "HubClient", FakeClient)
    monkeypatch.setitem(hub._S, "config", hub.hub_config.HubConfig(
        hub_url="http://h", api_keys={"acme": "k"},
        telegram_agent="owner-telegram", telegram_chat_id=-100,
        local_agents={}, turn_cap=12, network_ui_url=UI))
    monkeypatch.setitem(hub._S, "bot", FakeBot())
    monkeypatch.setitem(hub._S, "clients", {})
    return hub_store, tg, bus


def _msg(frm, body, needs_human=False):
    return {"conversation_id": 338, "from_agent": frm, "body": body,
            "needs_human": needs_human, "message_id": 1}


def _ev(m):
    asyncio.run(hub._handle_human_event("acme", "new_message", m))


def test_agent_reply_not_forwarded_before_human_answers(env):
    _, tg, _ = env
    _ev(_msg("simulatore-dev-linux", "Ricevuto"))
    assert tg["posted"] == [] and tg["edited"] == []


def test_ask_posts_one_card_with_link(env):
    hub_store, tg, _ = env
    _ev(_msg("portale-dev-linux", "Quale?", True))
    assert len(tg["posted"]) == 1
    chat, topic, text = tg["posted"][0]
    assert (chat, topic) == (-100, 77)
    assert "<b>portale-dev-linux</b> ha bisogno di te" in text and "Quale?" in text
    assert f'href="{UI}/conversations/338"' in text
    assert hub_store.get_conv(338)["card_msg_id"] == 501


def test_dropped_card_is_saved_without_traceback(env, monkeypatch, caplog):
    hub_store, tg, _ = env

    async def dropped_by_message_guard(*_args, **_kwargs):
        return None

    monkeypatch.setattr(hub._S["bot"], "send_message", dropped_by_message_guard)
    card = {"ask_from": "portale-dev-linux", "ask": "Quale?"}
    with caplog.at_level("WARNING", logger="coordinator"):
        asyncio.run(hub._show_card(338, card, notify=True))

    assert tg["posted"] == []
    assert hub_store.get_conv(338)["card"] == card
    assert "card for #338 not sent (dropped by message guard)" in caplog.text
    assert "card post failed" not in caplog.text


def test_agent_reply_after_human_answer_edits_the_card(env):
    hub_store, tg, bus = env
    _ev(_msg("portale-dev-linux", "Quale?", True))
    assert asyncio.run(hub.handle_topic_reply(-100, 77, "solo fuori tolleranza"))
    assert bus == [(338, "solo fuori tolleranza")]
    _ev(_msg("simulatore-dev-linux", "Ricevuto, procedo"))
    assert len(tg["posted"]) == 1  # no new message
    chat, msg_id, text = tg["edited"][-1]
    assert (chat, msg_id) == (-100, 501)
    assert "Quale?" in text
    assert "<b>simulatore-dev-linux</b>: Ricevuto, procedo" in text
    assert f'href="{UI}/conversations/338"' in text


def test_card_keeps_only_the_last_replies(env):
    hub_store, tg, _ = env
    _ev(_msg("portale-dev-linux", "Quale?", True))
    hub_store.mark_human_replied(338)
    for i in range(5):
        _ev(_msg("simulatore-dev-linux", f"passo {i}"))
    text = tg["edited"][-1][2]
    assert "passo 0" not in text and "passo 1" not in text
    assert all(f"passo {i}" in text for i in (2, 3, 4))


def test_forwarded_body_is_truncated(env):
    hub_store, tg, _ = env
    _ev(_msg("portale-dev-linux", "Quale?", True))
    hub_store.mark_human_replied(338)
    _ev(_msg("simulatore-dev-linux", "x" * 5000))
    text = tg["edited"][-1][2]
    assert "x" * hub.FORWARD_BODY_MAX + "…" in text
    assert "x" * (hub.FORWARD_BODY_MAX + 1) not in text


def test_new_ask_reposts_and_deletes_the_old_card(env):
    hub_store, tg, _ = env
    _ev(_msg("portale-dev-linux", "Quale?", True))
    _ev(_msg("portale-dev-linux", "E la data?", True))
    assert len(tg["posted"]) == 2
    assert tg["deleted"] == [(-100, 501)]
    assert "Quale?" not in tg["posted"][1][2] and "E la data?" in tg["posted"][1][2]
    assert hub_store.get_conv(338)["card_msg_id"] == 502


def test_empty_follow_up_ask_changes_nothing(env):
    _, tg, _ = env
    _ev(_msg("portale-dev-linux", "Quale?", True))
    _ev(_msg("portale-dev-linux", "   ", True))
    assert len(tg["posted"]) == 1 and tg["deleted"] == []


def test_failed_edit_reposts(env):
    hub_store, tg, _ = env
    _ev(_msg("portale-dev-linux", "Quale?", True))
    hub_store.mark_human_replied(338)
    tg["fail_edit"] = True
    _ev(_msg("simulatore-dev-linux", "fatto"))
    assert len(tg["posted"]) == 2 and "fatto" in tg["posted"][1][2]
    assert hub_store.get_conv(338)["card_msg_id"] == 502


def test_own_echo_and_auto_notice_not_forwarded(env):
    hub_store, tg, _ = env
    hub_store.mark_human_replied(338)
    _ev(_msg("owner-telegram", "eco"))
    _ev(_msg("x-agent", hub._auto_notice("muto")))
    assert tg["posted"] == [] and tg["edited"] == []


def test_resolved_edits_the_card_then_closes(env):
    _, tg, _ = env
    _ev(_msg("portale-dev-linux", "Quale?", True))
    asyncio.run(hub._handle_status("acme", {"conversation_id": 338,
                                              "new_status": "resolved",
                                              "changed_by": "portale-dev-linux"}))
    assert tg["closed"] == [(-100, 77)]
    assert len(tg["posted"]) == 1
    text = tg["edited"][-1][2]
    assert "Conversazione chiusa da <b>portale-dev-linux</b>" in text
    assert f"{UI}/conversations/338" in text


def test_no_link_when_ui_url_unset(env, monkeypatch):
    cfg = hub._S["config"]
    monkeypatch.setitem(hub._S, "config", hub.hub_config.HubConfig(
        **{**cfg.__dict__, "network_ui_url": ""}))
    assert hub._conv_link(338) == ""


def test_network_ui_url_from_env(monkeypatch):
    monkeypatch.setenv("NETWORK_UI_URL", UI + "/")
    assert hub.hub_config.load_config().network_ui_url == UI
