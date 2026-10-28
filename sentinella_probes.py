# sentinella_probes.py
"""Le sonde. Solo I/O: nessuna decisione, nessuno stato.

Regola non negoziabile: una sonda non solleva MAI. Un'eccezione diventa
`sconosciuto`, che a valle conta come fallimento — perche' un probe rotto e' esso
stesso un segnale, e trattarlo come "ok" e' il modo in cui un monitoraggio mente.

La distinzione fra GIU e SCONOSCIUTO non e' cosmetica: "il servizio risponde 502"
e "non sono riuscito a chiedere" sono due fatti diversi, e il messaggio che
arriva alle tre di notte deve dire quale dei due e'.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

import local_defaults
import sentinella
import sentinella_config

logger = logging.getLogger("coordinator.sentinella_probes")

TIMEOUT_SEC = 20

# Il box sorvegliato e la chiave per entrarci sono dell'installazione
# (locale/defaults.json), non del motore.
SSH_HOST = local_defaults.get("SENTINELLA_SSH_HOST")
SSH_KEY = local_defaults.get("SENTINELLA_SSH_KEY", "/root/.ssh/id_ed25519")
DEADMAN_URL = os.environ.get("SENTINELLA_DEADMAN_URL", "")

# Riferimento sulla tailnet che NON e' un box sorvegliato: serve a rispondere
# alla domanda "la cecita' e' mia?". Se punta al box sorvegliato non distingue
# niente. Vuoto = gate spento (vedi sonda_canale).
URL_CANALE_TAILNET = local_defaults.get("SENTINELLA_URL_CANALE_TAILNET")
# Controprova sulla rete pubblica: separa "la mia tailnet e' giu'" da
# "non ho piu' rete affatto", che sono due allarmi diversi.
URL_CANALE_INTERNET = os.environ.get(
    "SENTINELLA_URL_CANALE_INTERNET", "https://www.google.com/generate_204")

# ssh(1) usa 255 per "non sono riuscito a collegarmi", distinto dal codice di
# uscita del comando remoto: e' la differenza fra "box irraggiungibile" e
# "comando fallito sul box".
USCITA_SSH_IRRAGGIUNGIBILE = 255


@dataclass(frozen=True)
class ProbeResult:
    esito: str
    motivo: str = ""


# --------------------------------------------------------------------------
# Sonde HTTP
# --------------------------------------------------------------------------

async def sonda_http(url: str, *, client: httpx.AsyncClient) -> ProbeResult:
    """GET su `url`. Guarda anche il corpo quando e' un /health con database."""
    try:
        resp = await client.get(url, timeout=TIMEOUT_SEC)
    except Exception as exc:
        return ProbeResult(sentinella.SCONOSCIUTO, f"sonda fallita: {exc}")

    if resp.status_code >= 400:
        return ProbeResult(sentinella.GIU, f"HTTP {resp.status_code}")

    try:
        corpo = resp.json()
    except ValueError:
        return ProbeResult(sentinella.OK)

    if isinstance(corpo, dict):
        db = corpo.get("database")
        if isinstance(db, dict) and db.get("connected") is False:
            dettaglio = db.get("error") or "senza dettaglio"
            return ProbeResult(sentinella.GIU, f"database scollegato: {dettaglio}")
    return ProbeResult(sentinella.OK)


async def ping_deadman(url: str | None = None, *, client: httpx.AsyncClient) -> bool:
    """Ping al servizio esterno. Se questi ping SMETTONO, allarma lui.

    Copre "e' morta dev-linux", "e' morto il Coordinator", "e' morta la
    sentinella": nessuno dei tre puo' auto-denunciarsi, e l'assenza di segnale e'
    l'unico segnale che sopravvive alla propria morte.
    """
    destinazione = DEADMAN_URL if url is None else url
    if not destinazione:
        return False
    try:
        await client.get(destinazione, timeout=TIMEOUT_SEC)
        return True
    except Exception as exc:
        logger.warning("ping dead-man fallito: %s", exc)
        return False


async def sonda_canale(
    *,
    client: httpx.AsyncClient,
    url_tailnet: str | None = None,
    url_internet: str | None = None,
) -> ProbeResult:
    """La sentinella vede ancora i box, o e' cieca lei?

    Nasce dal 7 settembre 2026: tailscaled si e' fermato QUI, ogni sonda verso
    il box pubblico ha iniziato a rispondere "box irraggiungibile" e la prod — che stava
    servendo i clienti senza un secondo di interruzione — e' stata dichiarata
    giu'. Nessuna sonda sbagliava: sbagliava l'assenza di questa domanda.

    L'ordine e' quello di una diagnosi, non di un test di rete:
      tailnet risponde        -> il canale c'e', dei box si puo' parlare
      tailnet muta, web vivo  -> GIU: e' la MIA tailnet, non i box
      tutto muto              -> SCONOSCIUTO: potrebbe essere la linea, e in quel
                                 caso i box possono star benissimo servendo
    """
    tailnet = URL_CANALE_TAILNET if url_tailnet is None else url_tailnet
    if not tailnet:
        # Non configurato: il gate tace. Un controllo che, non configurato,
        # blocca la sorveglianza sarebbe solo un modo nuovo di essere ciechi.
        return ProbeResult(sentinella.OK)

    try:
        await client.get(tailnet, timeout=TIMEOUT_SEC)
        return ProbeResult(sentinella.OK)
    except Exception as exc:
        motivo_tailnet = str(exc) or exc.__class__.__name__

    internet = URL_CANALE_INTERNET if url_internet is None else url_internet
    if not internet:
        return ProbeResult(
            sentinella.SCONOSCIUTO, f"riferimento tailnet muto: {motivo_tailnet[:120]}")

    try:
        await client.get(internet, timeout=TIMEOUT_SEC)
    except Exception:
        return ProbeResult(
            sentinella.SCONOSCIUTO,
            "ne' la tailnet ne' la rete pubblica rispondono: colpa non attribuibile",
        )

    return ProbeResult(
        sentinella.GIU,
        "tailnet locale giu' (la rete pubblica risponde): la sentinella non vede i box, "
        f"nulla si puo' dire del loro stato — {motivo_tailnet[:120]}",
    )


# --------------------------------------------------------------------------
# Sonde via SSH
# --------------------------------------------------------------------------

def via_host() -> bool:
    """La sonda passa dal bridge dell'host invece che da una chiave nel container (#89).

    ``SENTINELLA_VIA_HOST=1|0`` decide; non impostata, si passa dall'host quando
    il bridge c'e' (``HOST_EXEC_SSH``) e la chiave del box non e' montata.
    """
    raw = os.environ.get("SENTINELLA_VIA_HOST", "").strip().lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    return bool(os.environ.get("HOST_EXEC_SSH", "").strip()) and not os.path.isfile(SSH_KEY)


async def _esegui_via_host(comando: str) -> tuple[int, str]:
    """Il box lo raggiunge l'host: il container chiede un comando, non ha la chiave.

    L'host (scripts/host_sentinella.py, dietro il comando forzato della chiave
    host-exec) accetta solo i comandi di ``sentinella_config.comandi_ammessi``
    e sceglie lui box e chiave. Uscita e testo sono quelli del comando remoto.
    """
    import json

    import claude_runner

    proc = await asyncio.create_subprocess_exec(
        *claude_runner.host_exec_command(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    req = (json.dumps({"op": "sentinella-ssh", "cmd": comando}) + "\n").encode()
    try:
        out, _ = await asyncio.wait_for(proc.communicate(req), timeout=TIMEOUT_SEC * 3)
    except asyncio.TimeoutError:
        proc.kill()
        return USCITA_SSH_IRRAGGIUNGIBILE, "bridge host: timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def esegui_ssh(comando: str, *, runner=None) -> tuple[int, str]:
    """Esegue `comando` sul box. `runner` iniettabile per i test."""
    if runner is not None:
        return await runner(comando)
    if via_host():
        return await _esegui_via_host(comando)
    if not SSH_HOST:
        # Detto chiaro, non un "box irraggiungibile": nessuno e' giu', manca
        # solo la configurazione.
        return 255, "SENTINELLA_SSH_HOST non configurato (locale/defaults.json o env)"

    proc = await asyncio.create_subprocess_exec(
        "ssh", "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        # Senza questo, ssh prova comunque a SCRIVERE la chiave host in
        # known_hosts: nel container quella home non e' scrivibile e stampa un
        # avviso che — con stderr fuso in stdout (sotto) — finisce nello stesso
        # flusso del dato. Il 2026-08-17 ha reso illeggibile il timestamp della
        # sonda vivacita' e messo la prod a "giu" con il sito che dava 200.
        "-o", "UserKnownHostsFile=/dev/null",
        # …e con /dev/null ssh annuncia comunque "Permanently added …": il
        # rumore cambia forma, non sparisce. LogLevel=ERROR lo zittisce davvero,
        # lasciando passare i messaggi che contano (auth negata, host irraggiungibile).
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
        SSH_HOST, comando,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def sonda_motore_docker(*, runner=None, prefisso: str = "") -> ProbeResult:
    """Il guasto dell'8 agosto: box acceso, motore Docker fermo.

    Docker Desktop avvia il suo motore solo al logon interattivo. Via SSH quel
    logon non avviene, quindi dopo un riavvio non sorvegliato il motore resta
    giu' e con lui ogni container del box.
    """
    try:
        uscita, testo = await esegui_ssh(
            sentinella_config.comando_motore(prefisso), runner=runner)
    except Exception as exc:
        return ProbeResult(sentinella.SCONOSCIUTO, f"ssh fallito: {exc}")

    if uscita == USCITA_SSH_IRRAGGIUNGIBILE:
        return ProbeResult(sentinella.SCONOSCIUTO, "box irraggiungibile via ssh")
    if uscita != 0:
        return ProbeResult(
            sentinella.GIU, f"motore Docker non risponde: {testo.strip()[:200]}")
    return ProbeResult(sentinella.OK)


async def sonda_container(
    attesi: tuple[str, ...], *, runner=None, prefisso: str = ""
) -> tuple[ProbeResult, list[str]]:
    """Un container caduto senza che il sito sembri giu'.

    Ritorna anche i mancanti, che sono l'input dell'auto-riparazione.
    """
    try:
        uscita, testo = await esegui_ssh(
            sentinella_config.comando_container(prefisso), runner=runner)
    except Exception as exc:
        return ProbeResult(sentinella.SCONOSCIUTO, f"ssh fallito: {exc}"), []

    if uscita == USCITA_SSH_IRRAGGIUNGIBILE:
        return ProbeResult(sentinella.SCONOSCIUTO, "box irraggiungibile via ssh"), []
    if uscita != 0:
        # Comando fallito: non sappiamo quali container ci siano. Dichiararli
        # tutti mancanti farebbe partire riavvii su una realta' immaginata.
        return ProbeResult(
            sentinella.SCONOSCIUTO, f"docker ps fallito: {testo.strip()[:200]}"), []

    presenti = {r.strip() for r in testo.splitlines() if r.strip()}
    mancanti = [c for c in attesi if c not in presenti]
    if mancanti:
        return ProbeResult(
            sentinella.GIU, f"container assenti: {', '.join(mancanti)}"), mancanti
    return ProbeResult(sentinella.OK), []


async def sonda_http_remota(comando: str, *, runner=None) -> ProbeResult:
    """Sonda HTTP eseguita SUL BOX, per i servizi legati a 127.0.0.1.

    Serve perche' un `127.0.0.1:8000` nella config non significa niente finche'
    non si dice *di chi*: sondato da dev-linux colpisce dev-linux. In una prova
    reale del 17 agosto questo ha prodotto un "tutto ok" leggendo la macchina
    sbagliata — un falso via libera, che e' peggio di nessun controllo.

    `comando` deve stampare il codice HTTP (es. `000` se nessuno risponde).
    """
    try:
        uscita, testo = await esegui_ssh(comando, runner=runner)
    except Exception as exc:
        return ProbeResult(sentinella.SCONOSCIUTO, f"sonda remota fallita: {exc}")

    if uscita == USCITA_SSH_IRRAGGIUNGIBILE:
        return ProbeResult(sentinella.SCONOSCIUTO, "box irraggiungibile via ssh")

    righe = [r.strip() for r in testo.splitlines() if r.strip()]
    try:
        codice = int(righe[-1])
    except (ValueError, IndexError):
        return ProbeResult(
            sentinella.SCONOSCIUTO, f"risposta non interpretabile: {testo.strip()[:80]!r}")

    if codice == 0:
        return ProbeResult(sentinella.GIU, "nessuna risposta dal servizio")
    if codice >= 400:
        return ProbeResult(sentinella.GIU, f"HTTP {codice}")
    return ProbeResult(sentinella.OK)


async def sonda_vivacita(
    comando: str,
    max_silenzio_min: int,
    adesso: datetime,
    *,
    runner=None,
) -> ProbeResult:
    """Lo scheduler zombie: processo su, job fermi.

    Non chiede allo scheduler come sta — legge DA FUORI quando ha eseguito
    l'ultimo job. Uno scheduler morto non puo' mentire su un timestamp che non
    aggiorna piu', mentre a un endpoint "sono vivo" risponderebbe di si'.

    `comando` deve stampare un timestamp ISO su stdout. Vuoto = sonda non
    applicabile a questo servizio.
    """
    if not comando:
        return ProbeResult(sentinella.OK)

    try:
        uscita, testo = await esegui_ssh(comando, runner=runner)
    except Exception as exc:
        return ProbeResult(sentinella.SCONOSCIUTO, f"sonda vivacita' fallita: {exc}")

    if uscita != 0:
        return ProbeResult(
            sentinella.SCONOSCIUTO, f"vivacita' non leggibile: {testo.strip()[:200]}")

    # stderr e' fuso in stdout (utile in diagnosi), quindi il dato puo' avere
    # righe di rumore davanti: si legge l'ULTIMA riga non vuota, che e' quella
    # stampata dal comando. Difesa in profondita' oltre a UserKnownHostsFile:
    # un avviso nuovo del client SSH non deve tornare a falsare l'allarme.
    righe = [r.strip() for r in testo.replace("\r", "\n").split("\n") if r.strip()]
    try:
        ultimo = datetime.fromisoformat(righe[-1]) if righe else None
        if ultimo is None:
            raise ValueError("nessun output")
    except ValueError:
        # Non sappiamo se lo scheduler e' fermo: non fingiamo di saperlo.
        return ProbeResult(
            sentinella.SCONOSCIUTO, f"timestamp illeggibile: {testo.strip()[:80]!r}")

    if ultimo.tzinfo is None:
        ultimo = ultimo.replace(tzinfo=timezone.utc)

    silenzio_min = (adesso - ultimo).total_seconds() / 60
    if silenzio_min > max_silenzio_min:
        return ProbeResult(
            sentinella.GIU,
            f"scheduler fermo da {int(silenzio_min)} minuti (atteso ogni {max_silenzio_min})",
        )
    return ProbeResult(sentinella.OK)


async def sonda_disco(soglia_pct: int, *, runner=None) -> ProbeResult:
    """Un guasto annunciato: si vede prima, non dopo.

    Severita' "attenzione": finisce nel riepilogo del mattino, non sveglia
    nessuno alle tre di notte.
    """
    try:
        uscita, testo = await esegui_ssh(sentinella_config.COMANDO_DISCO, runner=runner)
    except Exception as exc:
        return ProbeResult(sentinella.SCONOSCIUTO, f"sonda disco fallita: {exc}")

    if uscita != 0:
        return ProbeResult(sentinella.SCONOSCIUTO, "spazio disco non leggibile")

    try:
        usato = int(testo.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ProbeResult(sentinella.SCONOSCIUTO, "spazio disco non interpretabile")

    if usato >= soglia_pct:
        return ProbeResult(sentinella.GIU, f"disco C: al {usato}%")
    return ProbeResult(sentinella.OK)
