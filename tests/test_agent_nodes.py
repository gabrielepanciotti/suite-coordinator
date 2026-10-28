"""Nodes of type ``agent`` (Life#157, contract §3): recognition, dry answer, fallback,
closed tool list, no bus presence.

No test spawns ``claude``: the unit tests inject a fake executor, and the end-to-end
ones run Life's real runtime against a fake ``claude`` (``CLAUDE_BIN``) on a copy of
Life's demo department ``tooling/suite-tooling/examples/reparto-agenti/`` — its own
graph, never the live one.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_nodes
import bus_agents
import claude_runner
import hub
import projects
import reparto_routing
from project_model import Project

# The Life checkout: explicit, or the sibling of this repo (`<srv>/Life`).
LIFE_ROOT = Path(
    os.environ.get("LIFE_FRAMEWORK_ROOT_TEST")
    or Path(__file__).resolve().parents[2] / "Life"
)
LIFE_SHARED = LIFE_ROOT / "packages" / "shared"
EXAMPLE = LIFE_ROOT / "tooling" / "suite-tooling" / "examples" / "reparto-agenti"

needs_life = pytest.mark.skipif(
    not (LIFE_SHARED / "suite_core" / "runtime" / "agent.py").is_file()
    or not EXAMPLE.is_dir(),
    reason="checkout Life con il runtime agent (Life#157) non presente",
)


def _write_node(path: Path, config: dict, instructions: str = "Rispondi breve.") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "node.config").write_text(json.dumps(config), encoding="utf-8")
    (path / "agent.md").write_text(instructions, encoding="utf-8")
    return path


def _agent_config(**agent) -> dict:
    block = {"persona": "p", "competenza": "c", "istruzioni": "agent.md", **agent}
    return {"schemaVersion": "2.0", "name": "Demo", "slug": "demo", "type": "agent", "agent": block}


def _run(coro):
    return asyncio.run(coro)


# --- 1. recognition ---------------------------------------------------------


def test_type_agent_is_recognised_with_manifest_values(tmp_path):
    d = _write_node(tmp_path / "Demo", _agent_config(modello="sonnet", strumenti=["Read", "Grep"]))
    node = agent_nodes.node_of(str(d))
    assert node == agent_nodes.AgentNode(str(d), "demo", ("Read", "Grep"), "sonnet")


def test_defaults_are_read_only_and_haiku(tmp_path):
    d = _write_node(tmp_path / "Demo", _agent_config())
    node = agent_nodes.node_of(str(d))
    assert node.tools == ("Read", "Glob", "Grep")
    assert node.model == "haiku"


def test_block_without_type_is_agent_like_the_runtime(tmp_path):
    cfg = _agent_config()
    cfg.pop("type")
    d = _write_node(tmp_path / "Demo", cfg)
    assert agent_nodes.is_agent_dir(str(d))


@pytest.mark.parametrize(
    "config",
    [
        {"type": "fastapi-react-app", "slug": "x"},
        {"type": "bridge", "agent": {"persona": "p"}},  # another type wins over the block
        ["not", "a", "dict"],
    ],
)
def test_other_nodes_are_not_agents(tmp_path, config):
    tmp_path.joinpath("node.config").write_text(json.dumps(config), encoding="utf-8")
    assert agent_nodes.node_of(str(tmp_path)) is None


def test_missing_or_broken_config_is_not_an_agent(tmp_path):
    assert agent_nodes.node_of(str(tmp_path)) is None
    assert agent_nodes.node_of(None) is None
    tmp_path.joinpath("node.config").write_text("{rotto", encoding="utf-8")
    assert agent_nodes.node_of(str(tmp_path)) is None


# --- 2. dry answer, fake executor ------------------------------------------

NODE = agent_nodes.AgentNode("/n/Demo", "demo", ("Read",), "haiku")


def _executor(code=0, out="", err="", raises=None, seen=None):
    async def run(argv, stdin, cwd, timeout):
        if seen is not None:
            seen.append((argv, stdin, cwd))
        if raises is not None:
            raise raises
        return code, out, err

    return run


def test_answer_runs_the_runtime_cli_with_the_message_on_stdin():
    seen = []
    out = json.dumps({"nodo": "demo", "esito": "ok", "risposta": "fatto", "dettaglio": ""})
    ans = _run(agent_nodes.answer(NODE, "ciao", executor=_executor(out=out, seen=seen)))
    assert ans == agent_nodes.AgentAnswer(True, risposta="fatto")
    argv, stdin, cwd = seen[0]
    assert argv[1:] == ["-m", "suite_core.nodes", "agent", "run", "/n/Demo", "--json"]
    assert stdin == "ciao" and cwd == "/n/Demo"


@pytest.mark.parametrize(
    "kwargs, detail",
    [
        ({"code": 1, "out": json.dumps({"esito": "errore", "dettaglio": "claude exit 1"})}, "claude exit 1"),
        ({"code": 0, "out": json.dumps({"esito": "errore", "dettaglio": "risposta vuota"})}, "risposta vuota"),
        ({"code": 2, "out": "", "err": "manifest non valido: persona"}, "manifest non valido: persona"),
        ({"code": 0, "out": "non json"}, "non json"),
        ({"raises": asyncio.TimeoutError()}, "nessuna risposta entro"),
        ({"raises": FileNotFoundError("python")}, "FileNotFoundError"),
    ],
)
def test_every_failure_is_a_not_ok_answer_with_a_reason(kwargs, detail):
    ans = _run(agent_nodes.answer(NODE, "ciao", executor=_executor(**kwargs)))
    assert not ans.ok
    assert detail in ans.dettaglio
    assert detail in agent_nodes.failure_text(NODE, ans)


# --- 4. never bypassPermissions --------------------------------------------


def test_claude_args_with_agent_tools_are_closed():
    args, _ = claude_runner.claude_args(
        "x", persona=None, resume_id=None, model="haiku", effort=None,
        terminal_parity=False, tools="Read,Glob",
    )
    assert "bypassPermissions" not in args
    assert args[args.index("--permission-mode") + 1] == "default"
    assert args[args.index("--tools") + 1] == "Read,Glob"
    assert "--strict-mcp-config" in args and "--mcp-config" not in args


def test_readonly_stays_stricter_than_the_manifest():
    args, _ = claude_runner.claude_args(
        "x", persona=None, resume_id=None, model=None, effort=None,
        terminal_parity=False, readonly=True, tools="Read,Write,Bash",
    )
    assert args[args.index("--tools") + 1] == claude_runner.READONLY_TOOLS


def test_session_tools_force_claude_in_the_container(monkeypatch):
    calls = []

    async def fake_cli(prompt, **kw):
        calls.append(("claude", kw))
        return claude_runner.RunResult("ok", None)

    async def fake_oc(prompt, **kw):
        calls.append(("opencode", kw))
        return claude_runner.RunResult("ok", None)

    monkeypatch.setattr(claude_runner, "_run_claude_cli", fake_cli)
    monkeypatch.setattr(claude_runner, "_run_opencode", fake_oc)
    monkeypatch.setattr(claude_runner, "host_exec_enabled", lambda: True)
    token = claude_runner.SESSION_TOOLS.set("Read")
    try:
        _run(claude_runner.run_claude("x", workspace="/w", persona=None, backend="opencode", host_cwd="/h"))
    finally:
        claude_runner.SESSION_TOOLS.reset(token)
    assert calls[0][0] == "claude"
    assert "host_cwd" not in calls[0][1]


# --- 2/3. Telegram first contact -------------------------------------------


class _Msg:
    def __init__(self, text):
        self.text, self.caption, self.message_thread_id = text, None, 7
        self.is_topic_message = True
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _tg_setup(monkeypatch, workspace, answer):
    import bot

    project = Project(key="riassuntore", label="R", emoji="", host_path="", workspace=workspace)
    bound, handled = [], []
    monkeypatch.setattr(bot, "resolve_project", lambda k: project)
    monkeypatch.setattr(bot, "_profile", lambda u: "owner")
    monkeypatch.setattr(bot, "_bind_topic", lambda *a: bound.append(a))
    monkeypatch.setattr(bot, "_topic_id", lambda m: 7)

    async def fake_handle(update, context):
        handled.append(update)

    monkeypatch.setattr(bot, "handle_message", fake_handle)
    if answer is not None:
        async def fake_answer(node, text):
            return answer

        monkeypatch.setattr(agent_nodes, "answer", fake_answer)
    msg = _Msg("riassumi questo")
    update = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=-100))
    return bot, update, msg, bound, handled


def _materialized(tmp_path) -> Path:
    d = _write_node(tmp_path / "Riassuntore", {**_agent_config(), "slug": "riassuntore"})
    (d / ".claude" / "agents").mkdir(parents=True)
    (d / ".claude" / "agents" / "riassuntore.md").write_text(
        "---\nname: riassuntore\ndescription: c\n---\nSei il riassuntore.", encoding="utf-8"
    )
    return d


def test_first_contact_answers_dry_and_binds_the_slug(monkeypatch, tmp_path):
    d = _materialized(tmp_path)
    bot, update, msg, bound, handled = _tg_setup(
        monkeypatch, str(d), agent_nodes.AgentAnswer(True, risposta="• punto")
    )
    _run(bot._bind_and_run(update, None, "riassuntore", projects.GENERALE.name))
    assert bound == [(-100, 7, "riassuntore", "riassuntore")]
    assert msg.replies == ["• punto"]
    assert handled == []


def test_failed_run_is_said_then_falls_back_to_the_session(monkeypatch, tmp_path):
    d = _materialized(tmp_path)
    bot, update, msg, bound, handled = _tg_setup(
        monkeypatch, str(d), agent_nodes.AgentAnswer(False, dettaglio="claude exit 1")
    )
    _run(bot._bind_and_run(update, None, "riassuntore", projects.GENERALE.name))
    assert bound[0][3] == "riassuntore"
    assert len(msg.replies) == 1 and "claude exit 1" in msg.replies[0]
    assert handled == [update]


def test_ordinary_node_keeps_todays_path(monkeypatch, tmp_path):
    tmp_path.joinpath("node.config").write_text(json.dumps({"type": "bridge"}), encoding="utf-8")
    bot, update, msg, bound, handled = _tg_setup(monkeypatch, str(tmp_path), None)

    async def boom(node, text):
        raise AssertionError("runtime chiamato per un nodo normale")

    monkeypatch.setattr(agent_nodes, "answer", boom)
    _run(bot._bind_and_run(update, None, "riassuntore", "Generale"))
    assert bound == [(-100, 7, "riassuntore", "Generale")]
    assert handled == [update] and msg.replies == []


def test_unknown_sender_gets_no_dry_answer(monkeypatch, tmp_path):
    d = _materialized(tmp_path)
    bot, update, msg, bound, handled = _tg_setup(
        monkeypatch, str(d), agent_nodes.AgentAnswer(True, risposta="segreto")
    )
    monkeypatch.setattr(bot, "_profile", lambda u: None)
    _run(bot._bind_and_run(update, None, "riassuntore", "Generale"))
    assert msg.replies == [] and handled == [update]


def test_conversation_turn_uses_the_manifest_tools(monkeypatch, tmp_path):
    import bot

    d = _materialized(tmp_path)
    project = Project(key="riassuntore", label="R", emoji="", host_path="/h", workspace=str(d))
    seen = {}

    async def fake_run(prompt, **kw):
        seen.update(kw, tools=claude_runner.SESSION_TOOLS.get())
        return claude_runner.RunResult("ok", "sid")

    monkeypatch.setattr(bot, "run_claude", fake_run)
    monkeypatch.setattr(bot, "CLAUDE_LIVE_SESSIONS", True)
    monkeypatch.setattr(bot, "_live_manager", lambda: (_ for _ in ()).throw(AssertionError("live")))
    monkeypatch.setattr(bot, "_topic_host_cwd", lambda p: "/h")
    monkeypatch.setattr(bot, "_set_session", lambda *a: None)
    monkeypatch.setattr(bot, "_log_turn", lambda **k: None)
    monkeypatch.setattr(bot.indicazioni, "pubblica", lambda *a, **k: None)
    monkeypatch.setattr(bot.llm_usage, "record", lambda *a, **k: None)
    agent = projects.resolve_agent(project, "riassuntore")
    _run(bot._claude_turn_locked(-1, 7, project, agent, "ciao", None, None))
    assert seen["tools"] == "Read,Glob,Grep"
    assert seen["backend"] == "claude" and seen["host_cwd"] is None
    assert seen["model"] == "haiku"
    assert "Sei il riassuntore." in (seen["persona"] or "")
    assert claude_runner.SESSION_TOOLS.get() is None  # reset after the turn


# --- 2. bus wake ------------------------------------------------------------


class _Client:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, conv_id, body, needs_human=False, context=None):
        if self.fail:
            raise RuntimeError("bus giù")
        self.sent.append((conv_id, body, context))
        return {}


def test_bus_wake_posts_the_runtime_answer(monkeypatch):
    client = _Client()
    monkeypatch.setattr(hub, "_client", lambda t, a: client)

    async def fake_answer(node, text):
        assert text == "riassumi"
        return agent_nodes.AgentAnswer(True, risposta="• uno")

    monkeypatch.setattr(agent_nodes, "answer", fake_answer)
    assert _run(hub._agent_node_reply("t", "coordinator-x", 5, NODE, {"body": "riassumi"}))
    assert client.sent == [(5, "• uno", {"agent_node": "demo"})]


def test_bus_wake_failure_is_said_and_falls_back(monkeypatch):
    notices = []

    async def fake_notice(t, a, conv, text):
        notices.append(text)

    async def fake_answer(node, text):
        return agent_nodes.AgentAnswer(False, dettaglio="exit 2")

    monkeypatch.setattr(hub, "_post_wake_notice", fake_notice)
    monkeypatch.setattr(agent_nodes, "answer", fake_answer)
    assert not _run(hub._agent_node_reply("t", "a", 5, NODE, {"body": "x"}))
    assert len(notices) == 1 and "exit 2" in notices[0]


def test_bus_post_error_falls_back(monkeypatch):
    monkeypatch.setattr(hub, "_client", lambda t, a: _Client(fail=True))

    async def fake_answer(node, text):
        return agent_nodes.AgentAnswer(True, risposta="r")

    monkeypatch.setattr(agent_nodes, "answer", fake_answer)
    assert not _run(hub._agent_node_reply("t", "a", 5, NODE, {"body": "x"}))


# --- 5. no bus presence -----------------------------------------------------


def test_agent_nodes_get_no_bus_identity(monkeypatch, tmp_path):
    agent_dir = _write_node(tmp_path / "Riassuntore", _agent_config())
    app_dir = tmp_path / "App"
    app_dir.mkdir()
    found = [
        SimpleNamespace(tenant="t", instance_id="i", graph_id="riassuntore", name="riassuntore-x",
                        path=str(agent_dir), former=()),
        SimpleNamespace(tenant="t", instance_id="i", graph_id="app", name="app-x",
                        path=str(app_dir), former=()),
    ]
    monkeypatch.setattr(bus_agents, "_import_local_agents", lambda root: lambda **kw: (found, []))
    agents, _ = bus_agents.derive({"LIFE_DEVICE": "box"})
    assert [a.graph_id for a in agents] == ["app"]


# --- end to end: Life's demo department, real runtime, fake claude ----------


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A copy of the demo department, materialised, with a fake claude on CLAUDE_BIN."""
    root = tmp_path / "reparto-agenti"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns(".claude", ".ai"))
    env = {**os.environ, "PYTHONPATH": str(LIFE_SHARED)}
    for name in ("Riassuntore", "Traduttore"):
        subprocess.run(
            [sys.executable, "-m", "suite_core.nodes", "agent", "materialize", str(root / name)],
            check=True, env=env, capture_output=True,
        )
    fake = tmp_path / "fake-claude"
    args_file = tmp_path / "claude-args"
    fake.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CLAUDE_FAKE_ARGS"\n'
        'if [ -n "$CLAUDE_FAKE_FAIL" ]; then echo boom >&2; exit 3; fi\n'
        'printf "FINTO: %s" "$(cat)"\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PYTHONPATH", str(LIFE_SHARED))
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    monkeypatch.setenv("CLAUDE_FAKE_ARGS", str(args_file))
    graph = json.loads((root / "life.graph.json").read_text(encoding="utf-8"))["nodes"]
    # What the bot's _node_graph adds: a node with a folder on this device exists.
    for node in graph.values():
        node["exists"] = bool(node.get("paths")) or node.get("kind") == "cluster"
    return SimpleNamespace(root=root, graph=graph, args_file=args_file)


@needs_life
def test_demo_department_end_to_end(demo, monkeypatch):
    monkeypatch.setattr(reparto_routing, "enabled", lambda: True)
    # The registry resolves a graph id to its folder (the graph's `path` for the device).
    registry = {
        nid: Project(key=nid, label=nid, emoji="", host_path="",
                     workspace=str(demo.root / n["paths"]["example"]))
        for nid, n in demo.graph.items()
        if n.get("paths")
    }
    monkeypatch.setattr(projects, "resolve_project", lambda k: registry.get(k))

    async def chooser(agent, text):
        assert agent == "reparto-demoAgenti"
        return json.dumps({"nodo": "riassuntore", "motivo": "è un riassunto"})

    decision = _run(reparto_routing.route("fammi il riassunto", "demoAgenti", demo.graph, chooser=chooser))
    assert decision.node_id == "riassuntore"

    project = registry[decision.node_id]
    node = agent_nodes.node_of(project.workspace)
    assert node is not None and node.slug == "riassuntore"
    # The thread's agent (§3.3) is the file suite-apply materialised.
    assert projects.resolve_agent(project, node.slug) is not None
    assert agent_nodes.node_of(str(demo.root)) is None  # the department folder is not an agent

    ans = _run(agent_nodes.answer(node, "testo lungo da riassumere"))
    assert ans == agent_nodes.AgentAnswer(True, risposta="FINTO: testo lungo da riassumere")
    argv = demo.args_file.read_text(encoding="utf-8").split("\n")
    assert "bypassPermissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob,Grep"
    assert argv[argv.index("--model") + 1] == "haiku"


@needs_life
def test_demo_runtime_failure_is_reported(demo, monkeypatch):
    monkeypatch.setenv("CLAUDE_FAKE_FAIL", "1")
    node = agent_nodes.node_of(str(demo.root / "Traduttore"))
    ans = _run(agent_nodes.answer(node, "hello"))
    assert not ans.ok and "claude exit 3" in ans.dettaglio
