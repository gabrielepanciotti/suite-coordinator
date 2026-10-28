import sentinella_store


def test_salva_e_ricarica(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinella_store, "STATO_PATH", str(tmp_path / "stato.json"))
    stati = {"portale": sentinella_store.StatoServizio(
        stato="giu", da_iso="2026-08-17T10:00:00+00:00", fallimenti=2,
        ultimo_allarme_iso="2026-08-17T10:10:00+00:00", motivo="motore Docker fermo",
    )}

    sentinella_store.salva(stati)
    riletti = sentinella_store.carica()

    assert riletti["portale"].stato == "giu"
    assert riletti["portale"].fallimenti == 2
    assert riletti["portale"].motivo == "motore Docker fermo"


def test_lo_stato_sopravvive_al_riavvio(tmp_path, monkeypatch):
    """Se il contatore si azzerasse a ogni riavvio, un guasto lungo non
    raggiungerebbe mai la soglia dell'allarme."""
    monkeypatch.setattr(sentinella_store, "STATO_PATH", str(tmp_path / "stato.json"))
    sentinella_store.salva({"x": sentinella_store.StatoServizio(fallimenti=1)})

    assert sentinella_store.carica()["x"].fallimenti == 1


def test_riparazioni_sono_persistite(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinella_store, "STATO_PATH", str(tmp_path / "stato.json"))
    stato = sentinella_store.StatoServizio(riparazioni={"c1": "2026-08-17T10:00:00+00:00"})

    sentinella_store.salva({"x": stato})

    assert sentinella_store.carica()["x"].riparazioni == {"c1": "2026-08-17T10:00:00+00:00"}


def test_carica_su_file_assente_ritorna_vuoto(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinella_store, "STATO_PATH", str(tmp_path / "manca.json"))
    assert sentinella_store.carica() == {}


def test_file_corrotto_non_esplode(tmp_path, monkeypatch):
    p = tmp_path / "stato.json"
    p.write_text("{ questo non e' json", encoding="utf-8")
    monkeypatch.setattr(sentinella_store, "STATO_PATH", str(p))

    assert sentinella_store.carica() == {}
