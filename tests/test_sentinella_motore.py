"""Un servizio migrato in WSL non e' piu' visibile al Docker del box.

Il 26 agosto la sentinella ha suonato per un'ora su `offerte-pubbliche` — sano,
ma spostato su un altro motore — e ha tentato dodici riparazioni sul motore
sbagliato. Un allarme che grida al lupo e' peggio di nessun allarme: si impara a
ignorarlo, ed e' esattamente il difetto che la sentinella esiste per eliminare.
"""
import sentinella
import sentinella_config
import sentinella_probes
import sentinella_repair
from datetime import datetime, timezone
from sentinella_config import Servizio
from sentinella_store import StatoServizio

T0 = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)


def _srv(**kw):
    base = dict(chiave="x", titolo="X", dominio="x.it", url_salute="http://x/h",
                container=("c1",), project_dir="/srv/x")
    base.update(kw)
    return Servizio(**base)


def test_default_e_il_motore_del_box():
    assert _srv().motore == "desktop"
    assert sentinella_config.prefisso_docker(_srv()) == ""


def test_servizio_in_wsl_ha_un_prefisso():
    assert sentinella_config.prefisso_docker(_srv(motore="wsl")).startswith("wsl ")


def test_motore_sconosciuto_e_un_errore_di_configurazione():
    """Meglio scartare la voce che sorvegliare il motore sbagliato."""
    assert any("motore" in e for e in sentinella_config.valida(_srv(motore="podman")))


async def test_i_container_si_cercano_sul_motore_giusto():
    visti = []

    async def runner(comando):
        visti.append(comando)
        return 0, "c1\n"

    await sentinella_probes.sonda_container(
        ("c1",), runner=runner, prefisso="wsl -d Ubuntu-24.04 -u root -- ")
    assert visti[0].startswith("wsl -d Ubuntu-24.04")
    assert "docker ps" in visti[0]


async def test_la_riparazione_usa_il_motore_del_servizio():
    visti = []

    async def runner(comando):
        visti.append(comando)
        return 0, ""

    await sentinella_repair.ripara(
        _srv(motore="wsl", project_dir="/srv/offerte-pubbliche"),
        ["c1"], StatoServizio(), T0, runner=runner)
    assert visti[0].startswith("wsl ")
    assert "/srv/offerte-pubbliche" in visti[0]
    assert "docker compose" in visti[0]


async def test_la_riparazione_sul_box_resta_come_prima():
    visti = []

    async def runner(comando):
        visti.append(comando)
        return 0, ""

    await sentinella_repair.ripara(
        _srv(project_dir="X:/srv/x"), ["c1"], StatoServizio(), T0, runner=runner)
    assert not visti[0].startswith("wsl")
    assert visti[0].startswith("cd ")


def test_il_motore_viene_LETTO_dal_file(tmp_path, monkeypatch):
    """Aggiungere il campo alla dataclass non basta: va letto anche dal JSON.

    Dimenticarlo e' silenzioso — la voce carica, valida, e sorveglia il motore
    sbagliato — ed e' esattamente cio' che e' successo al primo tentativo.
    """
    import json
    f = tmp_path / "servizi.json"
    f.write_text(json.dumps([{
        "chiave": "x", "titolo": "X", "dominio": "x.it",
        "url_salute": "http://x/h", "container": ["c1"], "motore": "wsl",
    }]))
    monkeypatch.setattr(sentinella_config, "SERVIZI_PATH", str(f))
    assert sentinella_config.carica()[0].motore == "wsl"
