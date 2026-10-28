from datetime import datetime, timedelta, timezone

import sentinella
from sentinella_store import StatoServizio

T0 = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)


def test_un_solo_fallimento_non_allarma():
    """Il 404 isolato del 13 agosto, durante il rebuild, non deve svegliare nessuno."""
    stato, azione = sentinella.valuta(
        "portale", sentinella.GIU, "404", StatoServizio(stato="ok"), T0)
    assert azione is None
    assert stato.fallimenti == 1


def test_due_fallimenti_consecutivi_allarmano():
    prec = StatoServizio(stato="ok", fallimenti=1)
    stato, azione = sentinella.valuta("portale", sentinella.GIU, "502", prec, T0)
    assert azione == "allarme"
    assert stato.stato == "giu"
    assert stato.ultimo_allarme_iso == T0.isoformat()
    assert stato.motivo == "502"


def test_un_ok_azzera_il_contatore():
    prec = StatoServizio(stato="ok", fallimenti=1)
    stato, azione = sentinella.valuta("portale", sentinella.OK, "", prec, T0)
    assert azione is None
    assert stato.fallimenti == 0


def test_sconosciuto_conta_come_fallimento():
    """Una sonda rotta e' essa stessa un segnale: non e' salute."""
    prec = StatoServizio(stato="ok", fallimenti=1)
    stato, azione = sentinella.valuta(
        "portale", sentinella.SCONOSCIUTO, "ssh timeout", prec, T0)
    assert azione == "allarme"
    assert stato.stato == "giu"


def test_rientro_quando_torna_ok():
    prec = StatoServizio(stato="giu", da_iso=T0.isoformat(), fallimenti=2)
    stato, azione = sentinella.valuta(
        "portale", sentinella.OK, "", prec, T0 + timedelta(hours=4))
    assert azione == "rientro"
    assert stato.stato == "ok"
    assert stato.fallimenti == 0


def test_gia_giu_non_riallarma_a_ogni_giro():
    """L'allarme si ripete a cadenza, non ogni cinque minuti."""
    prec = StatoServizio(stato="giu", da_iso=T0.isoformat(),
                         ultimo_allarme_iso=T0.isoformat(), fallimenti=2)
    _, azione = sentinella.valuta(
        "portale", sentinella.GIU, "502", prec, T0 + timedelta(minutes=5))
    assert azione is None


def test_le_riparazioni_non_si_perdono_nelle_transizioni():
    prec = StatoServizio(stato="ok", fallimenti=1, riparazioni={"c1": T0.isoformat()})
    stato, _ = sentinella.valuta("portale", sentinella.GIU, "502", prec, T0)
    assert stato.riparazioni == {"c1": T0.isoformat()}
