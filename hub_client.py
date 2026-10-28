# hub_client.py
"""HTTP client for Network: REST wrapper + SSE stream parser.
Auth via X-Agent-Key (strong) or X-Api-Key (legacy tenant key), plus X-Agent-Id
— see Network api/dependencies.py::get_auth_context."""
from __future__ import annotations

import json
import logging
import os
import socket
from typing import AsyncIterator

import httpx

logger = logging.getLogger("coordinator.hub_client")

# Agents already warned about running on the legacy tenant key. Module-level so
# the warning fires once per agent instead of once per HTTP call (the SSE loops
# reconnect forever and would otherwise flood the log).
_DEGRADED_LOGGED: set[str] = set()


def auth_headers(api_key: str, agent_id: str, agent_key: str = "") -> dict[str, str]:
    """Bus auth headers for one acting agent.

    STRONG (preferred): the agent's own key in `X-Agent-Key`. The bus resolves
    the agent from the key alone (dependencies.py path 1) and never reads the
    tenant key, so revoking the tenant key cannot cut this caller off.
    `X-Agent-Id` is still sent: the bus cross-checks it against the key's owner
    and returns 403 on a mismatch, which turns a mis-pasted key into a loud
    error instead of silently acting as somebody else.

    LEGACY (fallback): shared tenant key in `X-Api-Key` + `X-Agent-Id`. Kept so
    a half-configured deployment keeps working mid-rollout — but it is logged at
    WARNING the first time each agent degrades. Never degrade silently: on this
    bus an agent that is registered but never reached looks exactly like a
    healthy idle one, so an unnoticed auth downgrade would stay invisible until
    the tenant key is revoked and everything stops at once.
    """
    if agent_key:
        return {"X-Agent-Key": agent_key, "X-Agent-Id": agent_id}
    if agent_id not in _DEGRADED_LOGGED:
        _DEGRADED_LOGGED.add(agent_id)
        logger.warning(
            "bus auth DEGRADED for agent '%s': no per-agent key configured "
            "(HUB_AGENT_KEYS) - falling back to the shared tenant key. This "
            "call stops working the moment the tenant key is revoked.",
            agent_id,
        )
    return {"X-Api-Key": api_key, "X-Agent-Id": agent_id}


class HubClient:
    def __init__(
        self, base_url: str, api_key: str, agent_id: str, agent_key: str = ""
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = auth_headers(api_key, agent_id, agent_key)

    async def create_conversation(
        self, to: list[str], subject: str, body: str,
        priority: str = "normal", context: dict | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/api/conversations",
                headers=self.headers,
                json={"to": to, "subject": subject, "body": body,
                      "priority": priority, "context": context},
            )
            r.raise_for_status()
            return r.json()

    async def send_message(
        self, conv_id: int, body: str, needs_human: bool = False,
        context: dict | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/api/conversations/{conv_id}/messages",
                headers=self.headers,
                json={"body": body, "needs_human": needs_human, "context": context},
            )
            r.raise_for_status()
            return r.json()

    async def get_bus_epoch(self) -> str:
        """Fetch the bus DB instance fingerprint (GET /api/system/epoch).

        Empty string if the endpoint is missing (older Network) so the caller
        treats it as 'no epoch signal' rather than a hard error."""
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self.base_url}/api/system/epoch", headers=self.headers
            )
            if r.status_code == 404:
                return ""
            r.raise_for_status()
            return str(r.json().get("epoch") or "")

    async def list_agents(self, tenant: str) -> list[dict]:
        """Every agent registered on the bus for a tenant (GET /api/agents).

        Used by the boot-time roster reconciliation: an agent configured in
        HUB_LOCAL_AGENTS that the bus does not know answers 403 to every call,
        forever and per-request, so the only way to see it is to compare the two
        lists once at startup.
        """
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{self.base_url}/api/agents",
                headers=self.headers,
                params={"tenant": tenant},
            )
            r.raise_for_status()
            return r.json()

    async def get_inbox(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{self.base_url}/api/inbox", headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def get_conversation(self, conv_id: int) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.base_url}/api/conversations/{conv_id}", headers=self.headers
            )
            r.raise_for_status()
            return r.json()

    async def set_delivery(
        self, conv_id: int, state: str, *, behind: int | None = None,
        position: int | None = None,
    ) -> dict:
        """Tell the bus where this agent's wake for ``conv_id`` stands
        (Network#35): ``queued`` behind another conversation, ``running``, or
        ``clear``. The agent's own reply clears it on the bus side."""
        body: dict = {"state": state}
        if state == "queued":
            body.update(behind=behind, position=position)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.put(
                f"{self.base_url}/api/conversations/{conv_id}/delivery",
                headers=self.headers,
                json=body,
            )
            r.raise_for_status()
            return r.json()

    async def resolve_conversation(self, conv_id: int) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.patch(
                f"{self.base_url}/api/conversations/{conv_id}",
                headers=self.headers, json={"status": "resolved"},
            )
            r.raise_for_status()
            return r.json()


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict, int]]:
    """Parse an SSE line stream into (event_type, data_dict, event_id).
    Mirrors the framing in Network scripts/hub-listener.py."""
    event_type = data_raw = None
    event_id = 0
    async for line in lines:
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_raw = line[5:].strip()
        elif line.startswith("id:"):
            try:
                event_id = int(line[3:].strip())
            except ValueError:
                event_id = 0
        elif line.startswith(":"):
            continue
        elif line == "":
            if event_type and data_raw:
                try:
                    yield event_type, json.loads(data_raw), event_id
                except json.JSONDecodeError:
                    logger.warning("hub SSE: bad JSON %s", data_raw[:200])
            event_type = data_raw = None
            event_id = 0


def nome_host() -> str:
    """Come questo processo si presenta al bus quando ascolta per un agente.

    Sovrascrivibile con ``HUB_LISTENER_HOST`` perche' dentro un container
    ``gethostname()`` restituisce l'id del container, che non dice a nessuno
    su quale macchina sta: il bus vedeva `?172.24.0.1` e i 14 nomi contesi
    risultavano tenuti da un processo di cui nessuno rispondeva (issue #37).
    """
    return os.environ.get("HUB_LISTENER_HOST") or socket.gethostname()


async def dichiara_proprietario(
    base_url: str, api_key: str, agent_id: str, agent_key: str = "", *, timeout: float = 5.0
) -> bool:
    """Dire al bus quale processo ascolta per questo agente.

    E' *provare*, non promettere: la chiamata e' autenticata con la chiave
    dell'agente, la stessa che serve per ascoltare per lui. Chi non puo'
    ascoltare non puo' nemmeno dichiararsi.

    Best-effort per scelta. La dichiarazione e' diagnosi, non permesso: un bus
    che la rifiuta o non risponde non deve impedire al listener di partire,
    altrimenti un miglioramento dell'osservabilita' diventerebbe un nuovo modo
    di restare muti.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/api/agents/{agent_id}/owner",
                headers=auth_headers(api_key, agent_id, agent_key),
                json={"host": nome_host()},
            )
        resp.raise_for_status()
        return True
    except (httpx.HTTPError, OSError) as exc:
        logger.info("hub: dichiarazione owner non riuscita per %s (%s)", agent_id, exc)
        return False


async def stream_events(
    base_url: str, api_key: str, agent_id: str, last_event_id: int | None = None,
    agent_key: str = "",
) -> AsyncIterator[tuple[str, dict, int]]:
    """Open one SSE connection and yield parsed events until it drops."""
    headers = auth_headers(api_key, agent_id, agent_key)
    headers["X-Listener-Host"] = nome_host()
    if last_event_id:
        headers["Last-Event-ID"] = str(last_event_id)
    # read=90s MUST stay above the hub's keepalive interval: a silently dead
    # TCP connection (NAT drop, hub restart, host sleep) otherwise hangs
    # aiter_lines() forever and the listener never reconnects.
    timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "GET", f"{base_url.rstrip('/')}/api/events/stream", headers=headers,
        ) as resp:
            resp.raise_for_status()
            async for ev in parse_sse(resp.aiter_lines()):
                yield ev


AUTH_REJECT_STATUSES = (401, 403)


def auth_reject_status(exc: BaseException | None) -> int | None:
    """Lo status HTTP se questa eccezione (o una sua causa) e' un rifiuto di
    autenticazione del bus, altrimenti None.

    Si guarda la catena `__cause__`/`__context__` perche' l'errore nasce dentro
    il generatore SSE e puo' arrivare al chiamante riavvolto da un altro strato.

    Vive qui, accanto al trasporto, perche' la domanda "il bus mi ha rifiutato?"
    e' del trasporto, e perche' i due che la pongono — il bot (`hub.py`) e il
    watcher delle sessioni interattive (`hub_watch.py`) — non possono
    condividerla altrimenti: il secondo non importa il primo, e per questo la
    risposta e' rimasta giusta da una parte e sbagliata dall'altra per mesi
    (issue #31).
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in AUTH_REJECT_STATUSES:
            return int(code)
        exc = exc.__cause__ or exc.__context__
    return None
