#!/usr/bin/env python3
"""Background SSE watcher for a live-session agent.

Keeps the agent claim warm (heartbeat < HUB_CLAIM_TTL) and exits on the first
inbound wake event, printing it as JSON so the harness re-enters the live session
with it. Reuses hub_client's proven SSE parser.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import time

import httpx

import local_defaults

_REPO = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HUB_CLAIMS_DIR", os.path.join(_REPO, "data", "claims"))


def _load_repo_env() -> None:
    """Legge il `.env` del repo, senza sovrascrivere l'ambiente gia' presente.

    Questo watcher e' fatto per essere lanciato a mano da una sessione Claude
    (`cd <Coordinator> && .venv/bin/python hub_watch.py <agente>`), che e'
    esattamente il comando stampato da `hub-watch-hint.sh` -- e quel comando
    moriva con `KeyError: 'HUB_URL'`, perche' qui si leggeva `os.environ` e
    nessuno aveva mai esportato il `.env`. Chi lancia il programma non deve
    conoscerne il rito: il difetto sta nel programma, non nell'invocazione
    (ETL_V2#88, 10/09/2026).
    """
    path = os.path.join(_REPO, ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            righe = fh.read().splitlines()
    except OSError:
        return
    for riga in righe:
        riga = riga.strip()
        if not riga or riga.startswith("#") or "=" not in riga:
            continue
        chiave, valore = riga.split("=", 1)
        os.environ.setdefault(chiave.strip(), _valore_env(valore))


def _valore_env(grezzo: str) -> str:
    """Il valore di una riga `.env`, senza virgolette ne' commento in coda.

    `CLAUDE_MAX_TIMEOUT_SEC=3600     # tetto ...` arrivava intero in
    `os.environ`, e il primo `int()` che lo leggeva cadeva. Un `#` dentro un
    valore tra virgolette resta valore; fuori, preceduto da spazio, e' commento.
    """
    v = grezzo.strip()
    if v[:1] in ("'", '"'):
        chiusa = v.find(v[0], 1)
        if chiusa > 0:
            return v[1:chiusa]
    for sep in (" #", "\t#"):
        i = v.find(sep)
        if i >= 0:
            v = v[:i]
    return v.strip().strip("'\"")


# Solo quando il watcher parte da riga di comando: importato (dai test, o da
# chi ne riusa le funzioni) non deve riversare il `.env` del deploy
# nell'ambiente del processo -- la suite si ritrovava AI_BACKEND e i timeout
# di produzione, e falliva solo sui checkout che un `.env` ce l'hanno.
if __name__ == "__main__":
    _load_repo_env()

import hub_client  # noqa: E402
import hub_config  # noqa: E402
import hub_store   # noqa: E402

WAKE_EVENTS = ("new_message", "conversation_created")
REFRESH_S = 30  # < HUB_CLAIM_TTL (90): keep the claim fresh while idle
# Capped backoff between SSE reconnects. A long-lived connection that drops
# reconnects fast (index 0); a flapping endpoint backs off so we don't hammer it.
RECONNECT_BACKOFF_S = (1, 2, 5, 10, 30)
# A connection that lived at least this long was healthy: reset the backoff.
HEALTHY_CONN_S = 30


def _dichiara_la_morte(tenant: str, agent: str, segnale: int) -> None:
    """Dire perche' si muore, e ritirare il claim invece di lasciarlo scadere.

    Chi ha lanciato il watcher vede un file di output con dentro `[killed]` e
    nient'altro: non sa se ha consegnato un evento, se e' caduta la rete o se
    l'ha ucciso il reaper dei task in background per "low memory" — che e' il
    caso di gran lunga piu' frequente su questa macchina (quattro watcher in
    mezz'ora, issue #36). Una riga sola toglie la parte peggiore.

    E il claim si ritira subito. Lasciarlo scadere da solo significa fino a 45
    secondi in cui l'agente risulta presidiato da un processo che non esiste,
    e in cui l'anti-gemello zittisce il worker headless — cioe' l'unico
    rimasto capace di rispondere.

    Vale per SIGTERM/SIGINT. Un SIGKILL non lascia eseguire niente, per
    definizione: li' la bugia la smonta chi legge, guardando se il pid del
    claim e' ancora vivo (``hub_store.is_watched``).
    """
    nome = signal.Signals(segnale).name if segnale else "?"
    print(
        f"hub_watch: ucciso da {nome} — '{agent}' NON ascolta piu' il bus. "
        f"Se la sessione e' ancora viva, rilancia in background: "
        f'cd "{_REPO}" && .venv/bin/python hub_watch.py "{agent}"',
        file=sys.stderr,
        flush=True,
    )
    hub_store.release(tenant, agent, only_pid=os.getpid())
    raise SystemExit(128 + segnale)


def _installa_handler_morte(tenant: str, agent: str) -> None:
    for segnale in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(segnale, lambda s, _f: _dichiara_la_morte(tenant, agent, s))
        except (ValueError, OSError):
            pass  # non nel thread principale: nessun handler, nessun danno


async def _heartbeat(tenant: str, agent: str) -> None:
    while True:
        hub_store.claim(tenant, agent, host=os.environ.get("HUB_CLAIM_HOST") or socket.gethostname(), source="watch")
        await asyncio.sleep(REFRESH_S)


async def _first_wake(src) -> dict | None:
    """Consume one stream; return the first wake event, or None if it ends."""
    async for event_type, data, event_id in src:
        if event_type in WAKE_EVENTS:
            return {"event": event_type, "data": data, "id": event_id}
    return None


def _consegna(wake: dict, agent: str) -> None:
    """Stampa l'evento e ricorda che da questo istante il nodo non ascolta piu'.

    L'uscita di questo programma E' la consegna: il harness rientra nella
    sessione viva proprio perche' il comando in background termina. Il rovescio
    e' che dopo la consegna nessuno e' in ascolto, e non lo dice nessuno --
    l'hint del SessionStart ha gia' parlato all'apertura e non parlera' piu'
    (non puo': `UserPromptSubmit` pretende step muti sotto i 10 ms, e questo ne
    costa 700). Cosi' un nodo restava sordo dopo la PRIMA conversazione, e di
    nuovo sembrava in ascolto: il claim-hook continua a scrivere l'heartbeat.
    E' la stessa forma del guasto di ETL_V2#88 -- un canale finto e' peggio di
    un canale assente -- e la stessa cura di `_load_repo_env`: chi lancia il
    programma non deve conoscerne il rito, lo dice il programma.

    Il promemoria va su stderr: stdout e' il payload dell'evento.
    """
    print(json.dumps(wake), flush=True)
    print(
        f"hub_watch: evento consegnato, watcher USCITO — da adesso '{agent}' non "
        f"ascolta il bus. Dopo aver gestito questa conversazione rilancialo in "
        f"background (Bash, run_in_background: true): "
        f'cd "{_REPO}" && .venv/bin/python hub_watch.py "{agent}"',
        file=sys.stderr,
        flush=True,
    )


class RifiutatoDalBus(RuntimeError):
    """Il bus rifiuta questa identita': nessun retry puo' risolverlo.

    Distinta da una caduta di stream proprio perche' il rimedio e' di un'altra
    natura — si allinea una chiave o si toglie l'agente dal roster — e va
    detta a chi puo' farlo, una volta, invece di essere ripetuta mille volte
    l'ora a nessuno.
    """

    def __init__(self, status: int, agent: str) -> None:
        super().__init__(
            f"chiave rifiutata dal bus per '{agent}' ({status}): non ritento. "
            f"La credenziale non e' valida o l'identita' non e' piu' nel roster "
            f"del Network. Allinea HUB_AGENT_KEYS (o togli l'agente) e rilancia."
        )
        self.status = status
        self.agent = agent


async def watch(
    tenant, agent, url, key, *, agent_key="", stream=None, connect=None
) -> None:
    """Refresh the claim until the first wake event, print it as JSON, stop.

    The live SSE connection is long-lived but drops periodically (keepalive/idle
    timeout, hub restart, NAT) — `hub_client.stream_events` yields "until it
    drops". So this reconnects across drops instead of crashing, keeping the
    claim warm via the heartbeat the whole time, until a wake event arrives.

    Injection points for tests:
    - `stream`: a single async-iterator of (event_type, data, id) consumed once
      (no reconnect) — back-compat single-pass path.
    - `connect`: a factory returning a fresh stream per attempt; the live default
      is `lambda: hub_client.stream_events(url, key, agent)`.
    """
    if connect is None:
        # last_event_id is intentionally omitted: a fresh watcher exits on the
        # first wake event, it does not resume a cursor.
        def connect():
            return hub_client.stream_events(url, key, agent, agent_key=agent_key)

    # Initial claim before the loop so it is set even when the stream yields
    # its first event immediately (heartbeat task may not have run yet).
    _installa_handler_morte(tenant, agent)
    hub_store.claim(tenant, agent, host=os.environ.get("HUB_CLAIM_HOST") or socket.gethostname(), source="watch")
    hb = asyncio.create_task(_heartbeat(tenant, agent))
    try:
        if stream is not None:
            wake = await _first_wake(stream)  # single-pass injected iterator
            if wake is not None:
                _consegna(wake, agent)
            return
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                wake = await _first_wake(connect())
            except (httpx.HTTPError, OSError) as exc:
                rifiuto = hub_client.auth_reject_status(exc)
                if rifiuto is not None:
                    # Un 401/403 non e' una rete che balla: e' il bus che dice
                    # "questa credenziale non vale". Ritentarla identica un
                    # secondo dopo non puo' riuscire, e continuera' a non
                    # riuscire finche' qualcuno non cambia la chiave — cioe'
                    # potenzialmente per sempre. Il bot aveva gia' imparato a
                    # fermarsi; questo watcher no, e riempiva il log con un
                    # warning al secondo finche' un guasto vero non ci si
                    # perdeva dentro (issue #31).
                    raise RifiutatoDalBus(rifiuto, agent) from exc
                wake = None
                print(f"hub_watch: SSE drop ({type(exc).__name__}: {exc}); reconnecting",
                      file=sys.stderr, flush=True)
            if wake is not None:
                _consegna(wake, agent)
                return
            if time.monotonic() - started >= HEALTHY_CONN_S:
                attempt = 0  # healthy long connection that just ended → reconnect fast
            await asyncio.sleep(RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)])
            attempt += 1
    finally:
        hb.cancel()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    agent = args[0] if args else os.environ.get("HUB_INTERACTIVE_AGENT", "")
    tenant = local_defaults.get("HUB_CLAIM_TENANT", "default")
    url = os.environ["HUB_URL"]
    agent_key = hub_config.load_agent_keys().get(agent, "") or os.environ.get(
        "HUB_AGENT_KEY", ""
    )
    # The tenant key is optional once this agent has its own key: after the
    # revocation HUB_API_KEY_<TENANT> is simply gone from the env.
    key = os.environ.get(hub_config.api_key_env(tenant), "")
    if not (key or agent_key):
        raise KeyError(f"{hub_config.api_key_env(tenant)} or HUB_AGENT_KEYS[{agent}]")
    try:
        asyncio.run(watch(tenant, agent, url, key, agent_key=agent_key))
    except RifiutatoDalBus as rifiuto:
        # Uscita, ma di tutt'altro genere da quella che consegna un evento: li'
        # su stdout c'e' il wake in JSON e il codice e' 0, qui stdout resta
        # vuoto e il codice e' 2. Chi ha lanciato il watcher deve poter
        # distinguere "ho consegnato, rilanciami" da "non ritentare".
        print(f"hub_watch: {rifiuto}", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
