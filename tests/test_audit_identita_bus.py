"""L'audit delle identita' bus (issue #27, punto 1).

Ogni caso qui sotto e' uno che si e' visto davvero in produzione il 2026-09-11,
non un'ipotesi: un'identita' di un altro device sul box prod, un nome vecchio che
punta a un bus diverso da quello nuovo, un repo che parla del bus senza avere
un'identita'. Nessuno di questi da errore a runtime — il guasto e' sempre e solo
silenzio, e per questo va preso da un test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

import local_defaults

_MOD = Path(__file__).resolve().parents[1] / "scripts" / "audit_identita_bus.py"

# I suffissi dei device oltre a `-dev-linux` sono dell'installazione
# (BUS_SUFFISSI_DEVICE in locale/defaults.json, issue #43) e il modulo li legge
# all'import: lo si carica contro un locale di prova, non quello vero.
_SUFFISSI_DI_PROVA = [
    "prefisso|x:/srv|-dev-webserver",
    "contiene|/srv/core-box|-dev-core",
]


def _carica_audit(suffissi: list[str] = _SUFFISSI_DI_PROVA):
    with tempfile.TemporaryDirectory() as d:
        locale = Path(d) / "defaults.json"
        locale.write_text(json.dumps({"BUS_SUFFISSI_DEVICE": suffissi}),
                          encoding="utf-8")
        prima = os.environ.get(local_defaults.LOCALE_FILE_ENV)
        os.environ[local_defaults.LOCALE_FILE_ENV] = str(locale)
        local_defaults.clear_cache()
        try:
            spec = importlib.util.spec_from_file_location("audit_identita_bus", _MOD)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            if prima is None:
                os.environ.pop(local_defaults.LOCALE_FILE_ENV, None)
            else:
                os.environ[local_defaults.LOCALE_FILE_ENV] = prima
            local_defaults.clear_cache()
    return mod


audit = _carica_audit()

NOTI = {"offerte-dev-linux", "claude-dev-core", "offerte-prod"}


def test_identita_dal_campo_esplicito():
    assert audit.identita({"HUB_AGENT_ID": "offerte-dev-linux"}) == (
        "offerte-dev-linux", "HUB_AGENT_ID")


def test_identita_dal_fallback_local_agents():
    """Sui box prod l'identita' interattiva e' la voce *:claude."""
    env = {"HUB_LOCAL_AGENTS": "offerte-prod:offerte,claude-dev-core:claude"}
    assert audit.identita(env) == ("claude-dev-core", "HUB_LOCAL_AGENTS")


def test_identita_assente():
    assert audit.identita({"HUB_URL": "http://x"}) == ("", "")


def test_repo_a_posto_non_dice_niente():
    env = {"HUB_URL": "http://x", "HUB_AGENT_ID": "offerte-dev-linux"}
    assert audit.analizza(env, "-dev-linux", NOTI) == []


def test_identita_di_un_altro_device():
    """Il caso OutBound sul box prod: claima l'agente del PC dev-linux."""
    env = {"HUB_URL": "http://x", "HUB_AGENT_ID": "offerte-dev-linux"}
    note = audit.analizza(env, "-dev-core", NOTI)
    assert any("ALTRO device" in n for n in note)


def test_identita_non_registrata_sul_bus():
    env = {"HUB_URL": "http://x", "HUB_AGENT_ID": "fantasma-dev-linux"}
    note = audit.analizza(env, "-dev-linux", NOTI)
    assert any("non e' registrato" in n for n in note)


def test_bus_non_interrogabile_non_inventa_anomalie():
    """Set vuoto = non si e' potuto chiedere. Non e' 'non esiste'."""
    env = {"HUB_URL": "http://x", "HUB_AGENT_ID": "fantasma-dev-linux"}
    assert audit.analizza(env, "-dev-linux", set()) == []


def test_url_senza_identita():
    note = audit.analizza({"HUB_URL": "http://x"}, "-dev-linux", NOTI)
    assert any("il claim non scatta mai" in n for n in note)


def test_nome_vecchio_che_punta_a_un_altro_bus():
    """Il caso Offerte: hub-listener.py preferisce HUB_BASE_URL, che qui e' prod."""
    env = {"HUB_AGENT_ID": "offerte-dev-linux",
           "HUB_URL": "http://100.64.0.1:10002",
           "HUB_BASE_URL": "https://bus.example.com"}
    note = audit.analizza(env, "-dev-linux", NOTI)
    assert any("non concordano" in n for n in note)


def test_nome_vecchio_concorde_non_e_un_problema():
    env = {"HUB_AGENT_ID": "offerte-dev-linux",
           "HUB_URL": "http://x", "HUB_BASE_URL": "http://x"}
    assert audit.analizza(env, "-dev-linux", NOTI) == []


def test_solo_il_nome_vecchio():
    env = {"HUB_BASE_URL": "http://x", "HUB_AGENT_NAME": "db-dev"}
    note = audit.analizza(env, "-dev-webserver", NOTI)
    assert any("solo il nome vecchio HUB_AGENT_NAME" in n for n in note)


def test_porta_ritirata():
    env = {"HUB_AGENT_ID": "offerte-dev-linux", "HUB_URL": "http://100.64.0.1:8031"}
    note = audit.analizza(env, "-dev-linux", NOTI)
    assert any("porta ritirata" in n for n in note)


def test_identita_prod_non_e_giudicata_per_device():
    """Un'identita' -prod appartiene a un listener headless, non a questo device."""
    env = {"HUB_URL": "http://x", "HUB_AGENT_ID": "offerte-prod"}
    assert audit.analizza(env, "-dev-core", NOTI) == []


def test_suffisso_device(tmp_path):
    assert audit.suffisso_device("Linux", Path("./workspaces")) == "-dev-linux"
    assert audit.suffisso_device("Linux", Path("/srv/core-box/repos")) == "-dev-core"
    assert audit.suffisso_device("Windows", Path("X:/srv")) == "-dev-webserver"


def test_suffisso_device_senza_locale_resta_quello_del_motore():
    """Un'installazione nuova non ha device suoi: Linux e' -dev-linux, il resto nessuno."""
    nudo = _carica_audit([])
    assert nudo.suffisso_device("Linux", Path("/srv/core-box/repos")) == "-dev-linux"
    assert nudo.suffisso_device("Windows", Path("X:/srv")) == ""


def test_leggi_env_ignora_commenti_e_virgolette(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# commento\nHUB_AGENT_ID="pippo-dev-linux"\nriga senza uguale\n', encoding="utf-8")
    assert audit.leggi_env(f)["HUB_AGENT_ID"] == "pippo-dev-linux"
