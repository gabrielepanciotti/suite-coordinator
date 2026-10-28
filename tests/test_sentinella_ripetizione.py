from datetime import datetime, timedelta, timezone

import sentinella
from sentinella_store import StatoServizio

T0 = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)


def _giu(da: datetime, ultimo: datetime) -> StatoServizio:
    return StatoServizio(stato="giu", da_iso=da.isoformat(),
                         ultimo_allarme_iso=ultimo.isoformat(), fallimenti=2)


def test_non_ripete_prima_di_un_ora():
    assert sentinella.ripetizione_dovuta(_giu(T0, T0), T0 + timedelta(minutes=59)) is False


def test_ripete_dopo_un_ora():
    assert sentinella.ripetizione_dovuta(_giu(T0, T0), T0 + timedelta(minutes=61)) is True


def test_dopo_sei_ore_rallenta_a_tre_ore():
    """Insiste senza logorare: un allarme che martella viene silenziato."""
    giu_da = T0 - timedelta(hours=7)
    stato = _giu(giu_da, T0)
    assert sentinella.ripetizione_dovuta(stato, T0 + timedelta(hours=1, minutes=1)) is False
    assert sentinella.ripetizione_dovuta(stato, T0 + timedelta(hours=3, minutes=1)) is True


def test_un_servizio_sano_non_ripete_niente():
    assert sentinella.ripetizione_dovuta(StatoServizio(stato="ok"), T0) is False


def test_durata_umana():
    assert sentinella.durata_umana(T0.isoformat(), T0 + timedelta(hours=4, minutes=20)) == "4h 20m"
    assert sentinella.durata_umana(T0.isoformat(), T0 + timedelta(minutes=12)) == "12m"


def test_cinque_giorni_di_buio_producono_decine_di_messaggi():
    """Il difetto piu' costoso di agosto: una sola email in cinque giorni."""
    stato = _giu(T0, T0)
    adesso = T0
    messaggi = 0
    for _ in range(5 * 24 * 12):          # cinque giorni, un giro ogni 5 minuti
        adesso += timedelta(minutes=5)
        if sentinella.ripetizione_dovuta(stato, adesso):
            messaggi += 1
            stato.ultimo_allarme_iso = adesso.isoformat()
    assert messaggi > 25
