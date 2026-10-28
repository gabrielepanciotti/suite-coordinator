"""Menu navigation over Life's node graph — pure logic, no network.

The fixture graph below mirrors the real shape of ``life.graph.json``
(``{id, label, kind, children, exists, commercial}``) including the cases that
actually bite: a cluster three levels deep, a child that does not exist yet, a
commercial node, and a dangling child id.
"""
from __future__ import annotations

import json

import pytest

import life_registry_client
import node_menu


def _n(node_id, label, kind, children=(), exists=True, commercial=False):
    return {
        "id": node_id,
        "label": label,
        "kind": kind,
        "children": list(children),
        "exists": exists,
        "commercial": commercial,
    }


@pytest.fixture
def graph():
    return {
        "life": _n("life", "Life", "root", ["health", "food", "ghost"]),
        "health": _n("health", "Health", "cluster", ["fitness", "phyto", "buyFinder", "dead"]),
        "fitness": _n("fitness", "Fitness", "cluster", ["hiit", "yoga"]),
        "hiit": _n("hiit", "HIIT", "app"),
        "yoga": _n("yoga", "Yoga", "app"),
        "phyto": _n("phyto", "Fitoterapia", "app"),
        "buyFinder": _n("buyFinder", "Buy Finder", "app", commercial=True),
        "dead": _n("dead", "Not built yet", "app", exists=False),
        "food": _n("food", "Food", "cluster", ["personalChef"]),
        "personalChef": _n("personalChef", "Personal Chef", "app"),
        # "ghost" is referenced by life.children but has no node of its own
    }


def test_cluster_menu_lists_its_children(graph):
    menu = node_menu.build_menu("health", graph)
    assert [e.node_id for e in menu.entries] == ["fitness", "phyto"]
    assert [e.label for e in menu.entries] == ["Fitness", "Fitoterapia"]
    assert not menu.is_leaf


def test_menu_hides_nodes_that_do_not_exist_or_are_commercial(graph):
    ids = [e.node_id for e in node_menu.build_menu("health", graph).entries]
    assert "dead" not in ids, "a node not built yet must not be offered"
    assert "buyFinder" not in ids, "commercial nodes are not part of the personal menu"


def test_menu_skips_dangling_child_ids(graph):
    ids = [e.node_id for e in node_menu.build_menu("life", graph).entries]
    assert ids == ["health", "food"], "a child id with no node must be dropped, not crash"


def test_entry_knows_whether_it_navigates_or_opens_a_session(graph):
    entries = {e.node_id: e for e in node_menu.build_menu("health", graph).entries}
    assert entries["fitness"].is_cluster and not entries["fitness"].is_leaf
    assert entries["phyto"].is_leaf


def test_leaf_menu_signals_open_a_session(graph):
    menu = node_menu.build_menu("hiit", graph)
    assert menu.is_leaf
    assert menu.entries == ()
    assert node_menu.resolve_target("hiit", graph) == "session"
    assert node_menu.resolve_target("health", graph) == "menu"


def test_depth_three_navigation_with_back(graph):
    """Health -> Fitness -> HIIT, and Back climbs one level at a time."""
    health = node_menu.build_menu("health", graph, root_id="health")
    assert health.parent_id is None, "the bot's own root has no Back"

    fitness = node_menu.build_menu("fitness", graph, root_id="health")
    assert [e.node_id for e in fitness.entries] == ["hiit", "yoga"]
    assert fitness.parent_id == "health"
    assert fitness.breadcrumb == ("Health", "Fitness")

    hiit = node_menu.build_menu("hiit", graph, root_id="health")
    assert hiit.is_leaf
    assert hiit.parent_id == "fitness"
    assert hiit.breadcrumb == ("Health", "Fitness", "HIIT")


def test_back_never_escapes_the_bots_own_subtree(graph):
    """A Health bot must not let Back walk up into Life and across to Food."""
    menu = node_menu.build_menu("health", graph, root_id="health")
    assert menu.parent_id is None
    # Without a root the same node does expose its real parent.
    assert node_menu.build_menu("health", graph).parent_id == "life"


def test_parent_and_path_are_cycle_safe():
    cyclic = {
        "a": _n("a", "A", "cluster", ["b"]),
        "b": _n("b", "B", "cluster", ["a"]),
    }
    assert node_menu.path_to("a", cyclic)[-1] == "a"
    assert len(node_menu.path_to("a", cyclic)) <= node_menu.MAX_DEPTH + 1


def test_unknown_node_yields_no_menu(graph):
    assert node_menu.build_menu("nope", graph) is None
    assert node_menu.build_menu("health", {}) is None


def test_label_falls_back_to_the_id(graph):
    assert node_menu.label_of("hiit", graph) == "HIIT"
    assert node_menu.label_of("unknown", graph) == "unknown"


# --- the graph source: reuse of life_registry_client, still offline ----------
def test_load_graph_nodes_uses_the_live_api(monkeypatch):
    monkeypatch.setattr(
        life_registry_client,
        "fetch_nodes",
        lambda base_url: [_n("health", "Health", "cluster", ["hiit"]), _n("hiit", "HIIT", "app")],
    )
    nodes = life_registry_client.load_graph_nodes()
    assert set(nodes) == {"health", "hiit"}
    assert nodes["health"]["children"] == ["hiit"]


def test_load_graph_nodes_falls_back_to_the_shared_disk_cache(monkeypatch, tmp_path):
    """Same cache file the project registry writes — one source of truth."""
    import httpx

    cache = tmp_path / "projects.cache.json"
    cache.write_text(json.dumps([_n("health", "Health", "cluster")]), encoding="utf-8")

    def _boom(base_url):
        raise httpx.ConnectError("life_backend down")

    monkeypatch.setattr(life_registry_client, "fetch_nodes", _boom)
    nodes = life_registry_client.load_graph_nodes(cache_path=cache)
    assert list(nodes) == ["health"]


def test_load_graph_nodes_degrades_to_empty_without_api_or_cache(monkeypatch, tmp_path):
    import httpx

    def _boom(base_url):
        raise httpx.ConnectError("life_backend down")

    monkeypatch.setattr(life_registry_client, "fetch_nodes", _boom)
    assert life_registry_client.load_graph_nodes(cache_path=tmp_path / "missing.json") == {}
