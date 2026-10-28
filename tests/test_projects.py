# tests/test_projects.py
import json
import textwrap

import projects
from projects import _DEFAULT_PROJECTS, load_projects


def test_escape_path_windows_and_container():
    assert projects.escape_path(r"G:\PersonalChef") == "G--PersonalChef"
    assert projects.escape_path("/workspaces/cosmos") == "-workspaces-cosmos"
    # The drive letter is carried through, never rewritten: sessions recorded from
    # $SRV_ROOT/Cosmos live in X--srv-Cosmos. (Before the 2026-07 consolidation the same
    # checkout was reached as $SRV_ROOT/Cosmos, hence the old X--srv-* folders.)
    assert projects.escape_path(r"X:\srv\Cosmos") == "X--srv-Cosmos"


def test_resolve_project_by_key_and_alias():
    assert projects.resolve_project("mycorise").key == "mycorise"
    assert projects.resolve_project("personalChef").key == "personalChef"  # slug
    assert projects.resolve_project("nope") is None
    assert projects.resolve_project(None) is None


def test_default_registry_keys():
    # Asserts the built-in _DEFAULT_PROJECTS tuple, NOT list_projects(): the
    # latter resolves the live per-device registry (Life graph / projects.local.json),
    # so pinning it here makes the suite fail on any machine whose registry differs.
    keys = {p.key for p in _DEFAULT_PROJECTS}
    assert keys == {
        "academicAssistant",
        "personalChef",
        "mycorise",
        "travel",
        "experienceFinder",
        "stayFinder",
        "transportFinder",
        "offerte",
        "cruscotto",
        "etl",
        "db",
        "segnalatori",
        "scheduler",
        "network",  # ex "agenthub" — rinominato con la migrazione al bus Network
        "offerte-pubbliche",
        "launcher",  # ex "dashboard" (agent-dashboard, nome morto)
        "cosmos",
        "tabella",
        "claude",
        "homeResearcher",
    }


def test_resolve_project_travel_by_key():
    assert projects.resolve_project("travel").key == "travel"
    assert projects.resolve_project("travel").emoji == "🧳"
    # legacy Italian aliases still resolve, the old monolith key does not
    assert projects.resolve_project("viaggi").key == "travel"
    assert projects.resolve_project("travelers") is None


def test_travel_container_project_dir():
    p = projects.resolve_project("travel")
    assert projects.container_project_dir(p) == "-workspaces-travel"


# --- Task 2: agent discovery ---


def _make_workspace(tmp_path):
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "builder.md").write_text(
        textwrap.dedent("""\
        ---
        name: builder
        description: Builds the event builder UI and fixes its bugs.
        ---
        You are the builder specialist. Focus on the Builder/EventBasics area.
        """),
        encoding="utf-8",
    )
    (agents_dir / "solver.md").write_text(
        textwrap.dedent("""\
        ---
        description: Constraint solver and scheduling logic.
        ---
        You are the solver. Optimize constraints.
        """),
        encoding="utf-8",
    )
    return tmp_path


def test_agents_of_lists_generale_first_then_files(tmp_path):
    ws = _make_workspace(tmp_path)
    p = projects.Project("x", "X", "🔹", r"C:\x", str(ws))
    agents = projects.agents_of(p)
    assert agents[0].name == "generale" and agents[0].persona is None
    names = [a.name for a in agents]
    assert names == ["generale", "builder", "solver"]
    builder = agents[1]
    assert "Builds the event builder" in builder.description
    assert "builder specialist" in builder.persona  # body, frontmatter stripped


def test_agents_of_only_generale_when_no_agents_dir(tmp_path):
    p = projects.Project("y", "Y", "🔹", r"C:\y", str(tmp_path))
    agents = projects.agents_of(p)
    assert [a.name for a in agents] == ["generale"]


# --- Life graph drift check ---


def _write_graph(tmp_path, nodes):
    path = tmp_path / "life.graph.json"
    path.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    return path


def test_life_graph_drift_none_when_file_missing(tmp_path):
    assert projects.life_graph_drift(tmp_path / "nope.json") is None


def test_life_graph_drift_detects_missing_and_extra(monkeypatch, tmp_path):
    monkeypatch.setattr(
        projects,
        "PROJECTS",
        (projects.Project("mycorise", "Mycorise", "🍄", r"C:\x", "/w", kind="personal"),),
    )
    graph_path = _write_graph(
        tmp_path,
        {
            "mycorise": {"kind": "product", "exists": True, "children": []},
            "experienceFinder": {"kind": "agent", "exists": True, "children": []},
            "phytotherapy": {"kind": "agent", "exists": False, "children": []},
        },
    )
    drift = projects.life_graph_drift(graph_path)
    assert drift == {
        "missing_in_coordinator": ["experienceFinder"],
        "not_in_life_graph": [],
    }


def test_life_graph_drift_matches_through_aliases(monkeypatch, tmp_path):
    # Life e il Coordinator nominano diversamente le stesse cose: il nodo
    # `hubOfferte` È il progetto `offerte`. Prima il confronto era fra stringhe
    # e lo segnalava come mancante — rumore che nascondeva le assenze vere.
    monkeypatch.setattr(
        projects,
        "PROJECTS",
        (projects.Project("offerte", "Hub Offerte", "💼", r"C:\x", "/w",
                          aliases=("hubOfferte",), kind="work"),),
    )
    graph_path = _write_graph(
        tmp_path,
        {
            "hubOfferte": {"kind": "agent", "exists": True, "children": []},
            "sconosciuto": {"kind": "agent", "exists": True, "children": []},
        },
    )
    assert projects.life_graph_drift(graph_path) == {
        "missing_in_coordinator": ["sconosciuto"],
        "not_in_life_graph": [],
    }


def test_life_graph_drift_counts_framework_overrides_as_known(monkeypatch, tmp_path):
    # I nodi del framework inclusi dall'istanza compaiono solo fra gli
    # `overrides`: /api/graph serve il file grezzo, senza risolvere
    # includeFramework. Senza questo sarebbero falsi positivi permanenti.
    monkeypatch.setattr(
        projects,
        "PROJECTS",
        (projects.Project("network", "Network", "🛰", r"C:\x", "/w", kind="work"),),
    )
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps({
        "nodes": {"cruscotto": {"kind": "agent", "exists": True, "children": []}},
        "overrides": {"network": {"node": "X:/srv/Network"}},
    }), encoding="utf-8")
    drift = projects.life_graph_drift(graph_path)
    assert drift["not_in_life_graph"] == []
    assert drift["missing_in_coordinator"] == ["cruscotto"]


def test_life_graph_drift_skips_clusters_root_and_system_container(monkeypatch, tmp_path):
    monkeypatch.setattr(projects, "PROJECTS", ())
    graph_path = _write_graph(
        tmp_path,
        {
            "home": {"kind": "cluster", "exists": True, "children": ["homeResearcher"]},
            "life": {"kind": "root", "exists": True, "children": ["home"]},
            "system": {"kind": "system", "exists": True, "children": ["coordinator"]},
            "coordinator": {"kind": "system", "exists": True, "children": []},
        },
    )
    drift = projects.life_graph_drift(graph_path)
    assert drift["missing_in_coordinator"] == ["coordinator"]


def test_life_graph_drift_still_flags_active_node_that_gained_children(monkeypatch, tmp_path):
    # A real, still-selectable node (e.g. homeResearcher after the RentFinder
    # split) must NOT be excluded just because it now has children in the
    # taxonomy tree — only "cluster"/"root" kind and the "system" id are.
    monkeypatch.setattr(projects, "PROJECTS", ())
    graph_path = _write_graph(
        tmp_path,
        {"homeResearcher": {"kind": "agent", "exists": True, "children": ["rentFinder"]}},
    )
    drift = projects.life_graph_drift(graph_path)
    assert drift["missing_in_coordinator"] == ["homeResearcher"]


def test_resolve_agent_by_name_and_default(tmp_path):
    ws = _make_workspace(tmp_path)
    p = projects.Project("x", "X", "🔹", "", str(ws))
    assert projects.resolve_agent(p, "builder").name == "builder"
    assert projects.resolve_agent(p, "BUILDER").name == "builder"  # case-insensitive
    assert projects.resolve_agent(p, None).name == "generale"
    assert projects.resolve_agent(p, "   ").name == "generale"  # whitespace -> Generale
    assert projects.resolve_agent(p, "nope") is None


def test_parse_agent_md_frontmatter_only_no_body(tmp_path):
    d = tmp_path / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "meta.md").write_text(
        "---\ndescription: Only frontmatter here.\n---\n", encoding="utf-8"
    )
    p = projects.Project("z", "Z", "🔹", "", str(tmp_path))
    meta = next(a for a in projects.agents_of(p) if a.name == "meta")
    assert "Only frontmatter here" in meta.description
    assert meta.persona is None  # empty body -> None


# --- Task 7: session discovery (sessions_of) ---

import json as _json


def _write_session(dir_, sid, title=None, agent=None, first_user=None):
    dir_.mkdir(parents=True, exist_ok=True)
    lines = []
    if title is not None:
        lines.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    if agent is not None:
        lines.append({"type": "agent-name", "agentName": agent, "sessionId": sid})
    if first_user is not None:
        lines.append(
            {"type": "user", "message": {"role": "user", "content": first_user}}
        )
    (dir_ / f"{sid}.jsonl").write_text(
        "\n".join(_json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )


def test_sessions_of_merges_pc_and_tg_titled_and_sorted(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(projects, "PROJECTS_ROOT", str(root))
    p = projects.Project(
        "cosmos", "🛠 Cosmos", "🛠", r"X:\srv\Cosmos", "/workspaces/cosmos"
    )
    _write_session(
        root / projects.escape_path(p.host_path),
        "aaaaaaaa-0000-0000-0000-000000000001",
        title="builder ricorrenze",
        agent="builder",
    )
    _write_session(
        root / "-workspaces-cosmos",
        "bbbbbbbb-0000-0000-0000-000000000002",
        first_user="fix auth jwt",
    )
    import os
    import time

    os.utime(
        root / "-workspaces-cosmos" / "bbbbbbbb-0000-0000-0000-000000000002.jsonl",
        (time.time(), time.time()),
    )
    os.utime(
        root / projects.escape_path(p.host_path) / "aaaaaaaa-0000-0000-0000-000000000001.jsonl",
        (time.time() - 60, time.time() - 60),
    )
    sessions = projects.sessions_of(p, limit=10)
    assert [s.source for s in sessions] == ["TG", "PC"]  # newest first
    assert sessions[0].title == "fix auth jwt"
    assert sessions[1].title == "builder ricorrenze" and sessions[1].agent == "builder"


def test_sessions_of_without_host_path_claims_nothing_of_its_neighbours(
    tmp_path, monkeypatch
):
    """No host path means no PC sessions — not "all of them".

    The live Life registry has a path only for the devices the graph names, so
    a node without an entry for this box arrives with host_path "". The PC
    branch matched on `startswith(prefix)`, and every string starts with "" —
    so such a project used to list every session directory on the machine,
    other projects' included.
    """
    root = tmp_path / "projects"
    monkeypatch.setattr(projects, "PROJECTS_ROOT", str(root))
    _write_session(
        root / "-nodes-SomeoneElse",
        "dddddddd-0000-0000-0000-000000000004",
        title="not mine",
    )
    homeless = projects.Project("travel", "Travel", "", "", "/workspaces/travel")
    assert projects.sessions_of(homeless, limit=10) == []


def test_sessions_of_respects_limit_and_dedup(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(projects, "PROJECTS_ROOT", str(root))
    p = projects.Project(
        "cosmos", "🛠 Cosmos", "🛠", r"X:\srv\Cosmos", "/workspaces/cosmos"
    )
    dup = "cccccccc-0000-0000-0000-000000000003"
    _write_session(root / projects.escape_path(p.host_path), dup, title="dup")
    _write_session(root / "-workspaces-cosmos", dup, title="dup-tg")
    sessions = projects.sessions_of(p, limit=10)
    ids = [s.id for s in sessions]
    assert ids.count(dup) == 1
    assert next(s for s in sessions if s.id == dup).source == "TG"  # container wins


def test_sessions_of_personalChef_finds_pc_from_host_path(tmp_path, monkeypatch):
    """Regression (issue #1): personalChef PC sessions live under the folder derived from
    its host_path, whatever that is on this device (G:\\PersonalChef before the 2026-07
    consolidation, <SRV_ROOT>/PersonalChef after). /resume must list them, and TG sessions
    in -workspaces-personalChef must keep working."""
    root = tmp_path / "projects"
    monkeypatch.setattr(projects, "PROJECTS_ROOT", str(root))
    p = projects.resolve_project("personalChef")
    pc_dir = projects.escape_path(p.host_path)
    assert pc_dir.endswith("PersonalChef")
    _write_session(
        root / pc_dir,
        "11111111-0000-0000-0000-000000000001",
        title="dinner tonight",
        agent="nutrition-query-runner",
    )
    _write_session(
        root / "-workspaces-personalChef",
        "22222222-0000-0000-0000-000000000002",
        first_user="weekly menu",
    )
    by_id = {s.id: s for s in projects.sessions_of(p, limit=10)}
    assert by_id["11111111-0000-0000-0000-000000000001"].source == "PC"  # host checkout
    assert by_id["22222222-0000-0000-0000-000000000002"].source == "TG"  # container


# --- Task 8: prepare_resume ---


def test_prepare_resume_copies_pc_into_container(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(projects, "PROJECTS_ROOT", str(root))
    p = projects.Project(
        "cosmos", "🛠 Cosmos", "🛠", r"X:\srv\Cosmos", "/workspaces/cosmos"
    )
    sid = "dddddddd-0000-0000-0000-000000000004"
    _write_session(root / projects.escape_path(p.host_path), sid, title="pc one")
    ref = projects.SessionRef(sid, "PC", "pc one", None, 0.0)
    assert projects.prepare_resume(p, ref) == sid
    assert (root / "-workspaces-cosmos" / f"{sid}.jsonl").exists()


def test_prepare_resume_tg_is_noop(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(projects, "PROJECTS_ROOT", str(root))
    p = projects.Project(
        "cosmos", "🛠 Cosmos", "🛠", r"X:\srv\Cosmos", "/workspaces/cosmos"
    )
    sid = "eeeeeeee-0000-0000-0000-000000000005"
    _write_session(root / "-workspaces-cosmos", sid, title="tg one")
    ref = projects.SessionRef(sid, "TG", "tg one", None, 0.0)
    assert projects.prepare_resume(p, ref) == sid


# --- label/emoji + button label ---


def _ws_with_labeled_agent(tmp_path):
    d = tmp_path / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "cook.md").write_text(
        "---\n"
        "name: cook\n"
        "description: Cooks the dinner and plates it.\n"
        "label: Aiutami a cucinare\n"
        "emoji: \U0001f469‍\U0001f373\n"
        "---\nYou are the cook.\n",
        encoding="utf-8",
    )
    (d / "plain.md").write_text(
        "---\ndescription: No label here.\n---\nbody\n", encoding="utf-8"
    )
    return tmp_path


def test_parse_agent_md_extracts_label_and_emoji(tmp_path):
    p = projects.Project(
        "x", "X", "\U0001f539", "", str(_ws_with_labeled_agent(tmp_path))
    )
    cook = next(a for a in projects.agents_of(p) if a.name == "cook")
    assert cook.label == "Aiutami a cucinare"
    assert cook.emoji == "\U0001f469‍\U0001f373"
    plain = next(a for a in projects.agents_of(p) if a.name == "plain")
    assert plain.label is None and plain.emoji is None


def test_agent_button_label_prefers_emoji_label_then_slug(tmp_path):
    p = projects.Project(
        "x", "X", "\U0001f539", "", str(_ws_with_labeled_agent(tmp_path))
    )
    cook = next(a for a in projects.agents_of(p) if a.name == "cook")
    plain = next(a for a in projects.agents_of(p) if a.name == "plain")
    assert (
        projects.agent_button_label(cook) == "\U0001f469‍\U0001f373 Aiutami a cucinare"
    )
    assert projects.agent_button_label(plain) == "plain"
    assert projects.agent_button_label(projects.GENERALE) == "\U0001f4ac Generale"


# --- config-driven per-device registry: load_projects() ---


def test_load_projects_fallback_default(tmp_path, monkeypatch):
    """No config file at the configured path -> load_projects() delegates to
    life_registry_client.load_projects_from_life(fallback=_DEFAULT_PROJECTS), which is
    mocked here to return the fallback directly so the test stays hermetic (no real
    HTTP call to life_backend, no dependency on a stale projects.cache.json on disk)."""
    monkeypatch.setenv("COORDINATOR_PROJECTS_CONFIG", str(tmp_path / "missing.json"))

    import life_registry_client

    def fake_load_from_life(*, fallback):
        return fallback

    monkeypatch.setattr(life_registry_client, "load_projects_from_life", fake_load_from_life)

    assert load_projects() == _DEFAULT_PROJECTS


def test_load_projects_from_config(tmp_path, monkeypatch):
    """A config file is parsed into Project objects with the right fields, aliases
    as tuples, and kind honored (default 'work' when omitted)."""
    cfg = tmp_path / "projects.local.json"
    cfg.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "key": "personalChef",
                        "label": "🍳 personalChef",
                        "emoji": "🍳",
                        "host_path": "/home/me/PersonalChef",
                        "workspace": "/workspaces/personalChef",
                        "aliases": ["chef", "cook"],
                        "kind": "personal",
                    },
                    {  # no "kind" -> defaults to "work"
                        "key": "offerte",
                        "label": "💼 Hub Offerte",
                        "emoji": "💼",
                        "host_path": "/home/me/Offerte",
                        "workspace": "/workspaces/offerte",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("COORDINATOR_PROJECTS_CONFIG", str(cfg))
    # With no live registry to merge into, the local file IS the registry --
    # the historic behaviour, which the supplement of issue #35 must not change.
    import life_registry_client
    monkeypatch.setattr(
        life_registry_client,
        "load_projects_from_life",
        lambda *, fallback, test_mounts=True: fallback,
    )

    loaded = load_projects()
    assert len(loaded) == 2
    assert all(isinstance(p, projects.Project) for p in loaded)

    toni, offerte = loaded
    assert toni.key == "personalChef" and toni.label == "🍳 personalChef" and toni.emoji == "🍳"
    assert (
        toni.host_path == "/home/me/PersonalChef"
        and toni.workspace == "/workspaces/personalChef"
    )
    assert toni.aliases == ("chef", "cook") and isinstance(toni.aliases, tuple)
    assert toni.session_globs == ()
    assert toni.kind == "personal"

    assert offerte.key == "offerte"
    assert offerte.aliases == () and offerte.kind == "work"  # kind defaulted


def test_load_projects_malformed_raises(tmp_path, monkeypatch):
    """Invalid JSON and a missing required key both raise ValueError."""
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{ not valid json ", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_PROJECTS_CONFIG", str(bad_json))
    try:
        load_projects()
        assert False, "expected ValueError for invalid JSON"
    except ValueError:
        pass

    missing_key = tmp_path / "missing_key.json"
    missing_key.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "label": "No Key",
                        "emoji": "❓",
                        "host_path": "/h",
                        "workspace": "/w",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("COORDINATOR_PROJECTS_CONFIG", str(missing_key))
    try:
        load_projects()
        assert False, "expected ValueError for missing 'key'"
    except ValueError:
        pass


# --- intent routing: keywords + resolve_agent_by_intent (issue #16) ---


def _ws_with_keyword_agents(tmp_path):
    d = tmp_path / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "logistica.md").write_text(
        "---\nname: logistica\ndescription: Lodging and transport.\n"
        "keywords: hotel, alloggio, volo, b&b\n---\nYou handle logistics.\n",
        encoding="utf-8",
    )
    (d / "destinazioni.md").write_text(
        "---\nname: destinazioni\ndescription: Destinations.\n"
        "keywords: [destinazione, itinerario, dove andare]\n---\nDestinations.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_parse_keywords_comma_and_yaml_list(tmp_path):
    p = projects.Project("x", "X", "🔹", "", str(_ws_with_keyword_agents(tmp_path)))
    by = {a.name: a for a in projects.agents_of(p)}
    assert by["logistica"].keywords == ("hotel", "alloggio", "volo", "b&b")
    assert by["destinazioni"].keywords == ("destinazione", "itinerario", "dove andare")
    assert projects.GENERALE.keywords == ()  # no keywords -> never intent-selected


def test_resolve_agent_by_intent_routes_and_falls_back(tmp_path):
    p = projects.Project("x", "X", "🔹", "", str(_ws_with_keyword_agents(tmp_path)))
    g = projects.GENERALE
    by_intent = projects.resolve_agent_by_intent
    assert by_intent(p, "mi trovi un hotel per stasera?", g).name == "logistica"
    assert by_intent(p, "non so dove andare quest'estate", g).name == "destinazioni"
    assert by_intent(p, "ciao come stai", g).name == "generale"  # no match -> default
    assert by_intent(p, "", g).name == "generale"  # empty -> default
    # word-boundary: 'volo' must NOT match inside 'rivolto'
    assert by_intent(p, "mi sono rivolto a un amico", g).name == "generale"


def test_resolve_agent_by_intent_default_wins_ties(tmp_path):
    p = projects.Project("x", "X", "🔹", "", str(_ws_with_keyword_agents(tmp_path)))
    logistica = next(a for a in projects.agents_of(p) if a.name == "logistica")
    # bound default is preserved when nothing matches...
    assert projects.resolve_agent_by_intent(p, "ciao", logistica).name == "logistica"
    # ...and on equal scores (hotel=logistica, itinerario=destinazioni) the default keeps it
    assert (
        projects.resolve_agent_by_intent(p, "hotel e itinerario", logistica).name
        == "logistica"
    )
    # a clearly stronger other-agent signal still overrides
    assert (
        projects.resolve_agent_by_intent(p, "che itinerario consigli?", logistica).name
        == "destinazioni"
    )


def _ws_with_settori(tmp_path):
    d = tmp_path / "ws" / ".claude" / "agents"
    d.mkdir(parents=True)
    for n in ("settore-azienda", "settore-marketing"):
        (d / f"{n}.md").write_text(f"---\nname: {n}\ndescription: {n}\n---\nPersona\n", encoding="utf-8")
    return tmp_path / "ws"


def test_competence_offer_routes_to_sector_agent(tmp_path):
    p = projects.Project("x", "X", "🔹", "", str(_ws_with_settori(tmp_path)))
    g = projects.GENERALE
    testo = "Ciao sarai il nostro supporto in tutto l'aspetto di legalità e branding."
    assert projects.resolve_agent_by_intent(p, testo, g).name == "settore-azienda"
    marketing = "Sono una grafica, mi occupo di branding e logo"
    assert projects.resolve_agent_by_intent(p, marketing, g).name == "settore-marketing"
    # nessuna offerta -> default
    assert projects.resolve_agent_by_intent(p, "come va il logo?", g).name == "generale"
    # una scheda legata a un agente non viene scavalcata
    az = next(a for a in projects.agents_of(p) if a.name == "settore-marketing")
    assert projects.resolve_agent_by_intent(p, testo, az).name == "settore-marketing"


def test_competence_offer_without_sector_agent_keeps_default(tmp_path):
    p = projects.Project("x", "X", "🔹", "", str(_ws_with_keyword_agents(tmp_path)))
    testo = "Sono un avvocato, mi occupo di contratti"
    assert projects.resolve_agent_by_intent(p, testo, projects.GENERALE).name == "generale"
