"""Remote cards land in ONE digest message per destination, edited in place,
instead of one Telegram message per card ("valanga di messaggi")."""
from __future__ import annotations

import pytest

import remote_cards as rc


class _Tg:
    def __init__(self):
        self.next_id = 100
        self.sent, self.edited, self.deleted = [], [], []
        self.fail_edit = False

    async def send(self, chat_id, thread_id, text, buttons):
        self.next_id += 1
        self.sent.append((chat_id, thread_id, text, buttons))
        return self.next_id

    async def edit(self, chat_id, message_id, text, buttons):
        if self.fail_edit:
            raise RuntimeError("Bad Request: message to edit not found")
        self.edited.append((chat_id, message_id, text, buttons))

    async def delete(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


@pytest.fixture
def tg(monkeypatch, tmp_path):
    monkeypatch.setenv("CARD_SOURCES", "tm=http://h:10055")
    monkeypatch.setenv("CARD_SOURCE_TOKEN_TM", "secret")
    monkeypatch.delenv("REMOTE_DIGEST_PATH", raising=False)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    monkeypatch.setattr(rc, "DIGEST_RENOTIFY_SEC", 600)
    fake = _Tg()
    monkeypatch.setitem(rc._S, "send_card", fake.send)
    monkeypatch.setitem(rc._S, "edit_card", fake.edit)
    monkeypatch.setitem(rc._S, "delete_card", fake.delete)
    monkeypatch.setitem(rc._S, "resolve_target", lambda pk: (111, 0))
    rc._dedup_locks.clear()
    return fake


def _card(n: int, text: str | None = None) -> dict:
    return {
        "source": "tm",
        "card_id": f"redmine-{n}",
        "target": {"project_key": ""},
        "text": text or f"Ticket {n}: stampante rotta",
        "buttons": [[{"label": "Prendi", "action": "take"},
                     {"label": "Ignora", "action": "skip"}]],
    }


@pytest.mark.asyncio
async def test_burst_of_cards_is_one_message_edited(tg):
    for n in (43, 44, 45):
        status, body = await rc.show(_card(n), "secret")
        assert status == 200 and body["message_id"] == 101
    assert len(tg.sent) == 1  # one notification for the burst
    text, rows = tg.edited[-1][2], tg.edited[-1][3]
    assert "Card aperte: 3" in text
    assert all(f"[{n}] Ticket {n}" in text for n in (43, 44, 45))
    assert [b["label"] for b in rows[0]] == ["43 · Prendi", "43 · Ignora"]
    assert rows[2][0]["callback_data"] == rc.callback_data("tm", "redmine-45", "take")


@pytest.mark.asyncio
async def test_new_card_after_quiet_period_reposts_and_deletes_old(tg, monkeypatch):
    await rc.show(_card(43), "secret")
    monkeypatch.setattr(rc, "DIGEST_RENOTIFY_SEC", 0)
    status, body = await rc.show(_card(44), "secret")
    assert body["message_id"] == 102
    assert len(tg.sent) == 2 and tg.deleted == [(111, 101)]
    assert "Card aperte: 2" in tg.sent[-1][2]


@pytest.mark.asyncio
async def test_failed_edit_reposts(tg):
    await rc.show(_card(43), "secret")
    tg.fail_edit = True
    await rc.show(_card(44), "secret")
    assert len(tg.sent) == 2 and tg.deleted == [(111, 101)]


@pytest.mark.asyncio
async def test_answer_with_buttons_updates_the_card_in_place(tg):
    await rc.show(_card(43), "secret")
    await rc.show(_card(44), "secret")
    handled = await rc.apply_answer("tm", "redmine-43", {
        "text": "Ticket 43: in carico a vicky",
        "buttons": [[{"label": "Chiudi", "action": "close"}]],
    })
    assert handled
    text, rows = tg.edited[-1][2], tg.edited[-1][3]
    assert "in carico a vicky" in text and "Card aperte: 2" in text
    assert [b["label"] for b in rows[0]] == ["43 · Chiudi"]
    assert len(tg.sent) == 1


@pytest.mark.asyncio
async def test_closed_card_leaves_the_digest(tg):
    await rc.show(_card(43), "secret")
    await rc.show(_card(44), "secret")
    assert await rc.apply_answer("tm", "redmine-43", {"text": "Fatto", "buttons": []})
    text = tg.edited[-1][2]
    assert "Card aperte: 1" in text and "[43]" not in text and "[44]" in text


@pytest.mark.asyncio
async def test_last_card_closed_deletes_the_digest(tg):
    await rc.show(_card(43), "secret")
    assert await rc.apply_answer("tm", "redmine-43", {"text": "Fatto"})
    assert tg.deleted == [(111, 101)]
    # The next card starts a fresh digest.
    await rc.show(_card(44), "secret")
    assert len(tg.sent) == 2


@pytest.mark.asyncio
async def test_toast_only_answer_leaves_the_card(tg):
    await rc.show(_card(43), "secret")
    edits = len(tg.edited)
    assert await rc.apply_answer("tm", "redmine-43", {"toast": "già preso"})
    assert len(tg.edited) == edits and tg.deleted == []


@pytest.mark.asyncio
async def test_card_not_in_digest_is_left_to_the_caller(tg):
    assert await rc.apply_answer("tm", "redmine-1", {"text": "x"}) is False


@pytest.mark.asyncio
async def test_destinations_have_separate_digests(tg, monkeypatch):
    monkeypatch.setitem(rc._S, "resolve_target", lambda pk: (222, 9) if pk == "x" else (111, 0))
    await rc.show(_card(43), "secret")
    other = _card(44)
    other["target"] = {"project_key": "x"}
    await rc.show(other, "secret")
    assert [(s[0], s[1]) for s in tg.sent] == [(111, 0), (222, 9)]


def test_digest_shows_newest_cards_and_counts_hidden():
    cards = [
        {"source": "tm", "card_id": f"redmine-{n}", "text": "t", "buttons": []}
        for n in range(rc.DIGEST_MAX_CARDS + 4)
    ]
    text, _ = rc.render_digest(cards)
    assert f"Card aperte: {rc.DIGEST_MAX_CARDS + 4}" in text
    assert "[0]" not in text and f"[{rc.DIGEST_MAX_CARDS + 3}]" in text
    assert "e altre 4 più vecchie" in text


def test_long_card_text_is_clipped():
    text, _ = rc.render_digest(
        [{"source": "tm", "card_id": "redmine-1", "text": "x" * 5000, "buttons": []}]
    )
    assert len(text) < rc.DIGEST_CARD_TEXT_MAX + 100 and text.endswith("…")
