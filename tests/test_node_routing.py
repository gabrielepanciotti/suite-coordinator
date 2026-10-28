"""Routing a message from a cluster's room down to the node it is about.

The rules under test, in order of importance:
  1. a link outranks a sentence;
  2. an ambiguous message becomes a question, never a guess;
  3. a node the graph marks as planned-but-not-built is never a destination.
"""
from __future__ import annotations

import pytest

import manifest
import node_routing
import projects

# A cluster shaped like the real Home one: a root, three built children, and one
# child that exists only on paper. Ids are synthetic.
GRAPH = {
    "root": {"label": "Root", "exists": True, "children": ["listings", "products", "bills"]},
    "listings": {"label": "Listings", "exists": True, "children": ["rentals", "sales"]},
    "rentals": {"label": "Rentals", "exists": False, "children": []},
    "sales": {"label": "Sales", "exists": False, "children": []},
    "products": {"label": "Products", "exists": True, "children": []},
    "bills": {"label": "Bills", "exists": True, "children": []},
}

RULES = {
    "listings": manifest.RoutingDecl(
        domains=("portal.example", "listings.example"),
        keywords=("annuncio", "affitto", "appartamento"),
    ),
    "products": manifest.RoutingDecl(
        domains=("shop.example",),
        keywords=("prodotto", "acquisto", "lampada"),
    ),
    "bills": manifest.RoutingDecl(keywords=("bolletta", "spesa", "conto")),
    "rentals": manifest.RoutingDecl(keywords=("canone", "inquilino")),
}


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    """Every node in GRAPH resolves to a project whose manifest declares RULES."""

    def _resolve(key):
        if key in GRAPH:
            return projects.Project(
                key=key, label=key, emoji="", host_path=f"/hosts/{key}",
                workspace=f"/workspaces/{key}",
            )
        return None

    monkeypatch.setattr(projects, "resolve_project", _resolve)
    monkeypatch.setattr(node_routing, "projects", projects)
    monkeypatch.setattr(
        manifest, "routing_of", lambda p: RULES.get(p.key, manifest.RoutingDecl())
    )


# --- host extraction ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("guarda https://www.portal.example/annunci/12", ("portal.example",)),
        ("portal.example/x", ("portal.example",)),
        ("http://sub.portal.example/x e shop.example", ("sub.portal.example", "shop.example")),
        ("nessun link qui", ()),
        ("", ()),
    ],
)
def test_hosts_are_found_in_however_a_human_writes_them(text, expected):
    assert node_routing.hosts_in(text) == expected


def test_a_domain_matches_its_subdomains_but_not_a_lookalike():
    assert node_routing._domain_hit("casa.it", "www.casa.it")
    assert node_routing._domain_hit("casa.it", "casa.it")
    assert not node_routing._domain_hit("casa.it", "nocasa.it"), (
        "the boundary must be a dot, or one node steals another's traffic"
    )


# --- the two signals ---------------------------------------------------------


def test_a_link_decides_even_against_a_pile_of_words_for_a_sibling():
    """Rule 1. The words all point at products; the single link does not."""
    text = "prodotto acquisto lampada — comunque guarda portal.example/annunci/9"
    decision = node_routing.route(text, "root", GRAPH)
    assert decision.kind == "routed"
    assert decision.node_id == "listings"


def test_words_alone_still_route_when_they_are_unopposed():
    decision = node_routing.route("ho pagato la bolletta del gas", "root", GRAPH)
    assert decision.kind == "routed" and decision.node_id == "bills"


def test_nothing_recognisable_stays_at_the_root():
    decision = node_routing.route("buongiorno a tutti", "root", GRAPH)
    assert decision.kind == "root" and decision.node_id == "root"


# --- rule 2: ambiguity becomes a question ------------------------------------


def test_a_tie_between_siblings_is_asked_not_guessed():
    """One keyword each: the routing must refuse to pick."""
    decision = node_routing.route("un prodotto e un annuncio", "root", GRAPH)
    assert decision.kind == "ambiguous" and decision.needs_question
    assert {c.node_id for c in decision.candidates} == {"listings", "products"}


def test_two_links_claimed_by_two_nodes_are_also_a_question():
    decision = node_routing.route("portal.example/a e shop.example/b", "root", GRAPH)
    assert decision.kind == "ambiguous"
    assert {c.node_id for c in decision.candidates} == {"listings", "products"}


def test_a_stronger_score_breaks_what_would_otherwise_be_a_tie():
    decision = node_routing.route("annuncio affitto e un prodotto", "root", GRAPH)
    assert decision.kind == "routed" and decision.node_id == "listings"


# --- rule 3: a planned node is not a destination -----------------------------


def test_a_planned_but_unbuilt_node_hands_over_to_its_nearest_built_ancestor():
    decision = node_routing.route("il canone è troppo alto", "root", GRAPH)
    assert decision.kind == "unbuilt"
    assert decision.planned == "rentals", "the caller must be able to name it"
    assert decision.node_id == "listings", "and to say where the work actually goes"


def test_the_root_is_the_floor_when_no_ancestor_is_built(monkeypatch):
    graph = {
        "root": {"exists": True, "children": ["mid"]},
        "mid": {"exists": False, "children": ["leaf"]},
        "leaf": {"exists": False, "children": []},
    }
    monkeypatch.setattr(
        manifest, "routing_of",
        lambda p: manifest.RoutingDecl(keywords=("x",)) if p.key == "leaf" else manifest.RoutingDecl(),
    )
    monkeypatch.setattr(
        projects, "resolve_project",
        lambda k: projects.Project(k, k, "", f"/hosts/{k}", f"/workspaces/{k}") if k in graph else None,
    )
    decision = node_routing.route("x", "root", graph)
    assert decision.kind == "unbuilt" and decision.node_id == "root"


# --- degradations ------------------------------------------------------------


def test_no_graph_is_reported_not_silently_read_as_no_match(caplog):
    with caplog.at_level("WARNING", logger="coordinator.node_routing"):
        decision = node_routing.route("annuncio affitto", "root", {})
    assert decision.kind == "root"
    assert "mappa dei nodi" in caplog.text, (
        "'I could not look' must not be logged as 'nothing matched'"
    )


def test_a_node_without_a_workspace_is_skipped_not_crashed(monkeypatch):
    monkeypatch.setattr(projects, "resolve_project", lambda k: None)
    assert node_routing.route("annuncio affitto", "root", GRAPH).kind == "root"


def test_descendants_survives_a_cycle_in_the_graph():
    cyclic = {
        "root": {"children": ["a"]},
        "a": {"children": ["b"]},
        "b": {"children": ["a"]},  # back-edge
    }
    assert node_routing.descendants("root", cyclic) == ["a", "b"]


def test_descendants_ignores_children_that_are_not_in_the_graph():
    assert node_routing.descendants("root", {"root": {"children": ["ghost"]}}) == []
