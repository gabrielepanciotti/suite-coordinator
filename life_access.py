# life_access.py
"""Per-instance access through Life's operator (#83, decisions of 16/10/2026).

``ALLOWED_USER_IDS`` (the profiles) is for the owners only. Everyone else is a
*person* in Life's registry (Life#122) with a role per instance, and the Coordinator
asks the operator before waking anyone:

    POST <LIFE_ACCESS_URL>/api/access/token   X-Life-Edge-Key: <LIFE_ACCESS_EDGE_KEY>
    {"provider": "telegram", "subject": "<from.id>", "instance": "<id>", "level": "use"}

``200`` grants, with ``role``/``person_id``/``display_name``; ``403`` refuses and its
``detail`` says why — ``"unknown identity"`` is the one case worth telling the owner
about, because only he can link the id to a person. Anything else (401 on our key,
503, network) is the operator being unavailable: **fail closed**, never open.

Roles mirror Life's registry (``access/registry.py``): ``viewer`` (reads, asks),
``contributor`` (proposes; the change waits for an admin), ``admin`` (changes,
approves, ships). Levels are ``use`` (viewer+), ``propose`` (contributor+), ``edit``
and ``publish`` (admin). Legacy ``player``/``creator`` are still understood because
a device can run against an older operator. The local copy of the table only
*reads* a role Life already granted; the grant itself is always Life's.

Unset ``LIFE_ACCESS_URL`` or ``LIFE_ACCESS_EDGE_KEY`` = feature off: the bot keeps
today's behaviour exactly (profiles + ``OPEN_GROUP_PROJECTS``).
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

logger = logging.getLogger("coordinator.life_access")

URL_ENV = "LIFE_ACCESS_URL"
KEY_ENV = "LIFE_ACCESS_EDGE_KEY"
TTL_ENV = "LIFE_ACCESS_TTL_SEC"
DEFAULT_TTL = 60.0
TIMEOUT = 5.0

ROLES: tuple[str, ...] = ("viewer", "contributor", "admin")
LEGACY_ROLES: dict[str, str] = {"player": "viewer", "creator": "contributor"}
LEVELS: dict[str, str] = {
    "use": "viewer",
    "propose": "contributor",
    "edit": "admin",
    "publish": "admin",
}

GRANTED = "granted"
DENIED = "denied"
UNKNOWN = "unknown"          # the id is in nobody's identities
UNAVAILABLE = "unavailable"  # operator unreachable / misconfigured: fail closed


@dataclass(frozen=True)
class Decision:
    status: str
    instance: str
    role: str | None = None
    person_id: int | None = None
    display_name: str | None = None
    reason: str = ""
    # The name the person chose for their assistant (#89): the bot introduces
    # itself and signs with it. Optional in the operator's answer.
    assistant_name: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == GRANTED


def clean_assistant_name(raw: object) -> str | None:
    """A chosen assistant name, safe to put in a prompt: one short line or nothing."""
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.split())[:40]
    return name or None


def canonical_role(role: str | None) -> str | None:
    """The modern spelling of a role; legacy ``player``/``creator`` still work."""
    if role in ROLES:
        return role
    return LEGACY_ROLES.get(role or "")


def role_allows(role: str | None, level: str) -> bool:
    """Whether ``role`` covers ``level`` (same order as Life's registry)."""
    canonical = canonical_role(role)
    if canonical is None or level not in LEVELS:
        return False
    return ROLES.index(canonical) >= ROLES.index(LEVELS[level])


def levels_of(role: str | None) -> tuple[str, ...]:
    """The levels a role covers, lowest first (``admin`` -> use, edit, publish)."""
    return tuple(lv for lv in LEVELS if role_allows(role, lv))


def configured(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool((env.get(URL_ENV) or "").strip() and (env.get(KEY_ENV) or "").strip())


def parse_response(status_code: int, body: object, instance: str) -> Decision:
    """The operator's answer as a :class:`Decision`. Pure, so the contract is testable."""
    data = body if isinstance(body, Mapping) else {}
    if status_code == 200 and data.get("role"):
        pid = data.get("person_id")
        return Decision(GRANTED, instance, role=canonical_role(str(data["role"])),
                        person_id=int(pid) if isinstance(pid, int) else None,
                        display_name=data.get("display_name") or None,
                        assistant_name=clean_assistant_name(data.get("assistant_name")))
    if status_code == 403:
        detail = str(data.get("detail") or "")
        status = UNKNOWN if detail == "unknown identity" else DENIED
        return Decision(status, instance, reason=detail)
    return Decision(UNAVAILABLE, instance, reason=f"HTTP {status_code}")


class AccessClient:
    """Asks the operator, remembering every answer for ``ttl`` seconds.

    A Telegram group sends many messages per person: without the cache each one is
    an HTTP round trip. A revoked role takes effect within ``ttl``. ``unavailable``
    is never cached, so the gate recovers as soon as the operator does.
    """

    def __init__(self, base_url: str, edge_key: str, *, ttl: float = DEFAULT_TTL,
                 clock: Callable[[], float] = time.monotonic,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._key = edge_key
        self._ttl = ttl
        self._clock = clock
        self._transport = transport
        self._cache: dict[tuple[str, str, str], tuple[float, Decision]] = {}

    async def check(self, subject: str, instance: str, level: str = "use") -> Decision:
        cache_key = (subject, instance, level)
        hit = self._cache.get(cache_key)
        now = self._clock()
        if hit is not None and hit[0] > now:
            return hit[1]
        decision = await self._ask(subject, instance, level)
        if decision.status != UNAVAILABLE:
            self._cache[cache_key] = (now + self._ttl, decision)
        return decision

    async def _ask(self, subject: str, instance: str, level: str) -> Decision:
        ask = {"provider": "telegram", "subject": subject, "instance": instance, "level": level}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as c:
                r = await c.post(f"{self._base}/api/access/token", json=ask,
                                 headers={"X-Life-Edge-Key": self._key})
            try:
                body = r.json()
            except ValueError:
                body = {}
            decision = parse_response(r.status_code, body, instance)
        except httpx.HTTPError as exc:
            decision = Decision(UNAVAILABLE, instance, reason=type(exc).__name__)
        if decision.status == UNAVAILABLE:
            logger.error("life access: operator unavailable for %s on %s (%s): fail closed",
                         subject, instance, decision.reason)
        else:
            logger.info("life access: telegram:%s on %s (%s) -> %s %s", subject, instance,
                        level, decision.status, decision.role or decision.reason)
        return decision


_CLIENT: AccessClient | None = None


def client(env: Mapping[str, str] | None = None) -> AccessClient | None:
    """The process-wide client, or ``None`` when the feature is off."""
    global _CLIENT
    env = os.environ if env is None else env
    if not configured(env):
        return None
    if _CLIENT is None:
        try:
            ttl = float(env.get(TTL_ENV) or DEFAULT_TTL)
        except ValueError:
            ttl = DEFAULT_TTL
        _CLIENT = AccessClient(env[URL_ENV].strip(), env[KEY_ENV].strip(), ttl=ttl)
    return _CLIENT


def invite_hint(user_id: int, instance: str | None) -> str:
    """The command the owner runs on the operator to link an unknown Telegram id.

    A dry run by design (Life's ``invite`` writes only with ``--esegui``); the name
    and any other identity of the same person (``--tailscale``, ``--github``) are his.
    """
    inst = instance or "<istanza>"
    return (f"python -m life_backend.access.invite --instance {inst} --role viewer "
            f"--name \"<nome>\" --telegram {user_id}")


PROPOSAL_KINDS = ("brain_note", "node_change", "config_change")


class ProposalError(RuntimeError):
    """The operator refused a proposal; ``detail`` is its human reason."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"proposal -> HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


async def propose(
    base_url: str,
    admin_token: str,
    instance: str,
    person_id: int,
    kind: str,
    payload: Mapping[str, object],
    node: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = TIMEOUT,
) -> dict:
    """A contributor's change request on the operator's review queue (Life Task 11).

    The shared admin secret is used only to *name* the author (``author_person_id``);
    Life checks the author's own role against ``propose`` and stores the proposal as
    pending. It never commits anything on the contributor's behalf.
    """
    body: dict[str, object] = {
        "kind": kind,
        "payload": dict(payload),
        "author_person_id": person_id,
    }
    if node:
        body["node"] = node
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as c:
            r = await c.post(f"{base_url.rstrip('/')}/api/instances/{instance}/proposals",
                             json=body, headers={"X-Life-Token": admin_token})
    except httpx.HTTPError as exc:
        raise ProposalError(0, type(exc).__name__) from None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code not in (200, 201):
        detail = data.get("detail") if isinstance(data, dict) else ""
        raise ProposalError(r.status_code, str(detail or ""))
    return data if isinstance(data, dict) else {}


def node_level_hint(role: str | None) -> str:
    """A short line for the agent prompt: what this role may do, and where."""
    levels = ", ".join(levels_of(role)) or "nessuno"
    if canonical_role(role) == "contributor":
        return (f"livelli {levels}. Una richiesta di modifica non la fai tu: digli di "
                "mandarla con /proponi <testo>, che la mette in coda per un admin.")
    if canonical_role(role) == "viewer":
        return (f"livelli {levels}. Legge e chiede: non modifica, non committa, non pubblica.")
    return f"livelli {levels}."
