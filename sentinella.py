# sentinella.py
"""Decisione della sentinella: quando allarmare, quando insistere, quando tacere.

Logica pura: nessuna rete, nessun orologio interno. L'istante arriva come
argomento, cosi' ogni transizione — comprese quelle che nella realta' durano
giorni — e' testabile senza aspettare.

Il principio, che viene dall'outage dell'8-13 agosto 2026: non si allarma perche'
qualcosa DICE di stare male, si allarma perche' qualcosa ha SMESSO di dire che sta
bene. E mai al primo fallimento, che e' rumore: un allarme che suona sul rumore
viene silenziato dalla persona, e a quel punto il problema originale e' stato
ricostruito a mano.
"""
from __future__ import annotations

from datetime import datetime

import local_defaults
from sentinella_store import StatoServizio

OK = "ok"
GIU = "giu"
SCONOSCIUTO = "sconosciuto"

# Due fallimenti, non uno: rilevamento in ~10 minuti invece che in cinque giorni,
# senza gridare al lupo per un singolo giro storto durante un rebuild.
FALLIMENTI_PRIMA_ALLARME = 2

RIPETI_OGNI_SEC = 3600
DOPO_ORE_RALLENTA = 6
RIPETI_RALLENTATO_SEC = 10800


def valuta(
    chiave: str,
    esito: str,
    motivo: str,
    stato: StatoServizio,
    adesso: datetime,
) -> tuple[StatoServizio, str | None]:
    """Ritorna (nuovo stato, azione), con azione in None|allarme|rientro.

    La ripetizione NON esce da qui: la decide `ripetizione_dovuta`, perche'
    dipende dall'orologio e non dal singolo esito.
    """
    del chiave  # per ora non serve alla decisione: la firma la tiene per i log
    adesso_iso = adesso.isoformat()

    if esito == OK:
        if stato.stato == GIU:
            return (
                StatoServizio(stato=OK, da_iso=adesso_iso, fallimenti=0,
                              riparazioni=dict(stato.riparazioni)),
                "rientro",
            )
        return (
            StatoServizio(stato=OK, da_iso=stato.da_iso or adesso_iso, fallimenti=0,
                          riparazioni=dict(stato.riparazioni)),
            None,
        )

    # Esito GIU o SCONOSCIUTO. Un probe rotto non e' salute: trattarlo come "ok"
    # e' il modo in cui un monitoraggio mente.
    if stato.stato == GIU:
        return (stato, None)

    fallimenti = stato.fallimenti + 1
    if fallimenti < FALLIMENTI_PRIMA_ALLARME:
        return (
            StatoServizio(stato=stato.stato, da_iso=stato.da_iso, fallimenti=fallimenti,
                          ultimo_allarme_iso=stato.ultimo_allarme_iso, motivo=motivo,
                          riparazioni=dict(stato.riparazioni)),
            None,
        )

    return (
        StatoServizio(stato=GIU, da_iso=adesso_iso, fallimenti=fallimenti,
                      ultimo_allarme_iso=adesso_iso, motivo=motivo,
                      riparazioni=dict(stato.riparazioni)),
        "allarme",
    )


def ripetizione_dovuta(stato: StatoServizio, adesso: datetime) -> bool:
    """True se e' ora di ribadire che il guasto e' ancora aperto.

    Il difetto piu' costoso dell'outage di agosto: un allarme solo, al cambio di
    stato. Cinque giorni di buio producevano una mail, facilissima da perdere.
    Qui il promemoria invecchia — ogni ora, poi ogni tre dopo sei ore: insiste
    senza logorare.
    """
    if stato.stato != GIU or not stato.ultimo_allarme_iso:
        return False

    ultimo = datetime.fromisoformat(stato.ultimo_allarme_iso)
    da = datetime.fromisoformat(stato.da_iso) if stato.da_iso else ultimo
    aperto_da_ore = (adesso - da).total_seconds() / 3600
    soglia = RIPETI_RALLENTATO_SEC if aperto_da_ore >= DOPO_ORE_RALLENTA else RIPETI_OGNI_SEC
    return (adesso - ultimo).total_seconds() >= soglia


def durata_umana(da_iso: str, adesso: datetime) -> str:
    if not da_iso:
        return "?"
    secondi = int((adesso - datetime.fromisoformat(da_iso)).total_seconds())
    ore, minuti = divmod(max(secondi, 0) // 60, 60)
    return f"{ore}h {minuti}m" if ore else f"{minuti}m"


def raggruppa(guasti: list[dict], adesso: datetime) -> list[dict]:
    """Un guasto comune produce UN messaggio, non uno per sonda.

    Quando cade il box ogni servizio fallisce insieme agli altri: mandare una
    notifica per ciascuno e' il modo piu' rapido per insegnare a ignorarle.
    """
    del adesso  # firma simmetrica a `formatta`
    del_box = [g for g in guasti if g.get("causa") == "box"]
    gruppi: list[dict] = []
    if del_box:
        gruppi.append({"causa": "box", "motivo": del_box[0]["motivo"], "servizi": del_box})
    for g in guasti:
        if g.get("causa") != "box":
            gruppi.append({"causa": g.get("causa", "servizio"),
                           "motivo": g["motivo"], "servizi": [g]})
    return gruppi


def nome_box() -> str:
    """Come si chiama, nei messaggi, il box sorvegliato (default dell'installazione)."""
    nome = local_defaults.get("SENTINELLA_BOX_NOME")
    if nome:
        return nome
    host = local_defaults.get("SENTINELLA_SSH_HOST")
    return host.rsplit("@", 1)[-1] if host else "box"


def formatta(gruppi: list[dict], adesso: datetime) -> str | None:
    """Il testo da mandare, o None se non c'e' niente da dire."""
    if not gruppi:
        return None

    righe: list[str] = []
    for gr in gruppi:
        servizi = gr["servizi"]
        primo = servizi[0]
        durata = durata_umana(primo["stato"].da_iso, adesso)

        if gr["causa"] == "box":
            nomi = ", ".join(s["titolo"] for s in servizi)
            righe.append(
                f"🔴 {nome_box()}: {gr['motivo']} — {len(servizi)} servizi giu' da {durata}"
                f"\n   {nomi}"
            )
        elif gr["causa"] == "canale":
            # Un allarme che dice "non so", non "e' rotto". La differenza non e'
            # di tono: cambia chi si sveglia e dove va a guardare.
            righe.append(
                f"\u26ab Sentinella cieca da {durata}: non vedo i box — "
                f"{gr['motivo']}\n   stato dei box sconosciuto, nessuna sonda eseguita"
            )
        elif gr["causa"] == "rientro":
            righe.append(f"🟢 {primo['titolo']}: {primo['motivo']}")
        elif gr["causa"] == "riparazione":
            righe.append(f"🔧 {primo['titolo']}: {primo['motivo']}")
        else:
            righe.append(f"🔴 {primo['titolo']} ({gr['motivo']}) — giu' da {durata}")

    return "\n".join(righe)
