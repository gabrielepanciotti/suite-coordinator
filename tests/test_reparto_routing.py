"""Two-level routing (Coordinator#77, Life#117): department first, then its agent.

The agent is always a fake: the rules under test are about what the Coordinator
does with its answer, and above all that no answer ever loses a message.
"""
from __future__ import annotations

import asyncio

import pytest

import manifest
import node_routing
import projects
import reparto_routing

REPARTO = {
    "agente": "reparto-infra",
    "persona": "...",
    "nodi": {"bus": "message bus", "bot": "telegram bot", "calls": "call", "plan": "x"},
}

GRAPH = {
    "infra": {"label": "Infra", "exists": True, "children": ["bus", "bot", "calls", "plan"],
              "reparto": REPARTO},
    "bus": {"label": "Bus", "exists": True, "children": []},
    "bot": {"label": "Bot", "exists": True, "children": []},
    "calls": {"label": "Calls", "exists": True, "children": []},
    "plan": {"label": "Plan", "exists": False, "children": []},
    "other": {"label": "Other", "exists": True, "children": []},
}

RULES = {
    "calls": manifest.RoutingDecl(domains=("calls.example",), keywords=("callmanager",)),
}


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    def _resolve(key):
        if key in GRAPH and key not in {"infra", "unregistered"}:
            return projects.Project(
                key=key, label=key, emoji="", host_path=f"/h/{key}", workspace=f"/w/{key}"
            )
        return None

    monkeypatch.setattr(projects, "resolve_project", _resolve)
    monkeypatch.setattr(
        manifest, "routing_of", lambda p: RULES.get(p.key, manifest.RoutingDecl())
    )
    monkeypatch.delenv("REPARTO_ROUTING", raising=False)


def _fake(answer, calls=None):
    async def chooser(agent: str, text: str) -> str:
        if calls is not None:
            calls.append((agent, text))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return chooser


def _route(text, chooser, root="infra", graph=GRAPH):
    return asyncio.run(reparto_routing.route(text, root, graph, chooser))


MSG = "il bus non consegna i messaggi all'agente di CallManager"


def test_agent_picks_the_node_words_would_miss():
    calls = []
    d = _route(MSG, _fake('{"nodo": "bus", "motivo": "consegna del bus"}', calls))
    assert (d.kind, d.node_id, d.reparto, d.motivo) == ("routed", "bus", "infra", "consegna del bus")
    assert calls == [("reparto-infra", MSG)]
    # Without the agent the keyword would have sent it to the wrong node.
    assert node_routing.route(MSG, "infra", GRAPH).node_id == "calls"


def test_a_link_wins_without_asking():
    calls = []
    d = _route("guarda calls.example/x", _fake('{"nodo": "bus"}', calls))
    assert d.node_id == "calls" and d.reparto == ""
    assert calls == []


@pytest.mark.parametrize(
    "answer",
    [
        '{"nodo": null, "motivo": "nessuno"}',
        '{"nodo": "other", "motivo": "fuori reparto"}',
        '{"nodo": "ghost"}',
        "non so",
        "",
        TimeoutError(),
        RuntimeError("claude exit 1"),
    ],
)
def test_any_bad_answer_falls_back_to_deterministic(answer):
    d = _route(MSG, _fake(answer))
    assert d == node_routing.route(MSG, "infra", GRAPH)


def test_json_wrapped_in_prose_is_accepted():
    d = _route(MSG, _fake('Ecco:\n```json\n{"nodo": "bot", "motivo": "m"}\n```'))
    assert d.node_id == "bot"


def test_unbuilt_choice_is_reported_as_unbuilt():
    d = _route(MSG, _fake('{"nodo": "plan", "motivo": "m"}'))
    assert (d.kind, d.planned, d.reparto) == ("unbuilt", "plan", "infra")


def test_node_missing_from_registry_falls_back():
    graph = {**GRAPH, "unregistered": {"exists": True, "children": []}}
    graph["infra"] = {**graph["infra"], "reparto": {**REPARTO, "nodi": {**REPARTO["nodi"], "unregistered": "x"}}}
    d = _route(MSG, _fake('{"nodo": "unregistered"}'), graph=graph)
    assert d == node_routing.route(MSG, "infra", graph)


def test_switch_off_never_asks(monkeypatch):
    monkeypatch.setenv("REPARTO_ROUTING", "off")
    calls = []
    _route(MSG, _fake('{"nodo": "bus"}', calls))
    assert calls == []


def test_department_below_root_found_through_candidates():
    graph = {**GRAPH, "top": {"exists": True, "children": ["infra", "other"]}}
    d = _route(MSG, _fake('{"nodo": "bus", "motivo": "m"}'), root="top", graph=graph)
    assert d.node_id == "bus" and d.reparto == "infra"


def test_no_department_no_question():
    graph = {k: {kk: vv for kk, vv in v.items() if kk != "reparto"} for k, v in GRAPH.items()}
    calls = []
    _route(MSG, _fake('{"nodo": "bus"}', calls), graph=graph)
    assert calls == []


def test_reparto_of_rejects_malformed():
    assert reparto_routing.reparto_of("x", {"reparto": {"agente": "a"}}) is None
    assert reparto_routing.reparto_of("x", {"reparto": {"agente": "", "nodi": {"a": 1}}}) is None
    assert reparto_routing.reparto_of("x", {}) is None
    rep = reparto_routing.reparto_of("infra", GRAPH["infra"])
    assert rep.agente == "reparto-infra" and "bus" in rep.nodi


def test_service_chooser_runs_the_instance_queue(monkeypatch, tmp_path):
    import claude_runner
    import wake_routing

    ws = tmp_path / "ws"
    (ws / ".claude" / "agents").mkdir(parents=True)
    (ws / ".claude" / "agents" / "reparto-infra.md").write_text("---\nname: x\n---\nSCEGLI UN NODO", encoding="utf-8")
    monkeypatch.setattr(
        projects, "resolve_project",
        lambda k: projects.Project(key=k, label=k, emoji="", host_path="/h/c", workspace=str(ws)),
    )
    monkeypatch.setattr(claude_runner, "host_exec_enabled", lambda: True)
    monkeypatch.setattr(wake_routing, "instance_with_service", lambda s: tmp_path)
    seen = {}

    async def fake_run(inst, servizio, *, cwd, prompt, timeout_s):
        seen.update(servizio=servizio, cwd=cwd, prompt=prompt)
        return wake_routing.FallbackResult(True, text='{"nodo": "bus", "motivo": "m"}')

    monkeypatch.setattr(wake_routing, "run_service", fake_run)
    out = asyncio.run(reparto_routing.service_chooser("reparto-infra", "ciao"))
    assert out == '{"nodo": "bus", "motivo": "m"}'
    assert seen["servizio"] == "coordinator.instradaReparto" and seen["cwd"] == "/h/c"
    assert "SCEGLI UN NODO" in seen["prompt"] and "name: x" not in seen["prompt"] and "«ciao»" in seen["prompt"]


def test_service_chooser_none_without_service(monkeypatch):
    import claude_runner
    import wake_routing

    monkeypatch.setattr(claude_runner, "host_exec_enabled", lambda: True)
    monkeypatch.setattr(wake_routing, "instance_with_service", lambda s: None)
    assert asyncio.run(reparto_routing.service_chooser("a", "t")) is None


def test_service_chooser_none_when_nothing_runs_on_this_host(monkeypatch, tmp_path):
    import claude_runner
    import wake_routing

    ws = tmp_path / "ws"
    (ws / ".claude" / "agents").mkdir(parents=True)
    (ws / ".claude" / "agents" / "reparto-infra.md").write_text("X", encoding="utf-8")
    monkeypatch.setattr(
        projects, "resolve_project",
        lambda k: projects.Project(key=k, label=k, emoji="", host_path="/h/c", workspace=str(ws)),
    )
    monkeypatch.setattr(claude_runner, "host_exec_enabled", lambda: True)
    monkeypatch.setattr(wake_routing, "instance_with_service", lambda s: tmp_path)

    async def fake_run(inst, servizio, *, cwd, prompt, timeout_s):
        return wake_routing.FallbackResult(False, attempts=[{"voce": "v", "esito": "skipped", "motivo": "m"}])

    monkeypatch.setattr(wake_routing, "run_service", fake_run)
    assert asyncio.run(reparto_routing.service_chooser("reparto-infra", "ciao")) is None
