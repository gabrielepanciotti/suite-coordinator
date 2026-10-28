"""Tests for remote_cards.py — the generic POST /api/cards protocol (#45).

Uses monkeypatch to stub the bot injection points (`remote_cards._S`) and the
env-driven source registry (`CARD_SOURCES` / `CARD_SOURCE_TOKEN_<ALIAS>`), so
the suite runs with no live bot and no filesystem beyond `tmp_path`.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import remote_cards as rc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _set_sources(monkeypatch, sources="tm=http://h:10055", token="secret"):
    monkeypatch.setenv("CARD_SOURCES", sources)
    monkeypatch.setenv("CARD_SOURCE_TOKEN_TM", token)


def _payload(**overrides) -> dict:
    payload = {
        "source": "tm",
        "card_id": "rm-38",
        "target": {"project_key": "hubOfferte"},
        "text": "🤖 Ticket da lavorare\n…",
        "buttons": [[{"label": "🤖 Prendi in carico", "action": "take"},
                      {"label": "Ignora", "action": "skip"}]],
    }
    payload.update(overrides)
    return payload


async def _fake_send_card(chat_id, thread_id, text, buttons):
    return 999


@pytest.fixture(autouse=True)
def _reset_S():
    rc._S["send_card"] = None
    rc._S["resolve_target"] = None
    rc._dedup_locks.clear()
    yield
    rc._S["send_card"] = None
    rc._S["resolve_target"] = None
    rc._dedup_locks.clear()


# ---------------------------------------------------------------------------
# load_sources
# ---------------------------------------------------------------------------

def test_load_sources_parses_alias_url_and_token(monkeypatch):
    monkeypatch.setenv("CARD_SOURCES", "tm=http://h:10055,other=http://x:1")
    monkeypatch.setenv("CARD_SOURCE_TOKEN_TM", "tok-tm")
    monkeypatch.setenv("CARD_SOURCE_TOKEN_OTHER", "tok-other")
    sources = rc.load_sources()
    assert sources == {
        "tm": ("http://h:10055", "tok-tm"),
        "other": ("http://x:1", "tok-other"),
    }


def test_load_sources_empty_when_unset(monkeypatch):
    monkeypatch.delenv("CARD_SOURCES", raising=False)
    assert rc.load_sources() == {}


def test_load_sources_skips_alias_without_token(monkeypatch):
    monkeypatch.setenv("CARD_SOURCES", "tm=http://h:10055")
    monkeypatch.delenv("CARD_SOURCE_TOKEN_TM", raising=False)
    assert rc.load_sources() == {}


def test_load_sources_reads_from_explicit_env_dict():
    env = {"CARD_SOURCES": "tm=http://h:10055", "CARD_SOURCE_TOKEN_TM": "tok"}
    assert rc.load_sources(env) == {"tm": ("http://h:10055", "tok")}


# ---------------------------------------------------------------------------
# callback_data / parse_callback
# ---------------------------------------------------------------------------

def test_callback_data_roundtrip():
    data = rc.callback_data("tm", "rm-38", "take")
    assert data == "cx:tm:rm-38:take"
    assert rc.parse_callback(data) == ("tm", "rm-38", "take")


def test_callback_data_rejects_invalid_ids():
    with pytest.raises(ValueError):
        rc.callback_data("TM", "rm-38", "take")
    with pytest.raises(ValueError):
        rc.callback_data("tm", "rm 38", "take")
    with pytest.raises(ValueError):
        rc.callback_data("tm", "rm-38", "do/it")


def test_callback_data_rejects_over_64_bytes():
    long_id = "a" * 60
    with pytest.raises(ValueError):
        rc.callback_data("tm", long_id, "take")


def test_parse_callback_rejects_wrong_prefix_or_shape():
    assert rc.parse_callback("rm:take:38") is None
    assert rc.parse_callback("cx:tm:rm-38") is None
    assert rc.parse_callback("cx:TM:rm-38:take") is None


# ---------------------------------------------------------------------------
# show() — auth and validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_show_unknown_source_is_403(monkeypatch, tmp_path):
    monkeypatch.delenv("CARD_SOURCES", raising=False)
    status, body = await rc.show(_payload(), "whatever")
    assert status == 403


@pytest.mark.asyncio
async def test_show_bad_token_is_401(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    status, body = await rc.show(_payload(), "wrong-token")
    assert status == 401


@pytest.mark.asyncio
async def test_show_missing_token_is_401(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    status, body = await rc.show(_payload(), None)
    assert status == 401


@pytest.mark.parametrize("overrides", [
    {"card_id": None},
    {"card_id": "Bad Id"},
    {"text": None},
    {"target": {}},
    {"target": "hubOfferte"},
    {"buttons": [[{"label": "x"}]]},           # missing action
    {"buttons": [[{"label": "x", "action": "not valid"}]]},
    {"buttons": "not-a-list"},
])
@pytest.mark.asyncio
async def test_show_invalid_payload_is_400(monkeypatch, tmp_path, overrides):
    _set_sources(monkeypatch)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    status, body = await rc.show(_payload(**overrides), "secret")
    assert status == 400


@pytest.mark.asyncio
async def test_show_callback_over_64_bytes_is_400(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    payload = _payload(card_id="a" * 60)
    status, body = await rc.show(payload, "secret")
    assert status == 400


@pytest.mark.asyncio
async def test_show_503_when_bot_not_started(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    rc._S["send_card"] = None
    rc._S["resolve_target"] = lambda pk: (111, 0)
    status, body = await rc.show(_payload(), "secret")
    assert status == 503


@pytest.mark.asyncio
async def test_show_422_when_no_target(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    rc._S["send_card"] = _fake_send_card
    rc._S["resolve_target"] = lambda pk: None
    status, body = await rc.show(_payload(), "secret")
    assert status == 422


@pytest.mark.asyncio
async def test_show_success_returns_message_id(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    rc._S["send_card"] = _fake_send_card
    rc._S["resolve_target"] = lambda pk: (111, 0)
    status, body = await rc.show(_payload(), "secret")
    assert status == 200
    assert body == {"ok": True, "message_id": 999}


@pytest.mark.asyncio
async def test_show_passes_resolved_callback_data_to_send_card(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    seen = {}

    async def _capture(chat_id, thread_id, text, buttons):
        seen["chat_id"] = chat_id
        seen["thread_id"] = thread_id
        seen["text"] = text
        seen["buttons"] = buttons
        return 42

    rc._S["send_card"] = _capture
    rc._S["resolve_target"] = lambda pk: (111, 7)
    status, body = await rc.show(_payload(), "secret")
    assert status == 200
    assert seen["chat_id"] == 111
    assert seen["thread_id"] == 7
    assert seen["buttons"][0][0]["callback_data"] == "cx:tm:rm-38:take"
    assert seen["buttons"][0][1]["callback_data"] == "cx:tm:rm-38:skip"


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_show_dedups_same_source_card_id_without_resending(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    calls = []

    async def _counting(chat_id, thread_id, text, buttons):
        calls.append(1)
        return 999

    rc._S["send_card"] = _counting
    rc._S["resolve_target"] = lambda pk: (111, 0)

    status1, body1 = await rc.show(_payload(), "secret")
    status2, body2 = await rc.show(_payload(), "secret")

    assert status1 == status2 == 200
    assert body1 == body2 == {"ok": True, "message_id": 999}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dedup_survives_reload_of_persisted_file(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    path = str(tmp_path / "remote_cards.json")
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", path)
    rc._S["send_card"] = _fake_send_card
    rc._S["resolve_target"] = lambda pk: (111, 0)

    status, body = await rc.show(_payload(), "secret")
    assert status == 200

    # Simulate a fresh process: re-point at the same path (nothing cached
    # in-process besides what's on disk) and call again with the bot NOT
    # even wired for send_card — the dedup hit must short-circuit before
    # ever touching _S["send_card"].
    rc._S["send_card"] = None
    status2, body2 = await rc.show(_payload(), "secret")
    assert status2 == 200
    assert body2 == {"ok": True, "message_id": 999}

    on_disk = json.loads((tmp_path / "remote_cards.json").read_text())
    assert on_disk["tm:rm-38"] == 999


@pytest.mark.asyncio
async def test_show_concurrent_posts_for_same_card_send_once(monkeypatch, tmp_path):
    """Two POSTs for the same (source, card_id) arriving close together (a
    node retrying a slow response) must not both pass the dedup check: the
    load->send->save sequence has an await point (send_card) between the
    dedup read and write, so without a lock both would see a miss and both
    would send — a duplicate Telegram card. Regression for the review finding
    on fix round 1."""
    _set_sources(monkeypatch)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    rc._S["resolve_target"] = lambda pk: (111, 0)

    calls = []
    release = asyncio.Event()

    async def _slow_send(chat_id, thread_id, text, buttons):
        calls.append(1)
        await release.wait()
        return 999

    rc._S["send_card"] = _slow_send

    task1 = asyncio.create_task(rc.show(_payload(), "secret"))
    # Let task1 run past validation and into send_card, where it blocks on
    # `release` while holding the per-card lock.
    for _ in range(5):
        await asyncio.sleep(0)

    task2 = asyncio.create_task(rc.show(_payload(), "secret"))
    # Let task2 run up to (and block on) acquiring the same lock.
    for _ in range(5):
        await asyncio.sleep(0)

    assert len(calls) == 1  # task2 must not have entered send_card yet

    release.set()
    status1, body1 = await task1
    status2, body2 = await task2

    assert status1 == status2 == 200
    assert body1 == body2 == {"ok": True, "message_id": 999}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dedup_map_caps_at_500_dropping_oldest(monkeypatch, tmp_path):
    _set_sources(monkeypatch)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    rc._S["resolve_target"] = lambda pk: (111, 0)

    counter = {"n": 0}

    async def _send(chat_id, thread_id, text, buttons):
        counter["n"] += 1
        return counter["n"]

    rc._S["send_card"] = _send

    for i in range(501):
        await rc.show(_payload(card_id=f"c-{i}"), "secret")

    on_disk = json.loads((tmp_path / "remote_cards.json").read_text())
    assert len(on_disk) == 500
    assert "tm:c-0" not in on_disk
    assert "tm:c-500" in on_disk


# ---------------------------------------------------------------------------
# forward_action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_action_returns_node_json(monkeypatch):
    _set_sources(monkeypatch)

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"text": "fatto", "buttons": []}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None):
            assert url == "http://h:10055/api/cards/rm-38/actions/take"
            assert headers["X-Card-Token"] == "secret"
            return _FakeResp()

    monkeypatch.setattr(rc.httpx, "AsyncClient", lambda *a, **kw: _FakeClient())
    result = await rc.forward_action("tm", "rm-38", "take")
    assert result == {"text": "fatto", "buttons": []}


@pytest.mark.asyncio
async def test_forward_action_unreachable_returns_toast(monkeypatch):
    _set_sources(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None):
            raise TimeoutError("boom")

    monkeypatch.setattr(rc.httpx, "AsyncClient", lambda *a, **kw: _FakeClient())
    result = await rc.forward_action("tm", "rm-38", "take")
    assert result == {"toast": "⚠️ tm non raggiungibile, riprova"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 404, 500, 503])
async def test_forward_action_non_200_returns_toast_not_the_body(monkeypatch, status):
    """A 401/500 body (e.g. FastAPI's {"detail": ...}) is not an answer: it
    must never reach the handler as if the node had replied."""
    _set_sources(monkeypatch)

    class _FakeResp:
        status_code = status

        def json(self):
            return {"detail": "invalid card token", "text": "must not be shown"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None):
            return _FakeResp()

    monkeypatch.setattr(rc.httpx, "AsyncClient", lambda *a, **kw: _FakeClient())
    result = await rc.forward_action("tm", "rm-38", "take")
    assert result == {"toast": "⚠️ tm non raggiungibile, riprova"}


@pytest.mark.asyncio
async def test_forward_action_unknown_source_returns_toast(monkeypatch):
    monkeypatch.delenv("CARD_SOURCES", raising=False)
    result = await rc.forward_action("ghost", "rm-38", "take")
    assert result == {"toast": "⚠️ ghost non raggiungibile, riprova"}


def test_forward_timeout_stays_under_telegram_callback_query_validity():
    """Telegram expires a callback query ~15s after the tap; the bot cannot
    even attempt q.answer() until forward_action returns, so the timeout must
    leave a real margin under that window (round 1 review finding)."""
    assert rc._FORWARD_TIMEOUT_SEC == 12
    assert rc._FORWARD_TIMEOUT_SEC < 15


@pytest.mark.asyncio
async def test_forward_action_uses_the_bounded_timeout(monkeypatch):
    _set_sources(monkeypatch)
    seen_timeout = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"toast": "ok"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None):
            return _FakeResp()

    def _fake_async_client(*a, **kw):
        seen_timeout["timeout"] = kw.get("timeout")
        return _FakeClient()

    monkeypatch.setattr(rc.httpx, "AsyncClient", _fake_async_client)
    await rc.forward_action("tm", "rm-38", "take")
    assert seen_timeout["timeout"] == rc._FORWARD_TIMEOUT_SEC == 12
