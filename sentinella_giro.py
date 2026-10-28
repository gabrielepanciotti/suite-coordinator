# sentinella_giro.py
"""Il giro della sentinella: esegue le sonde, decide, ripara, riferisce.

Ritorna il TESTO da mandare, oppure None quando non c'e' niente da dire.

Ritornare None mentre tutto funziona e' una scelta, non una dimenticanza: la
liveness della sentinella non e' provata da cio' che manda, ma dal fatto che e'
GIRATA — e a sorvegliarlo e' `notif_health`, esattamente come fa per il provider
`health`. Un allarme che parla ogni cinque minuti per dire "tutto bene" e' un
allarme che nessuno legge, e quando poi dice qualcosa di vero e' gia' invisibile.

Il riepilogo del mattino e' l'eccezione deliberata: quello parla sempre, perche'
la sua assenza deve essere notata.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

import sentinella
import sentinella_config
import sentinella_probes
import sentinella_repair
import sentinella_store

logger = logging.getLogger("coordinator.sentinella_giro")

# Chiave riservata nello stato: la sentinella sorveglia anche il proprio
# occhio, e lo fa con la stessa macchina degli altri (soglia, ripetizione,
# rientro) invece di una scorciatoia che si comporterebbe diversamente.
CHIAVE_CANALE = "__canale__"
TITOLO_CANALE = "Sentinella"


async def _sonde_di(servizio, *, client=None, runner=None, adesso=None):
    """Esegue le sonde di un servizio. Ritorna (ProbeResult, mancanti, causa).

    L'ordine non e' casuale: si va dalla causa piu' larga alla piu' stretta. Se
    il motore Docker e' giu' non ha senso chiedere dei container, e soprattutto
    la causa e' UNA — il box — cosi' i guasti dei singoli servizi si raggruppano
    in un messaggio invece di diventarne otto.
    """
    prefisso = sentinella_config.prefisso_docker(servizio)
    motore = await sentinella_probes.sonda_motore_docker(
        runner=runner, prefisso=prefisso)
    if motore.esito != sentinella.OK:
        return motore, [], "box"

    contenitori, mancanti = await sentinella_probes.sonda_container(
        servizio.container, runner=runner, prefisso=prefisso)
    if contenitori.esito != sentinella.OK:
        return contenitori, mancanti, "servizio"

    # Un servizio legato a 127.0.0.1 SUL BOX va sondato dal box: da qui quel
    # 127.0.0.1 sarebbe dev-linux, e la risposta parlerebbe di un'altra macchina.
    if servizio.comando_salute:
        salute = await sentinella_probes.sonda_http_remota(
            servizio.comando_salute, runner=runner)
    else:
        salute = await sentinella_probes.sonda_http(servizio.url_salute, client=client)
    if salute.esito != sentinella.OK:
        return salute, [], "servizio"

    # Ultima perche' e' la piu' sottile: tutto risponde, ma i job sono fermi.
    vivacita = await sentinella_probes.sonda_vivacita(
        servizio.comando_vivacita, servizio.max_silenzio_min,
        adesso or datetime.now(timezone.utc), runner=runner)
    return vivacita, [], "servizio"


NOTA_RIAVVIO = "riavvio tentato"


def _con_nota_riavvio(motivo: str, recenti: list[str]) -> str:
    """Aggiunge la nota UNA volta sola.

    Il motivo viene risalvato a ogni giro, quindi accodare senza controllare lo
    fa crescere all'infinito: il 26 agosto un messaggio conteneva dodici copie
    della stessa nota, illeggibile proprio quando serviva leggerlo.
    """
    if not recenti or NOTA_RIAVVIO in motivo:
        return motivo
    return f"{motivo} — {NOTA_RIAVVIO}: {', '.join(recenti)}"


SOGLIA_DISCO_PCT = 85


def _riepilogo(servizi, stati, adesso, attenzioni: list[str]) -> str:
    """Il messaggio del mattino. Parla SEMPRE, anche quando va tutto bene.

    E' l'unica notifica che rompe la regola del silenzio, di proposito: se una
    mattina non arriva, la sua assenza e' essa stessa un segnale — e
    `notif_health` la dichiara `silent`.
    """
    vuoto = sentinella_store.StatoServizio()
    giu = [s for s in servizi if stati.get(s.chiave, vuoto).stato == sentinella.GIU]
    container = sum(len(s.container) for s in servizi)

    if giu:
        nomi = ", ".join(s.titolo for s in giu)
        righe = [f"🔴 Prod work instance: {len(giu)} servizi su {len(servizi)} sono giu'",
                 f"   {nomi}"]
    else:
        righe = [
            f"🟢 Prod work instance: {len(servizi)} servizi, {container} container, tutti ok",
            f"   ultimo controllo {adesso.strftime('%H:%M')}",
        ]

    righe.extend(f"🟡 {a}" for a in attenzioni)
    return "\n".join(righe)


async def giro(
    adesso: datetime | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    runner=None,
    modo: str = "allarmi",
) -> str | None:
    """Un giro completo. `modo="riepilogo"` parla anche quando va tutto bene."""
    adesso = adesso or datetime.now(timezone.utc)
    servizi = sentinella_config.carica()
    stati = sentinella_store.carica()

    chiudi = client is None
    if client is None:
        client = httpx.AsyncClient()

    guasti: list[dict] = []
    attenzioni: list[str] = []
    try:
        # PRIMA di tutto: la sentinella vede ancora, o e' cieca lei?
        #
        # Il 7 settembre 2026 tailscaled si e' fermato su dev-linux e ogni sonda
        # ha iniziato a rispondere "box irraggiungibile via ssh". Le sonde non
        # sbagliavano — semplicemente, da qui non si poteva piu' sapere nulla dei
        # box. Senza questa domanda la sentinella ha letto quel nulla come un
        # guasto, ha dichiarato giu' una prod che serviva 200 senza un secondo di
        # interruzione, e ha mandato una persona a spegnere servizi sani.
        #
        # Cieca si allarma lo stesso — la sorveglianza e' ferma davvero, ed e' un
        # fatto che va detto — ma si allarma di SE', e i contatori dei servizi
        # restano dove sono: al ritorno della rete nessuno deve trovarsi oltre
        # soglia per un guasto mai avvenuto.
        canale = await sentinella_probes.sonda_canale(client=client)
        if canale.esito != sentinella.OK:
            prec = stati.get(CHIAVE_CANALE, sentinella_store.StatoServizio())
            nuovo_c, azione = sentinella.valuta(
                CHIAVE_CANALE, canale.esito, canale.motivo, prec, adesso)
            if azione is None and sentinella.ripetizione_dovuta(nuovo_c, adesso):
                nuovo_c.ultimo_allarme_iso = adesso.isoformat()
                azione = "ripetizione"
            stati[CHIAVE_CANALE] = nuovo_c
            sentinella_store.salva(stati)

            if azione in ("allarme", "ripetizione"):
                guasti.append({
                    "chiave": CHIAVE_CANALE, "titolo": TITOLO_CANALE,
                    "esito": canale.esito, "motivo": nuovo_c.motivo,
                    "causa": "canale", "stato": nuovo_c, "azione": azione,
                })
            await sentinella_probes.ping_deadman(client=client)
            if modo == "riepilogo":
                return _riepilogo(servizi, stati, adesso, attenzioni)
            return sentinella.formatta(sentinella.raggruppa(guasti, adesso), adesso)

        # Canale sano: se era cieca, dirlo. Il rientro chiude l'allarme aperto.
        prec_canale = stati.get(CHIAVE_CANALE)
        if prec_canale is not None and prec_canale.stato == sentinella.GIU:
            nuovo_c, azione = sentinella.valuta(
                CHIAVE_CANALE, sentinella.OK, "", prec_canale, adesso)
            stati[CHIAVE_CANALE] = nuovo_c
            if azione == "rientro":
                guasti.append({
                    "chiave": CHIAVE_CANALE, "titolo": TITOLO_CANALE,
                    "esito": sentinella.OK,
                    "motivo": ("torna a vedere i box dopo "
                               f"{sentinella.durata_umana(prec_canale.da_iso, adesso)}"),
                    "causa": "rientro", "stato": nuovo_c, "azione": "rientro",
                })
        elif prec_canale is not None:
            stati[CHIAVE_CANALE] = sentinella_store.StatoServizio(
                stato=sentinella.OK, da_iso=prec_canale.da_iso or adesso.isoformat())

        for servizio in servizi:
            prec = stati.get(servizio.chiave, sentinella_store.StatoServizio())
            try:
                res, mancanti, causa = await _sonde_di(
                    servizio, client=client, runner=runner, adesso=adesso)
            except Exception as exc:  # una sonda non deve MAI fermare il giro
                logger.exception("sonde di %s fallite", servizio.chiave)
                res = sentinella_probes.ProbeResult(
                    sentinella.SCONOSCIUTO, f"giro fallito: {exc}")
                mancanti, causa = [], "servizio"

            nuovo, azione = sentinella.valuta(
                servizio.chiave, res.esito, res.motivo, prec, adesso)

            # Riparazione solo dei container: mai il motore, mai il box.
            if mancanti and causa == "servizio":
                tentati = await sentinella_repair.ripara(
                    servizio, mancanti, nuovo, adesso, runner=runner)
                if tentati:
                    # Si parla SEMPRE quando si ripara, anche se il guasto non ha
                    # raggiunto la soglia d'allarme e anche se la riparazione e'
                    # riuscita. Una riparazione silenziosa nasconde un guasto che
                    # si ripete, e il giorno che il riavvio non basta piu' nessuno
                    # sa da quanto durava.
                    guasti.append({
                        "chiave": servizio.chiave, "titolo": servizio.titolo,
                        "esito": res.esito,
                        "motivo": f"caduto e riavviato: {', '.join(tentati)}",
                        "causa": "riparazione", "stato": nuovo, "azione": "riparazione",
                    })
                # La nota si lega ai tentativi RECENTI, non al singolo giro: il
                # riavvio avviene al primo fallimento, che e' silenzioso, mentre
                # il messaggio parte al secondo. Legandola al giro, la persona
                # leggerebbe "container assente" senza sapere che nel frattempo
                # qualcuno ha gia' provato a rimetterlo su.
                recenti = [
                    c for c in mancanti
                    if not sentinella_repair.riparazione_permessa(nuovo, c, adesso)
                ]
                if recenti and not tentati:
                    nuovo.motivo = _con_nota_riavvio(nuovo.motivo, recenti)

            if azione is None and sentinella.ripetizione_dovuta(nuovo, adesso):
                nuovo.ultimo_allarme_iso = adesso.isoformat()
                azione = "ripetizione"

            stati[servizio.chiave] = nuovo

            if azione in ("allarme", "ripetizione"):
                guasti.append({
                    "chiave": servizio.chiave, "titolo": servizio.titolo,
                    "esito": res.esito, "motivo": nuovo.motivo, "causa": causa,
                    "stato": nuovo, "azione": azione,
                })
            elif azione == "rientro":
                guasti.append({
                    "chiave": servizio.chiave, "titolo": servizio.titolo,
                    "esito": sentinella.OK,
                    "motivo": f"tornato su dopo {sentinella.durata_umana(prec.da_iso, adesso)}",
                    "causa": "rientro", "stato": nuovo, "azione": "rientro",
                })

        sentinella_store.salva(stati)

        # Livello "attenzione": guasti annunciati, che si vedono prima. Non
        # svegliano nessuno — finiscono solo nel riepilogo del mattino.
        if modo == "riepilogo":
            disco = await sentinella_probes.sonda_disco(SOGLIA_DISCO_PCT, runner=runner)
            if disco.esito != sentinella.OK and disco.motivo:
                attenzioni.append(disco.motivo)

        await sentinella_probes.ping_deadman(client=client)
    finally:
        if chiudi:
            await client.aclose()

    if modo == "riepilogo":
        return _riepilogo(servizi, stati, adesso, attenzioni)
    return sentinella.formatta(sentinella.raggruppa(guasti, adesso), adesso)
