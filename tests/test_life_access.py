"""Accesso per istanza dall'operatore Life (Coordinator#83, decisioni del 16/10/2026).

Il contratto in due metà: ``life_access`` legge le risposte dell'operatore (pura,
più il client con la cache), ``bot.life_access_gate`` decide prima di svegliare
chiunque. Il caso vero da cui nasce è Alice: admin di comunity e nient'altro, con
un id Telegram che all'inizio Life non conosce.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from telegram.ext import ApplicationHandlerStop

import bot
import life_access
import profiles

COMUNITY_CHAT = -1003875691522
PERSONAL_CHAT = -100777
OWNER = 5429690501
ALICE = 4242


# --- life_access: il contratto dell'operatore --------------------------------
def test_200_concede_con_ruolo_e_persona():
    d = life_access.parse_response(
        200, {"token": "t", "person_id": 7, "display_name": "Alice", "role": "admin"}, "comunity")
    assert d.ok and d.role == "admin" and d.person_id == 7 and d.display_name == "Alice"


def test_403_identita_sconosciuta_e_distinta_dal_rifiuto():
    assert life_access.parse_response(
        403, {"detail": "unknown identity"}, "comunity").status == life_access.UNKNOWN
    assert life_access.parse_response(
        403, {"detail": "not a member of personal"}, "personal").status == life_access.DENIED


@pytest.mark.parametrize("code", [401, 500, 503])
def test_operatore_guasto_chiude(code):
    """Una chiave sbagliata o un operatore giù non aprono mai: fail closed."""
    d = life_access.parse_response(code, {"detail": "x"}, "comunity")
    assert d.status == life_access.UNAVAILABLE and not d.ok


def test_livelli_come_nel_registro_di_life():
    assert life_access.levels_of("admin") == ("use", "propose", "edit", "publish")
    assert life_access.levels_of("contributor") == ("use", "propose")
    assert life_access.levels_of("creator") == ("use", "propose")
    assert life_access.levels_of("viewer") == ("use",)
    assert life_access.levels_of("player") == ("use",)
    assert life_access.role_allows("viewer", "use")
    assert not life_access.role_allows("viewer", "propose")
    assert life_access.role_allows("contributor", "propose")
    assert not life_access.role_allows("contributor", "edit")
    assert life_access.role_allows("admin", "publish")
    assert not life_access.role_allows("player", "publish")
    assert not life_access.role_allows(None, "use")


def test_ruoli_moderni_e_legacy_hanno_la_stessa_forma():
    assert life_access.canonical_role("viewer") == "viewer"
    assert life_access.canonical_role("player") == "viewer"
    assert life_access.canonical_role("creator") == "contributor"
    d = life_access.parse_response(200, {"person_id": 1, "role": "player"}, "comunity")
    assert d.role == "viewer"


def test_spento_senza_url_o_chiave():
    assert not life_access.configured({})
    assert not life_access.configured({"LIFE_ACCESS_URL": "http://x"})
    assert life_access.configured({"LIFE_ACCESS_URL": "http://x", "LIFE_ACCESS_EDGE_KEY": "k"})


def _client(handler, clock):
    return life_access.AccessClient("http://op", "edge-k", ttl=60,
                                    clock=clock, transport=httpx.MockTransport(handler))


def test_client_manda_la_chiave_edge_e_ricorda_la_risposta():
    seen = []

    def handler(request):
        seen.append((request.headers.get("X-Life-Edge-Key"), request.read()))
        return httpx.Response(200, json={"person_id": 7, "role": "admin", "display_name": "Y"})

    now = [0.0]
    c = _client(handler, lambda: now[0])
    assert asyncio.run(c.check("4242", "comunity")).ok
    assert asyncio.run(c.check("4242", "comunity")).ok
    assert len(seen) == 1 and seen[0][0] == "edge-k"
    assert b'"provider":"telegram"' in seen[0][1].replace(b" ", b"")
    now[0] = 61
    asyncio.run(c.check("4242", "comunity"))
    assert len(seen) == 2


def test_client_non_ricorda_un_operatore_irraggiungibile():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("down")

    c = _client(handler, lambda: 0.0)
    assert asyncio.run(c.check("1", "comunity")).status == life_access.UNAVAILABLE
    asyncio.run(c.check("1", "comunity"))
    assert len(calls) == 2


def test_propose_posta_la_coda_con_author_person_id():
    seen = []

    def handler(request):
        seen.append((request.headers.get("X-Life-Token"), request.read()))
        return httpx.Response(201, json={"id": 3, "status": "pending"})

    out = asyncio.run(life_access.propose(
        "http://op", "admin-k", "comunity", 7, "node_change",
        {"summary": "x"}, node="bizTech",
        transport=httpx.MockTransport(handler)))
    assert out["id"] == 3
    assert seen[0][0] == "admin-k"
    body = seen[0][1].replace(b" ", b"")
    assert b'"author_person_id":7' in body and b'"kind":"node_change"' in body
    assert b'"node":"bizTech"' in body


def test_propose_rifiutata_col_dettaglio():
    def handler(request):
        return httpx.Response(403, json={"detail": "role 'viewer' does not allow propose"})

    with pytest.raises(life_access.ProposalError) as e:
        asyncio.run(life_access.propose(
            "http://op", "k", "comunity", 7, "brain_note", {"text": "x"},
            transport=httpx.MockTransport(handler)))
    assert e.value.status_code == 403 and "does not allow propose" in e.value.detail


# --- bot: il varco prima di svegliare chiunque --------------------------------
class FakeClient:
    """L'anagrafe di oggi in piccolo: Alice admin di comunity, nient'altro."""

    def __init__(self, members=None):
        self.members = members if members is not None else {}
        self.asked = []

    async def check(self, subject, instance, level="use"):
        self.asked.append((subject, instance, level))
        roles = self.members.get(subject)
        if roles is None:
            return life_access.Decision(life_access.UNKNOWN, instance, reason="unknown identity")
        role = roles.get(instance)
        if role is None:
            return life_access.Decision(life_access.DENIED, instance,
                                        reason=f"not a member of {instance}")
        return life_access.Decision(life_access.GRANTED, instance, role=role,
                                    person_id=7, display_name="Alice")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _update(user_id, chat_id, text="ciao", tipo="supergroup"):
    replies = []

    async def reply_text(t, **kw):
        replies.append(t)

    chat = SimpleNamespace(id=chat_id, type=tipo, title="ComUnity")
    user = SimpleNamespace(id=user_id, is_bot=False, full_name="Alice", username="alice")
    msg = SimpleNamespace(chat=chat, message_thread_id=None, text=text, from_user=user,
                          reply_text=reply_text)
    upd = SimpleNamespace(effective_message=msg, effective_chat=chat, effective_user=user,
                          message=msg, edited_message=None, callback_query=None)
    return upd, replies


@pytest.fixture
def gate(monkeypatch):
    fake = FakeClient({str(ALICE): {"comunity": "admin"}})
    tg = FakeBot()
    monkeypatch.setattr(life_access, "client", lambda env=None: fake)
    monkeypatch.setattr(life_access, "configured", lambda env=None: True)
    owner = profiles.PROFILES[0]
    monkeypatch.setattr(bot, "profile_for", lambda uid: owner if uid == OWNER else None)
    monkeypatch.setattr(profiles, "admin_user_ids", lambda: [OWNER])
    chats = {COMUNITY_CHAT: "comunity", PERSONAL_CHAT: "personal"}
    monkeypatch.setattr(bot, "_binding_instance", lambda chat_id, tid: chats.get(chat_id))
    monkeypatch.setitem(bot.TOPIC_BINDING, (COMUNITY_CHAT, 0), ("comunity", "generale"))
    monkeypatch.setitem(bot.TOPIC_BINDING, (PERSONAL_CHAT, 0), ("finance", "generale"))
    monkeypatch.setattr(bot, "_APPS", {})
    monkeypatch.setattr(bot, "_LIFE_GRANTS", {})
    monkeypatch.setattr(bot, "_UNKNOWN_WARNED", set())
    monkeypatch.setattr(bot, "_REFUSAL_SENT", {})
    return SimpleNamespace(client=fake, bot=tg, ctx=SimpleNamespace(bot=tg))


def _run(gate, upd):
    asyncio.run(bot.life_access_gate(upd, gate.ctx))


def test_owner_non_chiede_niente_a_life(gate):
    upd, _ = _update(OWNER, PERSONAL_CHAT)
    _run(gate, upd)
    assert gate.client.asked == []


def test_id_sconosciuto_rifiutato_e_owner_avvisato_una_volta(gate):
    upd, replies = _update(999, COMUNITY_CHAT)
    with pytest.raises(ApplicationHandlerStop):
        _run(gate, upd)
    assert replies and "Non ti conosco" in replies[0]
    assert len(gate.bot.sent) == 1
    to, text = gate.bot.sent[0]
    assert to == OWNER and "999" in text and "--telegram 999" in text and "comunity" in text
    upd2, replies2 = _update(999, COMUNITY_CHAT)
    with pytest.raises(ApplicationHandlerStop):
        _run(gate, upd2)
    assert len(gate.bot.sent) == 1 and replies2 == []  # niente raffica in un gruppo


def test_membro_comunity_passa_su_comunity_col_suo_ruolo(gate):
    upd, replies = _update(ALICE, COMUNITY_CHAT)
    _run(gate, upd)
    assert replies == []
    assert gate.client.asked == [(str(ALICE), "comunity", "use")]
    p = bot._profile(upd)
    assert p is not None and not p.is_admin
    assert p.projects == ("comunity",) and p.instances == ("comunity",)
    line = bot._access_line(upd)
    assert "admin dell'istanza comunity" in line and "publish" in line


def test_membro_comunity_rifiutato_altrove(gate):
    upd, replies = _update(ALICE, PERSONAL_CHAT)
    with pytest.raises(ApplicationHandlerStop):
        _run(gate, upd)
    assert replies == [bot.LIFE_ACCESS_REFUSALS[life_access.DENIED]]
    assert bot._profile(upd) is None
    assert gate.bot.sent == []  # conosciuto: nessun avviso
    # il rifiuto non nomina nodi né istanze
    assert "finance" not in replies[0] and "personal" not in replies[0]


def test_ruolo_revocato_toglie_il_profilo(gate):
    upd, _ = _update(ALICE, COMUNITY_CHAT)
    _run(gate, upd)
    assert bot._profile(upd) is not None
    gate.client.members[str(ALICE)] = {}
    with pytest.raises(ApplicationHandlerStop):
        _run(gate, upd)
    assert bot._profile(upd) is None


def test_chat_senza_istanza_rifiuta_anche_chi_e_noto(gate, monkeypatch):
    import instances

    monkeypatch.setattr(instances, "default_instance_id", lambda: "personal")
    upd, replies = _update(ALICE, 555, tipo="private")
    with pytest.raises(ApplicationHandlerStop):
        _run(gate, upd)
    assert replies == [bot.LIFE_ACCESS_REFUSALS["no-instance"]]


def test_gruppo_aperto_non_vale_piu_con_life_configurato(gate, monkeypatch):
    monkeypatch.setattr(bot, "OPEN_GROUP_PROJECTS", {"comunity"})
    upd, _ = _update(999, COMUNITY_CHAT)
    assert bot._group_guest_profile(upd) is None


def test_prompt_di_gruppo_porta_il_ruolo():
    p = bot._group_message_prompt("Alice", ALICE, "ciao", None, False, access="[accesso Life] x")
    assert p.startswith("[CMD:/group-message]") and "[accesso Life] x" in p
