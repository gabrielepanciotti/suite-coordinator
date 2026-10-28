"""Il file locale completa il registro live, non lo sostituisce (issue #35).

La regola di fusione e' l'host path: due voci che puntano allo stesso checkout
sono un progetto solo sotto due nomi, e il nome del grafo vince perche' e'
quello che usa il resto della rete. Il nome storico non si perde — diventa un
alias, ed e' cosi' che `db-dev-linux` torna a risolvere su `dbAcme` senza una
tabella di corrispondenze scritta a mano.
"""
from __future__ import annotations

import json

import life_registry_client
import projects
from project_model import Project


def _live(monkeypatch, *nodi: Project) -> None:
    monkeypatch.setattr(
        life_registry_client,
        "load_projects_from_life",
        lambda *, fallback, test_mounts=True: nodi or fallback,
    )


def _config(tmp_path, monkeypatch, *voci: dict):
    path = tmp_path / "projects.local.json"
    path.write_text(json.dumps({"projects": list(voci)}), encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_PROJECTS_CONFIG", str(path))
    return path


def test_stesso_repo_il_nome_storico_diventa_un_alias(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    repo = str(tmp_path / "repo")
    _live(monkeypatch, Project("dbAcme", "DB", "", repo, "/workspaces/dbAcme"))
    _config(tmp_path, monkeypatch, {
        "key": "db", "label": "DB", "host_path": repo, "workspace": "/workspaces/db",
    })

    caricati = projects.load_projects()

    assert [p.key for p in caricati] == ["dbAcme"], "un repo, un progetto"
    assert "db" in caricati[0].aliases
    monkeypatch.setattr(projects, "PROJECTS", caricati)
    assert projects.resolve_project("db").key == "dbAcme"
    assert projects.resolve_project("dbAcme").key == "dbAcme"


def test_nodo_senza_percorso_qui_lo_prende_dalla_voce_locale(tmp_path, monkeypatch):
    """Sul minipc il grafo conosce ComUnity ma non il suo percorso: senza la
    voce locale l'host_path restava vuoto e i turni host-exec non partivano."""
    (tmp_path / "comunity").mkdir()
    repo = str(tmp_path / "comunity")
    _live(monkeypatch, Project("comunity", "ComUnity", "", "", "/workspaces/comunity"))
    _config(tmp_path, monkeypatch, {
        "key": "comunity", "label": "ComUnity", "host_path": repo,
        "workspace": "/workspaces/comunity",
    })

    caricati = projects.load_projects()

    assert [p.key for p in caricati] == ["comunity"], "un nodo, un progetto"
    assert caricati[0].host_path == repo


def test_il_workspace_montato_qui_ha_la_precedenza(tmp_path, monkeypatch):
    """Il grafo sa come si chiama un nodo; solo questa macchina sa dov'e' montato."""
    (tmp_path / "repo").mkdir()
    repo = str(tmp_path / "repo")
    _live(monkeypatch, Project("etlV2", "ETL", "", repo, "/workspaces/etlV2"))
    _config(tmp_path, monkeypatch, {
        "key": "etl", "label": "ETL", "host_path": repo, "workspace": "/workspaces/etl",
    })

    (nodo,) = projects.load_projects()

    assert nodo.workspace == "/workspaces/etl"
    # Il vecchio workspace nomina anche la cartella dei transcript: se sparisse,
    # /riprendi perderebbe ogni conversazione tenuta prima del rinomino.
    assert "/workspaces/etlV2" in nodo.session_globs


def test_quello_che_life_non_conosce_viene_aggiunto(tmp_path, monkeypatch):
    """~/.claude non e' un nodo Life, e i nodi personali stanno in un'altra istanza."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _live(monkeypatch, Project("cruscotto", "C", "", str(tmp_path / "a"), "/workspaces/cruscotto"))
    _config(tmp_path, monkeypatch, {
        "key": "claude", "label": "Config", "host_path": str(tmp_path / "b"),
        "workspace": "/workspaces/claude-config",
    })

    caricati = projects.load_projects()

    assert sorted(p.key for p in caricati) == ["claude", "cruscotto"]


def test_senza_registro_live_il_file_locale_resta_il_registro(tmp_path, monkeypatch):
    """Un ripiego non si traveste da risposta piu' ricca."""
    _live(monkeypatch)  # restituisce il fallback -> nessun registro live
    _config(tmp_path, monkeypatch, {
        "key": "solo", "label": "S", "host_path": "/nodes/solo", "workspace": "/workspaces/solo",
    })

    assert [p.key for p in projects.load_projects()] == ["solo"]
    assert "override manuale" in projects.registry_source()


def test_host_path_vuoto_non_fonde_tutto_insieme(tmp_path, monkeypatch):
    """Un nodo senza path su questo device non e' "lo stesso repo" di un altro."""
    _live(monkeypatch,
          Project("uno", "U", "", "", "/workspaces/uno"),
          Project("due", "D", "", "", "/workspaces/due"))
    _config(tmp_path, monkeypatch, {
        "key": "tre", "label": "T", "host_path": "", "workspace": "/workspaces/tre",
    })

    assert sorted(p.key for p in projects.load_projects()) == ["due", "tre", "uno"]


def test_una_voce_locale_stantia_non_trascina_giu_il_nodo_vivo(tmp_path, monkeypatch):
    """Il repo si e' spostato: il mount buono e' quello del grafo, non il vecchio."""
    (tmp_path / "nuovo").mkdir()
    repo = str(tmp_path / "nuovo")
    _live(monkeypatch, Project("offertePubbliche", "OP", "", repo, "/workspaces/offertePubbliche"))
    _config(tmp_path, monkeypatch, {
        "key": "offerte-pubbliche", "label": "OP", "host_path": repo,
        "workspace": "/workspaces/offerte-pubbliche",
    })
    monkeypatch.setattr(
        life_registry_client, "workspace_montato",
        lambda ws: ws == "/workspaces/offertePubbliche",
    )

    (nodo,) = projects.load_projects()

    assert nodo.workspace == "/workspaces/offertePubbliche"
    assert "offerte-pubbliche" in nodo.aliases


def test_etichetta_ed_emoji_scelte_a_mano_sopravvivono(tmp_path, monkeypatch):
    """Il grafo decide il nome canonico; il menu lo legge una persona."""
    (tmp_path / "repo").mkdir()
    repo = str(tmp_path / "repo")
    _live(monkeypatch, Project("offertePubbliche", "VetrinaOffertePubbliche", "", repo,
                               "/workspaces/offertePubbliche"))
    _config(tmp_path, monkeypatch, {
        "key": "offerte-pubbliche", "label": "📣 Offerte Pubbliche", "emoji": "📣",
        "host_path": repo, "workspace": "/workspaces/offertePubbliche",
    })

    (nodo,) = projects.load_projects()

    assert nodo.key == "offertePubbliche"
    assert nodo.label == "📣 Offerte Pubbliche" and nodo.emoji == "📣"
