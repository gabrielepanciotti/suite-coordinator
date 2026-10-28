# sentinella_store.py
"""Stato per servizio, persistito su JSON con scrittura atomica.

Lo stato DEVE sopravvivere al riavvio del Coordinator. Se non sopravvivesse, un
riavvio azzererebbe il contatore dei fallimenti e un guasto lungo non
raggiungerebbe mai la soglia dell'allarme: il monitoraggio sembrerebbe attivo e
sarebbe cieco, che e' il modo peggiore di essere rotti.

Stesso taglio di `notifications`/`notif_store`: qui c'e' solo l'I/O, la
decisione sta in `sentinella.py`.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

from atomic_io import replace_with_retry

logger = logging.getLogger("coordinator.sentinella_store")

STATO_PATH = os.environ.get("SENTINELLA_STATO_PATH", "/app/data/sentinella_stato.json")


@dataclass
class StatoServizio:
    stato: str = "sconosciuto"          # ok | giu | sconosciuto
    da_iso: str = ""                    # da quando dura lo stato corrente
    fallimenti: int = 0                 # fallimenti consecutivi non ancora allarmati
    ultimo_allarme_iso: str = ""        # ultimo messaggio mandato per questo guasto
    motivo: str = ""
    riparazioni: dict = field(default_factory=dict)  # container -> ultimo tentativo iso


def carica() -> dict[str, StatoServizio]:
    """Legge lo stato. File assente, illeggibile o corrotto -> dizionario vuoto."""
    try:
        with open(STATO_PATH, encoding="utf-8") as fh:
            grezzi = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("stato sentinella illeggibile: %s", exc)
        return {}

    stati: dict[str, StatoServizio] = {}
    for chiave, d in (grezzi or {}).items():
        try:
            stati[chiave] = StatoServizio(**d)
        except TypeError as exc:
            logger.warning("stato di %r scartato: %s", chiave, exc)
    return stati


def salva(stati: dict[str, StatoServizio]) -> None:
    d = os.path.dirname(STATO_PATH)
    if d and not os.path.isdir(d):
        return  # nessuna cartella dati scrivibile -> resta in memoria (test / installazione nuova)
    try:
        tmp = STATO_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({k: asdict(v) for k, v in stati.items()}, fh, ensure_ascii=False)
        replace_with_retry(tmp, STATO_PATH)
    except Exception:
        logger.exception("stato sentinella non persistito su %s", STATO_PATH)
