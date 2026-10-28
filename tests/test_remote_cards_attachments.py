"""Card attachments (TicketManager#15): text/photo/document messages sent after
the digest, on a dedup miss only, never failing the card."""
from __future__ import annotations

import base64

import pytest

import remote_cards as rc

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class _Tg:
    def __init__(self):
        self.sent, self.attachments = [], []
        self.fail_index: set[int] = set()

    async def send(self, chat_id, thread_id, text, buttons):
        self.sent.append((chat_id, thread_id, text))
        return 500 + len(self.sent)

    async def edit(self, *a):
        pass

    async def delete(self, *a):
        pass

    async def attach(self, chat_id, thread_id, att):
        if len(self.attachments) in self.fail_index:
            self.attachments.append(None)
            raise RuntimeError("Bad Request: IMAGE_PROCESS_FAILED")
        self.attachments.append((chat_id, thread_id, att))


@pytest.fixture
def tg(monkeypatch, tmp_path):
    monkeypatch.setenv("CARD_SOURCES", "tm=http://h:10055")
    monkeypatch.setenv("CARD_SOURCE_TOKEN_TM", "secret")
    monkeypatch.delenv("REMOTE_DIGEST_PATH", raising=False)
    monkeypatch.setattr(rc, "REMOTE_CARDS_PATH", str(tmp_path / "remote_cards.json"))
    fake = _Tg()
    monkeypatch.setitem(rc._S, "send_card", fake.send)
    monkeypatch.setitem(rc._S, "edit_card", fake.edit)
    monkeypatch.setitem(rc._S, "delete_card", fake.delete)
    monkeypatch.setitem(rc._S, "send_attachment", fake.attach)
    monkeypatch.setitem(rc._S, "resolve_target", lambda pk: (111, 7))
    rc._dedup_locks.clear()
    return fake


def _payload(attachments) -> dict:
    return {
        "source": "tm", "card_id": "proposta-15", "target": {"project_key": ""},
        "text": "Proposta pronta", "buttons": [[{"label": "Applica", "action": "apply"}]],
        "attachments": attachments,
    }


ALL = [
    {"type": "text", "text": "Logica backend: ..."},
    {"type": "photo", "name": "dopo.png", "data_b64": _b64(PNG), "caption": "dopo"},
    {"type": "document", "name": "anteprima.md", "data_b64": _b64(b"# diff\n")},
]


@pytest.mark.asyncio
async def test_attachments_follow_the_digest_in_order_same_topic(tg):
    status, body = await rc.show(_payload(ALL), "secret")
    assert status == 200
    assert body == {"ok": True, "message_id": 501,
                    "attachments_sent": 3, "attachments_failed": []}
    assert [a[:2] for a in tg.attachments] == [(111, 7)] * 3
    assert [a[2]["type"] for a in tg.attachments] == ["text", "photo", "document"]
    assert tg.attachments[1][2]["data"] == PNG
    assert tg.attachments[1][2]["caption"] == "dopo"
    assert tg.attachments[2][2]["data"] == b"# diff\n"


@pytest.mark.asyncio
async def test_duplicate_card_does_not_resend_attachments(tg):
    await rc.show(_payload(ALL), "secret")
    status, body = await rc.show(_payload(ALL), "secret")
    assert status == 200 and body == {"ok": True, "message_id": 501}
    assert len(tg.attachments) == 3


@pytest.mark.asyncio
async def test_refused_attachment_does_not_fail_the_card(tg):
    tg.fail_index = {1}
    status, body = await rc.show(_payload(ALL), "secret")
    assert status == 200
    assert body["attachments_sent"] == 2
    assert body["attachments_failed"] == [
        {"index": 1, "error": "Bad Request: IMAGE_PROCESS_FAILED"}
    ]


@pytest.mark.asyncio
async def test_closing_the_card_does_not_touch_attachments(tg):
    deleted = []

    async def _delete(chat_id, message_id):
        deleted.append(message_id)

    rc._S["delete_card"] = _delete
    await rc.show(_payload(ALL), "secret")
    await rc.apply_answer("tm", "proposta-15", {"text": "Applicata", "buttons": []})
    assert deleted == [501]  # only the digest, the attachments stay as the record


@pytest.mark.asyncio
async def test_jpeg_photo_is_accepted(tg):
    att = [{"type": "photo", "name": "a.jpg", "data_b64": _b64(JPEG)}]
    status, body = await rc.show(_payload(att), "secret")
    assert status == 200 and body["attachments_sent"] == 1
    assert tg.attachments[0][2]["caption"] is None


@pytest.mark.asyncio
async def test_no_attachments_keeps_the_old_answer(tg):
    p = _payload(None)
    del p["attachments"]
    status, body = await rc.show(p, "secret")
    assert body == {"ok": True, "message_id": 501}


@pytest.mark.parametrize("attachments,needle", [
    ("x", "expected a list"),
    ([{"type": "video", "name": "a", "data_b64": "AA=="}], "unknown type"),
    ([{"type": "text", "text": "a" * 4097}], "4096"),
    ([{"type": "text", "text": ""}], "invalid text"),
    ([{"type": "photo", "name": "a.png", "data_b64": "@@not-base64@@"}], "not valid base64"),
    ([{"type": "photo", "name": "a.gif", "data_b64": _b64(b"GIF89a....")}], "PNG or JPEG"),
    ([{"type": "photo", "name": "a.png", "data_b64": _b64(PNG), "caption": "c" * 1025}], "1024"),
    ([{"type": "document", "name": "", "data_b64": _b64(b"x")}], "invalid name"),
    ([{"type": "document", "name": "a.md"}], "missing data_b64"),
    ([{"type": "text", "text": "a"}] * 9, "too many"),
])
@pytest.mark.asyncio
async def test_invalid_attachments_refuse_the_card(tg, attachments, needle):
    status, body = await rc.show(_payload(attachments), "secret")
    assert status == 400
    assert needle in body["error"]
    assert tg.sent == [] and tg.attachments == []


@pytest.mark.asyncio
async def test_size_limits(tg, monkeypatch):
    monkeypatch.setattr(rc, "ATTACHMENT_PHOTO_MAX_BYTES", 40)
    monkeypatch.setattr(rc, "ATTACHMENT_DOCUMENT_MAX_BYTES", 10)
    big_photo = [{"type": "photo", "name": "a.png", "data_b64": _b64(PNG + b"\x00" * 10)}]
    status, body = await rc.show(_payload(big_photo), "secret")
    assert status == 400 and "limit 40" in body["error"]
    big_doc = [{"type": "document", "name": "a.md", "data_b64": _b64(b"x" * 11)}]
    status, body = await rc.show(_payload(big_doc), "secret")
    assert status == 400 and "limit 10" in body["error"]
