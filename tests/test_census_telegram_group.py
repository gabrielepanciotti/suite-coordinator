"""Censimento del gruppo: la copertura 100% e' un fatto verificato, non una promessa."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "census", Path(__file__).resolve().parents[1] / "scripts" / "census_telegram_group.py")
census_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = census_mod
_spec.loader.exec_module(census_mod)

CHAT = -1003875691522
TOKEN = "tok"


def _messages(tmp_path):
    path = tmp_path / "messages.jsonl"
    rows = [
        {"chat": CHAT, "da_id": 8316554911, "da": "Coordinator"},
        {"chat": CHAT, "da_id": 1087968824, "da": "Group"},  # anonimo: non e' un membro
        {"chat": -1, "da_id": 999, "da": "altrove"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


def test_archived_senders_ignora_anonimo_e_altre_chat(tmp_path):
    got = census_mod.archived_senders(_messages(tmp_path), CHAT)
    assert got == {8316554911: "Coordinator"}


def test_parse_people_espliciti_prima_degli_archiviati():
    people = census_mod.parse_people(
        ["1:Nome:admin:contributor"], {1: "vecchio", 2: "Archiviato"})
    assert people[0].name == "Nome" and people[0].instance_role == "admin"
    assert people[1].telegram == 2 and people[1].life_role == "contributor"


def test_parse_people_rifiuta_una_riga_malformata():
    with pytest.raises(census_mod.CensusError):
        census_mod.parse_people(["1:Nome"], {})


def _telegram_transport(members):
    def handler(request):
        path = request.url.path
        if path.endswith("/getChatMemberCount"):
            return httpx.Response(200, json={"ok": True, "result": len(members)})
        if path.endswith("/getChatAdministrators"):
            admins = [{"user": {"id": uid, "first_name": info["name"],
                                "is_bot": info["bot"]}}
                      for uid, info in members.items() if info["admin"]]
            return httpx.Response(200, json={"ok": True, "result": admins})
        if path.endswith("/getChatMember"):
            uid = int(request.url.params["user_id"])
            info = members.get(uid)
            if info is None:
                return httpx.Response(200, json={"ok": True, "result": {"status": "left"}})
            return httpx.Response(200, json={"ok": True, "result": {
                "status": "administrator" if info["admin"] else "member",
                "user": {"id": uid, "first_name": info["name"], "is_bot": info["bot"]}}})
        return httpx.Response(404, json={"ok": False, "description": "no"})
    return httpx.MockTransport(handler)


MEMBERS = {
    1: {"name": "the owner", "bot": False, "admin": True},
    2: {"name": "Alice", "bot": False, "admin": True},
    3: {"name": "ComUnity", "bot": True, "admin": True},
    4: {"name": "Coordinator", "bot": True, "admin": False},
}


def test_collect_copre_tutti_i_membri_e_ignora_i_bot_come_persone(tmp_path):
    people = census_mod.parse_people(
        ["1:the owner:admin:admin", "2:Alice:admin:contributor"],
        {4: "Coordinator"})
    c = asyncio.run(census_mod.collect(
        token=TOKEN, chat_id=CHAT, people=people,
        transport=_telegram_transport(MEMBERS)))
    assert c.complete and c.coverage == 4
    assert {p.telegram for p in c.humans} == {1, 2}
    assert {v["telegram"] for v in c.verified} == {1, 2, 3, 4}


def test_collect_rifiuta_un_membro_non_identificato(tmp_path):
    people = census_mod.parse_people(["1:the owner:admin:admin"], {})
    with pytest.raises(census_mod.CensusError, match="incompleto"):
        asyncio.run(census_mod.collect(
            token=TOKEN, chat_id=CHAT, people=people,
            transport=_telegram_transport(MEMBERS)))


def test_existing_members_legge_la_lista_dell_operatore(monkeypatch):
    """La risposta e' una lista JSON: scartarla faceva ri-creare persone esistenti (409)."""
    async def fake_life_call(base, token, method, path, body=None, **kw):
        if path == "/api/instances/comunity/members":
            return 200, [{"person_id": 1, "display_name": "the owner"}]
        return 404, {"detail": "Not Found"}

    monkeypatch.setattr(census_mod, "life_call", fake_life_call)
    rows = asyncio.run(census_mod.existing_members("http://life", "k", "comunity"))
    assert rows == [{"person_id": 1, "display_name": "the owner"}]


def test_apply_crea_persone_e_membership(monkeypatch):
    calls = []

    async def fake_existing(base, token, instance, **kw):
        return []

    async def fake_life_call(base, token, method, path, body=None, **kw):
        calls.append((method, path, body))
        if path == "/api/people":
            return 201, {"person_id": 100 if body["display_name"] == "the owner" else 200}
        return 200, {}

    monkeypatch.setattr(census_mod, "existing_members", fake_existing)
    monkeypatch.setattr(census_mod, "life_call", fake_life_call)
    c = census_mod.Census(chat_id=CHAT, member_count=2,
                          verified=[{"telegram": 1}, {"telegram": 2}],
                          people=[census_mod.Person(1, "the owner", "admin", "admin"),
                                  census_mod.Person(2, "Alice", "admin", "contributor")])
    asyncio.run(census_mod.apply("http://life", "k", "comunity", c))
    paths = [x[1] for x in calls]
    assert paths.count("/api/people") == 2
    assert "/api/instances/comunity/members/100" in paths
    assert "/api/life/members/200" in paths
    assert c.life_scope_supported


def test_apply_salta_il_life_scope_se_l_operatore_non_lo_conosce(monkeypatch):
    """Il gateway legge il ruolo d'istanza: un operatore piu' vecchio non blocca."""
    async def fake_existing(base, token, instance, **kw):
        return [{"person_id": 1, "display_name": "the owner", "role": "admin"}]

    async def fake_life_call(base, token, method, path, body=None, **kw):
        if path.startswith("/api/life/members"):
            return 404, {"detail": "Not Found"}
        return 200, {}

    monkeypatch.setattr(census_mod, "existing_members", fake_existing)
    monkeypatch.setattr(census_mod, "life_call", fake_life_call)
    c = census_mod.Census(chat_id=CHAT, member_count=1,
                          verified=[{"telegram": 1}],
                          people=[census_mod.Person(1, "the owner", "admin", "admin")])
    asyncio.run(census_mod.apply("http://life", "k", "comunity", c))
    assert not c.life_scope_supported
    assert c.people[0].person_id == 1
