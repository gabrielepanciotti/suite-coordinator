# sentinella_repair.py
"""Auto-riparazione: rimette su un container caduto, e nient'altro.

I limiti contano piu' della funzione.

* **Non tocca MAI** il motore Docker, il box, i dati, le migrazioni. Se il motore
  e' morto si allarma e basta: quel guasto si toglie migrando a un Docker Engine
  in WSL che parte come servizio, non con un'automazione fragile messa a guardia
  di un difetto in via di eliminazione.
* **Al massimo un tentativo all'ora per container**, perche' un watchdog che
  riavvia in loop trasforma un guasto in un disastro.
* **Chi chiama DEVE allarmare comunque**, anche quando la riparazione riesce: una
  riparazione silenziosa nasconde un guasto che si ripete, e il giorno che smette
  di funzionare nessuno sa da quanto durava.
"""
from __future__ import annotations

import logging
from datetime import datetime

import sentinella_config
from sentinella_config import Servizio
from sentinella_probes import esegui_ssh
from sentinella_store import StatoServizio

logger = logging.getLogger("coordinator.sentinella_repair")

ATTESA_TRA_RIPARAZIONI_SEC = 3600


def riparazione_permessa(stato: StatoServizio, container: str, adesso: datetime) -> bool:
    ultimo_iso = stato.riparazioni.get(container)
    if not ultimo_iso:
        return True
    try:
        ultimo = datetime.fromisoformat(ultimo_iso)
    except ValueError:
        return True
    return (adesso - ultimo).total_seconds() >= ATTESA_TRA_RIPARAZIONI_SEC


async def ripara(
    servizio: Servizio,
    mancanti: list[str],
    stato: StatoServizio,
    adesso: datetime,
    *,
    runner=None,
) -> list[str]:
    """Rimette su i container assenti. Ritorna quelli per cui ha TENTATO.

    "Tentato" e non "riuscito": il tentativo va registrato comunque, altrimenti
    un riavvio che fallisce verrebbe ritentato a ogni giro da cinque minuti.
    """
    da_tentare = [c for c in mancanti if riparazione_permessa(stato, c, adesso)]
    if not da_tentare:
        logger.info("riparazione di %s saltata: gia' tentata entro l'ora", servizio.chiave)
        return []

    comando = sentinella_config.comando_riparazione(servizio)
    try:
        uscita, testo = await esegui_ssh(comando, runner=runner)
        logger.warning(
            "riparazione %s (%s): uscita=%s %s",
            servizio.chiave, ", ".join(da_tentare), uscita, testo.strip()[:200])
    except Exception as exc:
        logger.warning("riparazione %s fallita: %s", servizio.chiave, exc)

    for container in da_tentare:
        stato.riparazioni[container] = adesso.isoformat()
    return da_tentare
