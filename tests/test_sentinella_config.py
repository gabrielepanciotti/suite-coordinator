import json

import sentinella_config


def test_carica_legge_i_servizi_dal_file(tmp_path):
    p = tmp_path / "servizi.json"
    p.write_text(json.dumps([{
        "chiave": "portale",
        "titolo": "Portale Segnalazioni",
        "dominio": "segnalatori.example.com",
        "url_salute": "http://100.64.0.3:8003/api/health",
        "container": ["portale_segnalazioni_backend_prod"],
        "compose_file": "docker-compose.prod.yml",
        "project_dir": "X:/srv/PortaleWeb_Segnalatori",
    }]), encoding="utf-8")

    servizi = sentinella_config.carica(str(p))

    assert len(servizi) == 1
    assert servizi[0].chiave == "portale"
    assert servizi[0].container == ("portale_segnalazioni_backend_prod",)
    assert servizi[0].critico is True


def test_valida_rifiuta_servizio_senza_container():
    s = sentinella_config.Servizio(
        chiave="x", titolo="X", dominio="x.it", url_salute="http://x/health",
        container=(), compose_file="c.yml", project_dir="/srv/x",
    )
    errori = sentinella_config.valida(s)
    assert any("container" in e for e in errori)


def test_servizio_non_valido_viene_scartato_non_tenuto_a_meta(tmp_path):
    """Un servizio dichiarato male produrrebbe un allarme permanente."""
    p = tmp_path / "servizi.json"
    p.write_text(json.dumps([
        {"chiave": "buono", "dominio": "b.it", "url_salute": "http://b/h",
         "container": ["c1"]},
        {"chiave": "rotto", "dominio": "r.it", "url_salute": "http://r/h",
         "container": []},
    ]), encoding="utf-8")

    servizi = sentinella_config.carica(str(p))

    assert [s.chiave for s in servizi] == ["buono"]


def test_carica_su_file_assente_ritorna_lista_vuota(tmp_path):
    assert sentinella_config.carica(str(tmp_path / "manca.json")) == []
