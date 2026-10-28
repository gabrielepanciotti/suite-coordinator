from datetime import datetime, timezone

import sentinella
from sentinella_store import StatoServizio

T0 = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)


def _guasto(chiave, causa, motivo="giu", azione="allarme"):
    return {"chiave": chiave, "titolo": chiave.title(), "esito": "giu",
            "motivo": motivo, "causa": causa,
            "stato": StatoServizio(stato="giu", da_iso=T0.isoformat()), "azione": azione}


def test_guasti_del_box_diventano_un_solo_gruppo():
    """Una dozzina di notifiche per un guasto solo insegna a ignorarle."""
    gruppi = sentinella.raggruppa(
        [_guasto("portale", "box"), _guasto("offerte-pubbliche", "box")], T0)
    assert len(gruppi) == 1
    assert gruppi[0]["causa"] == "box"
    assert len(gruppi[0]["servizi"]) == 2


def test_guasti_di_servizi_diversi_restano_separati():
    gruppi = sentinella.raggruppa(
        [_guasto("portale", "servizio"), _guasto("offerte-pubbliche", "servizio")], T0)
    assert len(gruppi) == 2


def test_formatta_nomina_il_box_e_i_servizi(locale_defaults, monkeypatch):
    # Il nome del box e' un default dell'installazione: qui uno di prova.
    monkeypatch.delenv("SENTINELLA_BOX_NOME", raising=False)
    locale_defaults({"SENTINELLA_BOX_NOME": "edge-box"})
    gruppi = sentinella.raggruppa(
        [_guasto("portale", "box", "motore Docker non risponde"),
         _guasto("offerte-pubbliche", "box", "motore Docker non risponde")], T0)
    testo = sentinella.formatta(gruppi, T0)
    assert "motore Docker non risponde" in testo
    assert "2 servizi" in testo
    assert "edge-box" in testo


def test_formatta_senza_guasti_ritorna_none():
    """Silenzio quando va tutto bene: un canale che parla sempre non viene letto."""
    assert sentinella.formatta([], T0) is None


def test_il_rientro_dice_quanto_e_durato():
    rientro = {"chiave": "portale", "titolo": "Portale", "esito": "ok",
               "motivo": "tornato su dopo 4h 20m", "causa": "rientro",
               "stato": StatoServizio(stato="ok", da_iso=T0.isoformat()),
               "azione": "rientro"}
    testo = sentinella.formatta(sentinella.raggruppa([rientro], T0), T0)
    assert "4h 20m" in testo
    assert "🟢" in testo
