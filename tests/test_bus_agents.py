"""Local bus identities computed from Life, not hand-written (#81)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

import bus_agents
import projects
from bus_agents import DerivedAgent
from project_model import Project

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import riconcilia_identita_bus as ric  # noqa: E402

A_OFFERTE = DerivedAgent(
    "life-acme", "acme", "hubOfferte", "hubOfferte-dev-linux", "/nodes/Offerte"
)
A_CB = DerivedAgent(
    "life-acme",
    "acme",
    "callBriefing",
    "callBriefing-dev-linux",
    "/nodes/CallBriefing",
    ("call-briefing",),
)
A_AUTH = DerivedAgent(
    "life-acme", "acme", "acmeAuth", "acmeAuth-dev-linux", "/nodes/acme-auth"
)
A_PENSA = DerivedAgent(
    "life-acme",
    "acme",
    "pensaDigitaleBot",
    "pensaDigitaleBot-dev-linux",
    "/nodes/PensaDigitaleBot",
)
A_MKT = DerivedAgent(
    "life-personal", "personal", "marketing", "marketing-dev-linux", "/nodes/Marketing"
)


@pytest.fixture
def computed(monkeypatch):
    def _set(agents, problems=()):
        bus_agents.reset_cache()
        bus_agents._CACHE["result"] = (list(agents), list(problems))

    yield _set
    bus_agents.reset_cache()


@pytest.fixture
def unavailable():
    bus_agents.reset_cache()
    bus_agents._CACHE["result"] = None
    bus_agents._CACHE["error"] = "Life non montato"
    yield
    bus_agents.reset_cache()


def test_pair_is_the_hub_local_agents_spelling():
    assert A_OFFERTE.pair == "life-acme/hubOfferte-dev-linux:hubOfferte"


def test_unset_or_auto_uses_the_graph(computed):
    computed([A_OFFERTE, A_PENSA])
    for raw in (None, "auto", " AUTO "):
        spec, source = bus_agents.local_agents_spec(raw)
        assert source == "grafo"
        assert spec == (
            "life-acme/hubOfferte-dev-linux:hubOfferte,"
            "life-acme/pensaDigitaleBot-dev-linux:pensaDigitaleBot"
        )


def test_empty_means_no_agents_even_when_the_graph_has_some(computed):
    # The PC: the worker owns these identities, a second listener answers twice.
    computed([A_OFFERTE])
    assert bus_agents.local_agents_spec("") == ("", "vuoto")
    assert bus_agents.local_agents_spec("   ") == ("", "vuoto")


def test_override_is_kept_and_its_divergence_is_logged(computed, caplog):
    computed([A_OFFERTE, A_PENSA])
    raw = "life-acme/hubOfferte-dev-linux:offerte,life-acme/call-briefing-dev-linux:x"
    with caplog.at_level(logging.WARNING, logger="coordinator.bus_agents"):
        spec, source = bus_agents.local_agents_spec(raw)
    assert (spec, source) == (raw, "override")
    msg = caplog.text
    assert "manca life-acme/pensaDigitaleBot-dev-linux" in msg
    assert "hubOfferte-dev-linux: progetto 'offerte', dal grafo 'hubOfferte'" in msg
    assert "in piu' life-acme/call-briefing-dev-linux" in msg


def test_override_equal_to_the_graph_is_silent(computed, caplog):
    computed([A_OFFERTE])
    with caplog.at_level(logging.WARNING, logger="coordinator.bus_agents"):
        bus_agents.local_agents_spec(A_OFFERTE.pair)
    assert caplog.text == ""


def test_unavailable_is_an_error_not_a_quiet_empty_list(unavailable, caplog):
    with caplog.at_level(logging.ERROR, logger="coordinator.bus_agents"):
        assert bus_agents.local_agents_spec(None) == ("", "non disponibile")
    assert "Life non montato" in caplog.text


def test_derive_without_life_checkout_is_unavailable(tmp_path):
    with pytest.raises(bus_agents.DerivationUnavailable):
        bus_agents.derive({"LIFE_FRAMEWORK_ROOT": str(tmp_path)})


def test_hub_config_reads_the_computed_agents(computed, monkeypatch):
    import hub_config

    computed([A_OFFERTE, A_MKT])
    monkeypatch.delenv("HUB_LOCAL_AGENTS", raising=False)
    cfg = hub_config.load_config()
    assert cfg.local_agents == {
        "hubOfferte-dev-linux": "hubOfferte",
        "marketing-dev-linux": "marketing",
    }
    assert cfg.agent_tenants["marketing-dev-linux"] == "life-personal"
    assert cfg.local_agents_source == "grafo"


def test_kebab_and_former_names():
    assert bus_agents.kebab("ownerGraphMcp") == "owner-graph-mcp"
    assert bus_agents.former_names(A_CB, "dev-linux") == ["call-briefing-dev-linux"]
    assert bus_agents.former_names(A_AUTH, "dev-linux") == ["acme-auth-dev-linux"]
    assert bus_agents.former_names(A_OFFERTE, "dev-linux") == ["hub-offerte-dev-linux"]
    one_word = DerivedAgent("t", "i", "vicky", "vicky-dev-linux", "/x")
    assert bus_agents.former_names(one_word, "dev-linux") == []


# --- registry: key = graph id, old keys stay aliases ---------------------------


def _p(key, host_path, **kw):
    return Project(key, kw.pop("label", key), "x", host_path, f"/workspaces/{key}", **kw)


def test_project_on_the_same_checkout_takes_the_graph_id_and_keeps_the_old_key():
    reg = (_p("call-briefing", "/nodes/CallBriefing", aliases=("cb",)),)
    out = projects.apply_derived_agents(reg, derived=[A_CB])
    assert out[0].key == "callBriefing"
    assert out[0].aliases == ("call-briefing", "cb")
    assert out[0].label == "call-briefing"


def test_static_slug_becomes_alias_too():
    reg = (_p("offerte", "/nodes/Offerte", slug="hubOfferte"),)
    out = projects.apply_derived_agents(reg, derived=[A_OFFERTE])
    assert (out[0].key, out[0].slug, out[0].aliases) == ("hubOfferte", "", ("offerte",))


def test_unknown_node_is_added_from_its_path_with_host_exec(monkeypatch):
    monkeypatch.setenv("HOST_EXEC_SSH", "dev@host")
    out = projects.apply_derived_agents((), derived=[A_PENSA, A_MKT])
    assert [(p.key, p.host_path, p.kind) for p in out] == [
        ("pensaDigitaleBot", "/nodes/PensaDigitaleBot", "work"),
        ("marketing", "/nodes/Marketing", "personal"),
    ]


def test_unknown_unmounted_node_stays_out_without_host_exec(monkeypatch):
    monkeypatch.delenv("HOST_EXEC_SSH", raising=False)
    assert projects.apply_derived_agents((), derived=[A_PENSA]) == ()


def test_resolve_project_finds_old_keys_after_the_merge(monkeypatch):
    reg = projects.apply_derived_agents(
        (_p("call-briefing", "/nodes/CallBriefing"),), derived=[A_CB]
    )
    monkeypatch.setattr(projects, "PROJECTS", reg)
    assert projects.resolve_project("callBriefing").key == "callBriefing"
    assert projects.resolve_project("/call-briefing").key == "callBriefing"


def test_no_derivation_leaves_the_registry_untouched(unavailable):
    reg = (_p("offerte", "/x"),)
    assert projects.apply_derived_agents(reg) is reg


# --- bus reconciliation: rename the old name, never a twin ----------------------


def test_plan_renames_former_and_kebab_names_and_creates_the_rest():
    present = {
        "life-acme": {"hubOfferte-dev-linux", "call-briefing-dev-linux", "acme-auth-dev-linux"},
    }
    plan = ric.piano([A_OFFERTE, A_CB, A_AUTH, A_PENSA, A_MKT], present)
    assert [(a, ag.name, old) for a, ag, old in plan] == [
        ("rinomina", "callBriefing-dev-linux", "call-briefing-dev-linux"),
        ("rinomina", "acmeAuth-dev-linux", "acme-auth-dev-linux"),
        ("crea", "pensaDigitaleBot-dev-linux", ""),
        ("crea", "marketing-dev-linux", ""),
    ]


def test_renamed_identity_carries_its_key():
    riga = "call-briefing-dev-linux:K1,hubOfferte-dev-linux:K2"
    out = ric.aggiorna_chiavi(riga, {"call-briefing-dev-linux": "callBriefing-dev-linux"}, {"marketing-dev-linux": "K3"})
    assert out == "callBriefing-dev-linux:K1,hubOfferte-dev-linux:K2,marketing-dev-linux:K3"


# --- per-instance graphs (#78) ------------------------------------------------


def test_instance_graph_is_composed_once_per_instance(monkeypatch):
    seen: list[str] = []

    def _fake(instance_id, env=None):
        seen.append(instance_id)
        return {"root": {"id": "root", "children": [instance_id]}, instance_id: {"id": instance_id}}

    monkeypatch.setattr(bus_agents, "_compose_instance", _fake)
    bus_agents.reset_cache()
    try:
        first = bus_agents.instance_graph("alpha")
        again = bus_agents.instance_graph("alpha")
        assert first is again
        assert seen == ["alpha"]
        assert bus_agents.instance_graph("beta")["root"]["children"] == ["beta"]
        assert seen == ["alpha", "beta"]
    finally:
        bus_agents.reset_cache()


def test_a_broken_instance_graph_is_none_not_another_graph(monkeypatch, caplog):
    def _boom(instance_id, env=None):
        raise bus_agents.DerivationUnavailable("non montata")

    monkeypatch.setattr(bus_agents, "_compose_instance", _boom)
    bus_agents.reset_cache()
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator.bus_agents"):
            assert bus_agents.instance_graph("comunity") is None
        assert "comunity" in caplog.text
    finally:
        bus_agents.reset_cache()


def test_instance_for_node_prefers_the_device_default_when_a_node_is_shared(monkeypatch):
    import instances

    monkeypatch.setattr(
        bus_agents,
        "graphs",
        lambda env=None: {
            "personal": {"academicAssistant": {}, "scheduler": {}},
            "comunity": {"comunity": {}, "scheduler": {}},
        },
    )
    monkeypatch.setattr(instances, "default_instance_id", lambda env=None: "personal")
    assert bus_agents.instance_for_node("academicAssistant") == "personal"
    assert bus_agents.instance_for_node("comunity") == "comunity"
    assert bus_agents.instance_for_node("scheduler") == "personal"
    assert bus_agents.instance_for_node("nope") is None
