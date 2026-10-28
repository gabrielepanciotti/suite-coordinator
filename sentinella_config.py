# sentinella_config.py
"""Lista dei servizi sorvegliati. Logica pura: nessuna rete, nessun SSH.

Un servizio dichiarato ma inesistente produce un allarme permanente, cioe' un
allarme che si impara a ignorare — ed e' esattamente il difetto che la sentinella
esiste per eliminare. Per questo `valida` e' severa e `carica` SCARTA le voci non
valide (registrandole) invece di tenerle a meta': meglio sorvegliare due servizi
per davvero che tre di cui uno mente.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("coordinator.sentinella_config")

SERVIZI_PATH = os.environ.get(
    "SENTINELLA_SERVIZI_PATH", "/app/data/sentinella_servizi.json"
)


@dataclass(frozen=True)
class Servizio:
    chiave: str
    titolo: str
    dominio: str
    url_salute: str
    container: tuple[str, ...] = field(default_factory=tuple)
    compose_file: str = "docker-compose.prod.yml"
    project_dir: str = ""
    critico: bool = True
    # Comando remoto che stampa un timestamp ISO: l'ultima volta che lo
    # scheduler del servizio ha fatto qualcosa. Vuoto = sonda non applicabile.
    comando_vivacita: str = ""
    max_silenzio_min: int = 45
    # Comando remoto che stampa il codice HTTP, per i servizi in ascolto solo su
    # 127.0.0.1 del box: da qui quell'indirizzo sarebbe dev-linux. Se valorizzato
    # ha la precedenza su `url_salute`.
    comando_salute: str = ""
    # Su quale motore Docker vive il servizio: "desktop" (Docker Desktop, il
    # motore storico del box) oppure "wsl" (docker-ce dentro la distro). Serve
    # perche' `docker ps` sul box NON vede i container dell'altro motore: un
    # servizio migrato risulterebbe sparito, con allarme e riparazione contro il
    # motore sbagliato. Successo davvero il 26 agosto su `offerte-pubbliche`.
    motore: str = "desktop"


MOTORI = ("desktop", "wsl")
PREFISSO_WSL = "wsl -d Ubuntu-24.04 -u root --exec "


def prefisso_docker(s: Servizio) -> str:
    """Cio' che va anteposto a un comando docker per colpire il motore giusto."""
    return PREFISSO_WSL if s.motore == "wsl" else ""


# I comandi che la sentinella manda al box, scritti in un posto solo: le sonde
# li usano, e il bridge sull'host (scripts/host_sentinella.py) accetta SOLO
# questi (#89). Cambiare una stringa qui la cambia da entrambe le parti.
COMANDO_DISCO = (
    "powershell -NoProfile -Command "
    "\"$d=Get-PSDrive C; [int](100*$d.Used/($d.Used+$d.Free))\""
)


def comando_motore(prefisso: str = "") -> str:
    return f"{prefisso}docker info --format ok"


def comando_container(prefisso: str = "") -> str:
    return prefisso + 'docker ps --format "{{.Names}}"'


def comando_riparazione(s: Servizio) -> str:
    # `up -d` SENZA nominare i servizi, di proposito: i nomi dei servizi compose
    # (`backend`, `frontend`) non coincidono con quelli dei container
    # (`portale_segnalazioni_backend_prod`), e passare il nome sbagliato fa
    # fallire il riavvio con "no such service" — successo davvero nella prova del
    # 17 agosto. Senza argomenti, compose avvia cio' che manca e lascia in pace
    # cio' che gira: e' anche il comportamento piu' prudente.
    interno = f'cd "{s.project_dir}" && docker compose -f {s.compose_file} up -d'
    # Il `cd` richiede una shell: sul box la fornisce gia' il comando remoto di
    # ssh, dentro la distro serve chiederla esplicitamente a wsl.
    if s.motore == "wsl":
        return f"{PREFISSO_WSL}bash -c '{interno}'"
    return interno


def comandi_ammessi(servizi: list[Servizio]) -> frozenset[str]:
    """Ogni comando che la sentinella puo' chiedere per questi servizi, e nessun altro."""
    ammessi = {COMANDO_DISCO, comando_motore(), comando_container()}
    for s in servizi:
        prefisso = prefisso_docker(s)
        ammessi |= {comando_motore(prefisso), comando_container(prefisso),
                    comando_riparazione(s)}
        ammessi |= {c for c in (s.comando_salute, s.comando_vivacita) if c}
    return frozenset(ammessi)


def valida(s: Servizio) -> list[str]:
    """Ritorna la lista degli errori. Lista vuota = servizio utilizzabile."""
    errori: list[str] = []
    if not s.chiave:
        errori.append("chiave mancante")
    if not s.dominio:
        errori.append("dominio mancante")
    if not s.url_salute and not s.comando_salute:
        errori.append("serve url_salute oppure comando_salute")
    if not s.container:
        errori.append("nessun container dichiarato")
    if s.motore not in MOTORI:
        errori.append(f"motore sconosciuto: {s.motore!r} (attesi {MOTORI})")
    return errori


def _da_dict(d: dict) -> Servizio:
    chiave = str(d.get("chiave", ""))
    return Servizio(
        chiave=chiave,
        titolo=str(d.get("titolo", chiave)),
        dominio=str(d.get("dominio", "")),
        url_salute=str(d.get("url_salute", "")),
        container=tuple(d.get("container", ())),
        compose_file=str(d.get("compose_file", "docker-compose.prod.yml")),
        project_dir=str(d.get("project_dir", "")),
        critico=bool(d.get("critico", True)),
        comando_vivacita=str(d.get("comando_vivacita", "")),
        max_silenzio_min=int(d.get("max_silenzio_min", 45)),
        comando_salute=str(d.get("comando_salute", "")),
        motore=str(d.get("motore", "desktop")),
    )


def carica(path: str | None = None) -> list[Servizio]:
    """Legge la lista dei servizi. File assente o illeggibile -> lista vuota."""
    percorso = path or SERVIZI_PATH
    try:
        with open(percorso, encoding="utf-8") as fh:
            grezzi = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning("lista servizi illeggibile (%s): %s", percorso, exc)
        return []

    validi: list[Servizio] = []
    for d in grezzi:
        s = _da_dict(d)
        errori = valida(s)
        if errori:
            logger.warning("servizio %r scartato: %s", s.chiave, "; ".join(errori))
            continue
        validi.append(s)
    return validi
