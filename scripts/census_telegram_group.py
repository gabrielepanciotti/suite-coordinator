#!/usr/bin/env python3
"""Censimento del gruppo Telegram -> person/identity/membership di Life.

Perche' esiste (Task 14): il gate Life di un'istanza si accende solo quando
*ogni* membro attuale del gruppo ha una persona nell'anagrafe. Un censimento
fatto a occhio lascia fuori proprio chi non ha ancora scritto, e il gate lo
rifiuterebbe al primo messaggio. Questo script:

1. legge gli amministratori dal Bot API e i mittenti dall'archivio del bot;
2. verifica con ``getChatMember`` ogni candidato (i bot contano come membri,
   ma non diventano persone);
3. rifiuta di scrivere se il numero dei membri verificati non copre
   ``getChatMemberCount`` — niente "100%" dichiarato a parole;
4. con ``--apply`` crea persona + identita' telegram + membership, e lascia un
   report JSON con l'elenco esatto di cosa ha visto.

Uso (dentro il container del bot, dove ci sono token e archivio):

    python scripts/census_telegram_group.py \
        --chat-id -1003875691522 --instance comunity \
        --person 5429690501:the owner:admin:admin \
        --person 1382883269:Alice:admin:contributor \
        --report /app/data/census/comunity.json --apply

Il formato ``--person`` e' ``telegram:nome:ruolo_istanza:ruolo_life``. Chi non
e' elencato ma risulta membro umano viene censito come ``admin`` dell'istanza e
``contributor`` su Life (spec 9.4: nessuno perde poteri al passaggio).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

TELEGRAM_API = "https://api.telegram.org"
DEFAULT_DATA = os.environ.get("COORDINATOR_DATA_DIR", "/app/data")
ANONYMOUS_BOT = 1087968824  # GroupAnonymousBot: scrive nei gruppi, non e' un membro
MEMBER_STATUSES = {"creator", "administrator", "member"}


class CensusError(RuntimeError):
    pass


@dataclass
class Person:
    telegram: int
    name: str
    instance_role: str = "admin"
    life_role: str = "contributor"
    person_id: int | None = None
    is_bot: bool = False


@dataclass
class Census:
    chat_id: int
    member_count: int = 0
    verified: list[dict] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    # Older operators have identities and instance roles but no Life-scope
    # membership route (/api/life/members): the gate only reads the instance
    # role, so we proceed and say so in the report instead of inventing a role.
    life_scope_supported: bool = True

    @property
    def humans(self) -> list[Person]:
        return [p for p in self.people if not p.is_bot]

    @property
    def coverage(self) -> int:
        return len(self.verified)

    @property
    def complete(self) -> bool:
        return self.member_count > 0 and self.coverage == self.member_count


def archived_senders(path: Path | str, chat_id: int) -> dict[int, str]:
    """``telegram_id -> display name`` for everyone who wrote in the archived chat."""
    out: dict[int, str] = {}
    path = Path(path)
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("chat") != chat_id:
            continue
        uid = row.get("da_id")
        if not isinstance(uid, int) or uid == ANONYMOUS_BOT:
            continue
        out.setdefault(uid, str(row.get("da") or f"utente {uid}"))
    return out


def parse_people(raw: list[str], archived: dict[int, str]) -> list[Person]:
    """Explicit ``--person`` entries first, then archived senders not named yet."""
    people: dict[int, Person] = {}
    for spec in raw:
        parts = spec.split(":")
        if len(parts) != 4:
            raise CensusError(f"--person {spec!r}: serve telegram:nome:ruolo_istanza:ruolo_life")
        uid, name, instance_role, life_role = parts
        people[int(uid)] = Person(int(uid), name, instance_role, life_role)
    for uid, name in archived.items():
        people.setdefault(uid, Person(uid, name))
    return list(people.values())


async def telegram_call(token: str, method: str, *, transport=None, **params) -> dict:
    async with httpx.AsyncClient(timeout=15.0, transport=transport) as c:
        r = await c.get(f"{TELEGRAM_API}/bot{token}/{method}", params=params)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not body.get("ok"):
        raise CensusError(f"Telegram {method}: {body.get('description') or r.status_code}")
    return body.get("result")


async def collect(*, token: str, chat_id: int, people: list[Person],
                  transport=None) -> Census:
    """Verify every candidate against the live group; never guess the rest."""
    count = await telegram_call(token, "getChatMemberCount", transport=transport,
                                chat_id=chat_id)
    admins = await telegram_call(token, "getChatAdministrators", transport=transport,
                                 chat_id=chat_id)
    candidates: dict[int, str] = {p.telegram: p.name for p in people}
    for entry in admins or []:
        user = entry.get("user") or {}
        if isinstance(user.get("id"), int):
            candidates.setdefault(user["id"], user.get("first_name") or "?")
    census = Census(chat_id=chat_id, member_count=int(count or 0))
    known = {p.telegram: p for p in people}
    for uid, name in candidates.items():
        member = await telegram_call(token, "getChatMember", transport=transport,
                                     chat_id=chat_id, user_id=uid)
        status = (member or {}).get("status")
        if status not in MEMBER_STATUSES:
            continue
        user = (member or {}).get("user") or {}
        is_bot = bool(user.get("is_bot"))
        display = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or name
        census.verified.append({"telegram": uid, "name": display, "is_bot": is_bot,
                                "status": status})
        if is_bot:
            continue
        person = known.get(uid) or Person(uid, display)
        person.name = person.name or display
        census.people.append(person)
    if not census.complete:
        raise CensusError(
            f"censimento incompleto: {census.coverage}/{census.member_count} membri verificati. "
            "I membri non identificati vanno aggiunti con --person (o fatti scrivere nel gruppo) "
            "prima di accendere il gate.")
    return census


async def life_call(base: str, token: str, method: str, path: str,
                    body: dict | None = None, *, transport=None) -> tuple[int, object]:
    async with httpx.AsyncClient(timeout=15.0, transport=transport) as c:
        r = await c.request(method, f"{base.rstrip('/')}{path}", json=body,
                            headers={"X-Life-Token": token})
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data


async def existing_members(base: str, token: str, instance: str, *,
                           transport=None) -> list[dict]:
    _, instance_members = await life_call(base, token, "GET",
                                          f"/api/instances/{instance}/members",
                                          transport=transport)
    _, life_members = await life_call(base, token, "GET", "/api/life/members",
                                      transport=transport)
    out = []
    for row in instance_members or []:
        if isinstance(row, dict):
            out.append(row)
    for row in life_members or []:
        if isinstance(row, dict):
            out.append(row)
    return out


async def apply(base: str, token: str, instance: str, census: Census, *,
                transport=None) -> Census:
    """Create people and memberships; refuse to run on partial coverage."""
    if not census.complete:
        raise CensusError("copertura incompleta: non scrivo nulla")
    existing = await existing_members(base, token, instance, transport=transport)
    by_name = {str(row.get("display_name")): row for row in existing}
    for person in census.humans:
        known = by_name.get(person.name)
        if known is not None:
            person.person_id = int(known["person_id"])
        else:
            status, data = await life_call(
                base, token, "POST", "/api/people",
                {"display_name": person.name,
                 "identities": [{"provider": "telegram", "subject": str(person.telegram)}]},
                transport=transport)
            if status not in (200, 201):
                detail = data.get("detail") if isinstance(data, dict) else ""
                raise CensusError(f"persona {person.name}: HTTP {status} {detail}")
            person.person_id = int(data["person_id"])
        status, data = await life_call(
            base, token, "PUT", f"/api/instances/{instance}/members/{person.person_id}",
            {"role": person.instance_role, "scope": "instance"}, transport=transport)
        if status not in (200, 201):
            detail = data.get("detail") if isinstance(data, dict) else ""
            raise CensusError(f"{person.name} su {instance}: HTTP {status} {detail}")
        status, data = await life_call(
            base, token, "PUT", f"/api/life/members/{person.person_id}",
            {"role": person.life_role, "scope": "life"}, transport=transport)
        if status == 404:
            # The operator predates the Life-scope route (Life Task 11 rollout):
            # identity + instance role is all the gate reads, so keep going.
            census.life_scope_supported = False
        elif status not in (200, 201):
            detail = data.get("detail") if isinstance(data, dict) else ""
            raise CensusError(f"{person.name} su Life: HTTP {status} {detail}")
    return census


def report(census: Census, instance: str) -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "chat_id": census.chat_id,
        "instance": instance,
        "member_count": census.member_count,
        "verified": census.verified,
        "coverage": f"{census.coverage}/{census.member_count}",
        "complete": census.complete,
        "life_scope_supported": census.life_scope_supported,
        "people": [
            {"telegram": p.telegram, "name": p.name, "is_bot": p.is_bot,
             "person_id": p.person_id, "instance_role": p.instance_role,
             "life_role": p.life_role}
            for p in census.people
        ],
    }


async def _run(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN_COMUNITY") or ""
    if not token:
        raise CensusError("manca il token Telegram (--token o TELEGRAM_BOT_TOKEN_COMUNITY)")
    archived = archived_senders(args.messages, args.chat_id)
    people = parse_people(args.person, archived)
    census = await collect(token=token, chat_id=args.chat_id, people=people,
                           transport=None)
    if args.apply:
        base = args.life_url or os.environ.get("LIFE_BACKEND_URL") or ""
        admin = args.life_token or os.environ.get("LIFE_ADMIN_TOKEN") or ""
        if not base or not admin:
            raise CensusError("mancano LIFE_BACKEND_URL / LIFE_ADMIN_TOKEN per --apply")
        await apply(base, admin, args.instance, census)
    payload = report(census, args.instance)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Censimento membri Telegram -> Life")
    p.add_argument("--chat-id", type=int, required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--token", help="token del bot (default TELEGRAM_BOT_TOKEN_COMUNITY)")
    p.add_argument("--messages", default=os.path.join(DEFAULT_DATA, "messages.jsonl"),
                   help="archivio messaggi del bot")
    p.add_argument("--person", action="append", default=[],
                   help="telegram:nome:ruolo_istanza:ruolo_life")
    p.add_argument("--life-url", help="default LIFE_BACKEND_URL")
    p.add_argument("--life-token", help="default LIFE_ADMIN_TOKEN")
    p.add_argument("--report", help="dove scrivere il report JSON")
    p.add_argument("--apply", action="store_true", help="scrive persone e membership")
    args = p.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except CensusError as exc:
        print(f"census: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
