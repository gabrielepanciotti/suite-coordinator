"""Bus wakes follow the instance's LLM queue, with fallback (#91, Life#164).

The scenario that started it: the subscription ran out of credits, `claude -p`
answered "Credit balance is too low", the wake retried the same dead mezzo and
ended in a "did not answer" notice. Now the rest of the queue answers instead,
and a human hears of it only if every entry failed.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import claude_runner
import hub
import wake_routing

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import host_llm_route  # noqa: E402

CREDIT = "Credit balance is too low"

QUEUE = {
    "version": 1,
    "profili": {
        "reclami": {"claudeDir": "~/.claude"},
        "owner": {"claudeDir": "PLACEHOLDER"},
    },
    "profiloPredefinito": "reclami",
    "categorie": {
        "sessione": {
            "timeoutMin": 1,
            "coda": [
                {"mezzo": "claude", "profilo": "owner", "modello": "sonnet"},
                {"mezzo": "opencode", "modello": "deepseek/deepseek-v4-pro"},
            ],
        }
    },
    "servizi": {
        "coordinator.offerte": {
            "coda": [{"mezzo": "opencode", "modello": "opencode/glm-5.3"}],
        }
    },
}


@pytest.fixture()
def instance(tmp_path, monkeypatch):
    """A registered instance `acme` with an llm-routing.json, on a fake LIFE_HOME."""
    inst = tmp_path / "life-acme"
    (inst / "config").mkdir(parents=True)
    gab = tmp_path / ".claude-owner"
    gab.mkdir()
    cfg = json.loads(json.dumps(QUEUE))
    cfg["profili"]["owner"]["claudeDir"] = str(gab)
    (inst / "config" / "llm-routing.json").write_text(json.dumps(cfg))
    home = tmp_path / ".life"
    home.mkdir()
    (home / "instances.json").write_text(json.dumps({
        "version": 1,
        "instances": {"acme": {"path": str(inst), "profile": "work", "band": 1}},
    }))
    monkeypatch.setenv("LIFE_HOME", str(home))
    monkeypatch.setenv("WAKE_FORZA_PATH", str(tmp_path / "wake_forza.json"))
    return inst


# ---- pure helpers -------------------------------------------------------------------

def test_credit_and_limit_texts_are_quota_errors():
    assert wake_routing.is_quota_error(CREDIT)
    assert wake_routing.is_quota_error("You've hit your limit · resets 3pm (Europe/Rome)")
    assert not wake_routing.is_quota_error("Fatto, ho risposto sul bus.")


def test_force_syntax_matches_the_engine():
    e = wake_routing.parse_force("claude:owner/opus")
    assert (e.mezzo, e.profilo, e.modello) == ("claude", "owner", "opus")
    e = wake_routing.parse_force("opencode/deepseek/deepseek-v4-pro")
    assert (e.mezzo, e.profilo, e.modello) == ("opencode", None, "deepseek/deepseek-v4-pro")
    assert e.spec() == "opencode/deepseek/deepseek-v4-pro"
    with pytest.raises(ValueError):
        wake_routing.parse_force("gpt/4")
    assert wake_routing.looks_like_force("claude:owner/opus")
    assert not wake_routing.looks_like_force("opus/high")
    assert not wake_routing.looks_like_force("deepseek")


def test_primary_label_follows_the_backend_that_really_runs(instance, monkeypatch):
    plan = wake_routing.load_plan("life-acme", "Scheduler")
    monkeypatch.setattr(claude_runner, "AI_BACKEND", "claude")
    assert wake_routing.primary_label(plan.primary) == "claude:owner sonnet"

    monkeypatch.setattr(claude_runner, "AI_BACKEND", "opencode")
    monkeypatch.setattr(claude_runner, "OPENCODE_MODEL", "deepseek/deepseek-v4-flash")
    assert wake_routing.primary_label(plan.primary) == "opencode deepseek/deepseek-v4-flash"
    # an entry that already names an opencode model keeps it
    assert wake_routing.primary_label(
        wake_routing.Entry("claude", "deepseek/deepseek-v4-pro", "owner")
    ) == "opencode deepseek/deepseek-v4-pro"
    # an opencode entry is never renamed: the engine runs exactly that
    assert wake_routing.primary_label(
        wake_routing.Entry("opencode", "opencode/glm-5.3")
    ) == "opencode opencode/glm-5.3"


def test_plan_takes_the_sessione_category(instance):
    plan = wake_routing.load_plan("life-acme", "Scheduler")
    assert plan.categoria == "sessione"
    assert plan.primary.label() == "claude:owner sonnet"
    assert [e.mezzo for e in plan.fallback] == ["opencode"]


def test_a_project_service_wins_over_the_category(instance):
    plan = wake_routing.load_plan("life-acme", "Offerte")
    assert plan.servizio == "coordinator.offerte"
    assert plan.primary is None  # first entry is not claude: straight to the engine
    assert len(plan.fallback) == 1


def test_forza_replaces_the_queue(instance):
    wake_routing.set_forza("Scheduler", "claude:reclami/opus")
    plan = wake_routing.load_plan("life-acme", "Scheduler", force=wake_routing.get_forza("scheduler"))
    assert plan.forced and plan.coda == (plan.forced,)
    assert plan.fallback == ()  # forced = exactly that one, no fallback
    wake_routing.set_forza("Scheduler", None)
    assert wake_routing.get_forza("scheduler") is None


def test_no_config_means_the_old_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFE_HOME", str(tmp_path))
    assert wake_routing.load_plan("life-acme", "Scheduler") is None


def test_route_request_never_carries_secrets(instance):
    plan = wake_routing.load_plan("life-acme", "Scheduler")
    req = json.loads(wake_routing.route_request(
        plan, cwd="/x", prompt="p",
        extra_env={"HUB_URL": "u", "ANTHROPIC_API_KEY": "sk-no", "PATH": "/bin"},
    ))
    assert req["env"] == {"HUB_URL": "u"}
    assert req["categoria"] == "sessione" and req["skip"] == 1


# ---- host side --------------------------------------------------------------------------

def test_host_resolves_the_profile_dir_from_its_own_config(instance, tmp_path):
    d = host_llm_route.profile_claude_dir({"instance": str(instance), "name": "owner"})
    assert d == str((tmp_path / ".claude-owner").resolve())
    # ~/.claude stays unset: CLAUDE_CONFIG_DIR would move .claude.json too (#46).
    assert host_llm_route.profile_claude_dir({"instance": str(instance), "name": "reclami"}) is None
    with pytest.raises(ValueError):
        host_llm_route.profile_claude_dir({"instance": str(tmp_path), "name": "owner"})
    with pytest.raises(ValueError):
        host_llm_route.profile_claude_dir({"instance": str(instance), "name": "../x"})


def test_host_validates_the_route_request(instance):
    ok = host_llm_route.validate({"instance": str(instance), "cwd": "/w", "prompt": "p",
                                  "categoria": "sessione", "skip": 1})
    assert ok["skip"] == 1
    for bad in ({"skip": 99}, {"categoria": "a b"}, {"prompt": ""}, {"forza": "x;rm -rf"}):
        with pytest.raises(ValueError):
            host_llm_route.validate({"instance": str(instance), "cwd": "/w", "prompt": "p",
                                     "categoria": "sessione", **bad})


def test_wrapper_passes_the_profile_name_not_a_path(instance):
    tok = claude_runner.WAKE_PROFILE.set({"instance": str(instance), "name": "owner"})
    try:
        req = json.loads(claude_runner.host_exec_request(["-p", "x"], host_cwd="/w", extra_env={}))
    finally:
        claude_runner.WAKE_PROFILE.reset(tok)
    assert req["profile"] == {"instance": str(instance), "name": "owner"}
    assert "profile" not in json.loads(claude_runner.host_exec_request(["-p", "x"], host_cwd="/w", extra_env={}))


def _life_python() -> str | None:
    try:
        return host_llm_route.life_python()
    except ValueError:
        return None


@pytest.mark.skipif(_life_python() is None, reason="Life's python not on this device")
def test_engine_falls_back_when_the_backend_says_credit_too_low(instance, tmp_path):
    """The real engine, a fake backend: claude says 'credit balance too low', the
    next entry answers. skip=0 so the claude entry is tried by the engine itself."""
    driver = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(SCRIPTS)!r})
        from suite_backend_core.llm_routing import backends
        backends.check = lambda v, ctx: (True, "")
        def run(v, ctx, task, text, timeout):
            if v.mezzo == "claude":
                raise backends.BackendError({CREDIT!r})
            return "pong da " + v.label()
        backends.run = run
        import host_llm_route
        raise SystemExit(host_llm_route.engine_main())
    """)
    req = {"instance": str(instance), "cwd": str(tmp_path), "prompt": "rispondi pong",
           "categoria": "sessione", "skip": 0}
    env = host_llm_route.engine_env({**os.environ, "LLM_ROUTING_LOG_DIR": str(tmp_path / "logs")})
    r = subprocess.run([_life_python(), "-c", driver], input=json.dumps(req) + "\n",
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.stdout.strip(), r.stderr[-800:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["ok"] is True
    assert out["text"].startswith("pong da opencode")
    assert [a["esito"] for a in out["attempts"]] == ["failed", "ok"]
    assert CREDIT in out["attempts"][0]["motivo"]


# ---- the wake loop -------------------------------------------------------------------------

class _Bus:
    def __init__(self, posted_by_agent: bool = False):
        self.sent: list[tuple[str, dict | None]] = []
        self.notices: list[str] = []
        self.posted_by_agent = posted_by_agent


def _wire(monkeypatch, tmp_path, bus: _Bus, run_text: str, fallback):
    monkeypatch.setattr(hub.hub_store, "HUB_TOPICS_PATH", str(tmp_path / "hub_topics.json"))
    calls: dict = {"runs": [], "fallback": 0}

    async def run_claude(prompt, **kw):
        calls["runs"].append({**kw, "profile": claude_runner.WAKE_PROFILE.get()})
        return (run_text, None)

    class Client:
        async def send_message(self, conv_id, text, context=None, **kw):
            bus.sent.append((text, context))

    async def verify(*a):
        return bus.posted_by_agent

    async def notice(tenant, agent, conv, text):
        bus.notices.append(text)
        return True

    async def nothing(*a, **k):
        return None

    async def run_fallback(plan, **kw):
        calls["fallback"] += 1
        return fallback

    monkeypatch.setitem(hub._S, "run_claude", run_claude)
    monkeypatch.setitem(hub._S, "config", None)
    monkeypatch.setattr(hub, "_client", lambda *a, **k: Client())
    monkeypatch.setattr(hub, "_verify_outcome", verify)
    monkeypatch.setattr(hub, "_post_auto_notice", notice)
    monkeypatch.setattr(hub, "_set_delivery", nothing)
    monkeypatch.setattr(hub, "_maybe_surface_escalation", nothing)
    monkeypatch.setattr(wake_routing, "run_fallback", run_fallback)
    return calls


def _attempts(plan, since=10):
    return hub._wake_attempts(
        "life-acme", "scheduler-dev-linux", 77, type("P", (), {"key": "scheduler", "workspace": "/w"})(),
        "prompt", None, since, bus_env={"HUB_URL": "u"}, host_cwd="/srv/Scheduler",
        run_cwd="/srv/Scheduler", max_s=None, plan=plan,
    )


def test_credit_exhausted_falls_back_and_answers_without_a_notice(instance, monkeypatch, tmp_path):
    plan = wake_routing.load_plan("life-acme", "Scheduler")
    bus = _Bus()
    ok = wake_routing.FallbackResult(True, "pong", [
        {"voce": "opencode deepseek/deepseek-v4-pro", "esito": "ok"}])
    calls = _wire(monkeypatch, tmp_path, bus, CREDIT, ok)

    asyncio.run(_attempts(plan))

    assert len(calls["runs"]) == 1, "a dead subscription is not retried"
    assert calls["runs"][0]["model"] == "sonnet"
    assert calls["runs"][0]["profile"] == {"instance": str(instance), "name": "owner"}
    assert calls["fallback"] == 1
    assert bus.notices == [], "no credit notice when a fallback answered"
    assert bus.sent == [("pong", {"llm_routing": {"voce": "opencode deepseek/deepseek-v4-pro",
                                                   "ripiego": True}})]


def test_fallback_that_posted_itself_is_not_posted_twice(instance, monkeypatch, tmp_path):
    plan = wake_routing.load_plan("life-acme", "Scheduler")
    bus = _Bus()
    ok = wake_routing.FallbackResult(True, "fatto", [{"voce": "opencode x", "esito": "ok"}])
    calls = _wire(monkeypatch, tmp_path, bus, CREDIT, ok)

    async def verify(*a):  # the primary did not post; the fallback did
        return calls["fallback"] > 0

    monkeypatch.setattr(hub, "_verify_outcome", verify)
    asyncio.run(_attempts(plan))
    assert bus.sent == [] and bus.notices == []


def test_whole_queue_failed_lists_every_attempt(instance, monkeypatch, tmp_path):
    monkeypatch.setattr(claude_runner, "AI_BACKEND", "claude")
    plan = wake_routing.load_plan("life-acme", "Scheduler")
    bus = _Bus()
    ko = wake_routing.FallbackResult(False, "", [
        {"voce": "opencode deepseek/deepseek-v4-pro", "esito": "failed", "motivo": "BackendError: 402"}])
    _wire(monkeypatch, tmp_path, bus, CREDIT, ko)

    asyncio.run(_attempts(plan))

    assert len(bus.notices) == 1
    n = bus.notices[0]
    assert "tutta la coda" in n
    assert "claude:owner sonnet: crediti/limite esauriti" in n
    assert "opencode deepseek/deepseek-v4-pro: failed (BackendError: 402)" in n


def test_failure_notice_names_the_backend_that_actually_ran(instance, monkeypatch, tmp_path):
    plan = wake_routing.load_plan("life-acme", "Scheduler")
    bus = _Bus()
    ko = wake_routing.FallbackResult(False, "", [
        {"voce": "opencode deepseek/deepseek-v4-pro", "esito": "failed", "motivo": "402"}])
    _wire(monkeypatch, tmp_path, bus, CREDIT, ko)
    monkeypatch.setattr(claude_runner, "AI_BACKEND", "opencode")
    monkeypatch.setattr(claude_runner, "OPENCODE_MODEL", "deepseek/deepseek-v4-flash")

    asyncio.run(_attempts(plan))

    n = bus.notices[0]
    assert "opencode deepseek/deepseek-v4-flash: crediti/limite esauriti" in n
    assert "claude:owner sonnet" not in n


def test_primary_answer_never_touches_the_engine(instance, monkeypatch, tmp_path):
    plan = wake_routing.load_plan("life-acme", "Scheduler")
    bus = _Bus(posted_by_agent=True)
    calls = _wire(monkeypatch, tmp_path, bus, "ok, risposto", wake_routing.FallbackResult(False))
    asyncio.run(_attempts(plan))
    assert calls["fallback"] == 0 and bus.notices == []
    assert claude_runner.WAKE_PROFILE.get() is None, "the profile does not leak past the wake"


def test_service_without_claude_goes_straight_to_the_engine(instance, monkeypatch, tmp_path):
    plan = wake_routing.load_plan("life-acme", "Offerte")
    bus = _Bus()
    calls = _wire(monkeypatch, tmp_path, bus, "unused",
                  wake_routing.FallbackResult(True, "ciao", [{"voce": "opencode o", "esito": "ok"}]))
    asyncio.run(_attempts(plan))
    assert calls["runs"] == [] and calls["fallback"] == 1
    assert bus.sent[0][0] == "ciao"


def test_without_a_plan_nothing_changes(monkeypatch, tmp_path):
    bus = _Bus(posted_by_agent=True)
    calls = _wire(monkeypatch, tmp_path, bus, "ok", wake_routing.FallbackResult(False))
    asyncio.run(_attempts(None))
    assert "model" not in calls["runs"][0] and calls["runs"][0]["profile"] is None
    assert calls["fallback"] == 0


# ---- non-interactive jobs as declared services (notifiche, taskgen, butler) ----

def _node_service_env(monkeypatch, tmp_path, *, declared=True, host_path="/h/node"):
    import projects

    monkeypatch.setattr(
        projects, "resolve_project",
        lambda k: projects.Project(key=k, label=k, emoji="", host_path=host_path, workspace="/ws"),
    )
    monkeypatch.setattr(claude_runner, "host_exec_enabled", lambda: True)
    monkeypatch.setattr(wake_routing, "instance_with_service", lambda s: tmp_path if declared else None)


def test_run_node_service_runs_the_queue_in_the_node_folder(monkeypatch, tmp_path):
    _node_service_env(monkeypatch, tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "llm-routing.json").write_text(json.dumps({"servizi": {
        "coordinator.taskgen": {"timeoutMin": 2, "coda": [{"mezzo": "opencode"}, {"mezzo": "claude"}]},
    }}), encoding="utf-8")
    seen = {}

    async def fake_run(inst, servizio, *, cwd, prompt, timeout_s):
        seen.update(servizio=servizio, cwd=cwd, prompt=prompt, timeout_s=timeout_s)
        return wake_routing.FallbackResult(True, text='{"suggestions": []}')

    monkeypatch.setattr(wake_routing, "run_service", fake_run)
    run = asyncio.run(wake_routing.run_node_service("coordinator.taskgen", "chef", "SEED", persona="chef-agent"))
    text, sid = run
    assert text == '{"suggestions": []}' and sid is None
    assert seen["servizio"] == "coordinator.taskgen" and seen["cwd"] == "/h/node"
    assert seen["prompt"].startswith("chef-agent") and seen["prompt"].endswith("SEED")
    # two entries x 2 min each, plus the ssh margin
    assert seen["timeout_s"] == 2 * 2 * 60 + wake_routing.FALLBACK_MARGIN_S


def test_run_node_service_failed_queue_is_an_answer_not_a_retry(monkeypatch, tmp_path):
    _node_service_env(monkeypatch, tmp_path)

    async def fake_run(inst, servizio, *, cwd, prompt, timeout_s):
        return wake_routing.FallbackResult(False, attempts=[{"voce": "opencode-go x", "esito": "errore", "motivo": "402"}])

    monkeypatch.setattr(wake_routing, "run_service", fake_run)
    text, _ = asyncio.run(wake_routing.run_node_service("coordinator.riorganizza", "chef", "SEED"))
    assert text.startswith("❌ coda coordinator.riorganizza esaurita") and "402" in text


@pytest.mark.parametrize("declared,host_path,host_exec", [(False, "/h", True), (True, "", True), (True, "/h", False)])
def test_run_node_service_none_when_the_engine_is_out_of_reach(monkeypatch, tmp_path, declared, host_path, host_exec):
    _node_service_env(monkeypatch, tmp_path, declared=declared, host_path=host_path)
    monkeypatch.setattr(claude_runner, "host_exec_enabled", lambda: host_exec)
    assert asyncio.run(wake_routing.run_node_service("coordinator.taskgen", "chef", "SEED")) is None


def test_agent_provider_goes_through_the_service(monkeypatch):
    import notifications
    import projects
    import providers

    monkeypatch.setattr(
        projects, "resolve_project",
        lambda k: projects.Project(key=k, label=k, emoji="", host_path="/h", workspace="/ws"),
    )
    calls = []

    async def fake_service(servizio, node_key, prompt, *, persona=None):
        calls.append((servizio, node_key, prompt))
        return claude_runner.RunResult("report pronto", None)

    async def no_claude(*a, **k):
        raise AssertionError("run_claude must not run when the service answered")

    monkeypatch.setattr(wake_routing, "run_node_service", fake_service)
    notif = notifications.Notification("chef.report", "T", "agent", "0 8 * * *", ("owner",), True,
                                       {"project": "chef", "prompt": "fai il report"})
    out = asyncio.run(providers._provider_agent(notif, run_claude=no_claude))
    assert out == "report pronto"
    assert calls == [("coordinator.notificaAgente", "chef", "fai il report" + providers.AGENT_DELIVERY_RULE)]


def test_engine_main_hands_a_v2_service_to_the_engine_whole(monkeypatch, capsys):
    """A service with `tipo` has no queue of its own: no skip-slicing, no "coda vuota"."""
    import io
    import types

    seen = {}

    class Compito:
        tipo, coda = "contesto-lungo", []

    class Cfg:
        def model_copy(self, deep):
            return self

        def resolve(self, cat, serv):
            return Compito()

    def route(cfg, **kw):
        seen.update(kw)
        return types.SimpleNamespace(ok=True, text="fatto", attempts=[], duration_s=1.0)

    fake = types.ModuleType("suite_backend_core.llm_routing")
    fake.load_config, fake.parse_force, fake.route = (lambda inst: Cfg()), None, route
    monkeypatch.setitem(sys.modules, "suite_backend_core", types.ModuleType("suite_backend_core"))
    monkeypatch.setitem(sys.modules, "suite_backend_core.llm_routing", fake)
    req = {"instance": "/i", "cwd": "/w", "prompt": "p", "servizio": "coordinator.taskgen", "skip": 0}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(req) + "\n"))
    assert host_llm_route.engine_main() == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True and out["text"] == "fatto"
    assert seen["servizio"] == "coordinator.taskgen"


def test_run_node_service_none_when_every_entry_is_skipped_here(monkeypatch, tmp_path):
    """minipc 2026-10-25: no key archive on the host, every entry `skipped` -> legacy path."""
    _node_service_env(monkeypatch, tmp_path)

    async def fake_run(inst, servizio, *, cwd, prompt, timeout_s):
        return wake_routing.FallbackResult(False, attempts=[
            {"voce": "opencode-go x", "esito": "skipped", "motivo": "nessuna chiave"},
            {"voce": "deepseek y", "esito": "skipped", "motivo": "llm-compito.py assente"},
        ])

    monkeypatch.setattr(wake_routing, "run_service", fake_run)
    assert asyncio.run(wake_routing.run_node_service("coordinator.riorganizza", "chef", "SEED")) is None


def test_instance_with_service_prefers_the_device_default_not_the_file_order(tmp_path, monkeypatch):
    """minipc 2026-10-25: comunity came first in instances.json and its accounts ran personal's jobs."""
    home = tmp_path / ".life"
    home.mkdir()
    reg = {}
    for name, band in (("comunity", 3), ("cosmos", 4), ("personal", 2)):
        inst = tmp_path / f"life-{name}"
        (inst / "config").mkdir(parents=True)
        (inst / "config" / "llm-routing.json").write_text(json.dumps({"servizi": {"coordinator.taskgen": {}}}))
        reg[name] = {"path": str(inst), "profile": name, "band": band}
    (home / "instances.json").write_text(json.dumps({"version": 1, "instances": reg}))
    monkeypatch.setenv("LIFE_HOME", str(home))
    for var in ("LIFE_INSTANCE", "LIFE_INSTANCE_DIR"):
        monkeypatch.delenv(var, raising=False)
    assert wake_routing.instance_with_service("coordinator.taskgen") == tmp_path / "life-personal"


def test_an_expired_login_on_the_host_is_out_of_reach_not_a_failed_queue():
    expired = "BackendError: claude exit 1: claude: Failed to authenticate. API Error: 401 OAuth access token has expired."
    res = wake_routing.FallbackResult(False, attempts=[
        {"voce": "opus low", "esito": "failed", "motivo": expired},
        {"voce": "terra", "esito": "skipped", "motivo": "codex o opencode-worker.py assenti"},
    ])
    assert wake_routing.nothing_runs_here(res)
    real = wake_routing.FallbackResult(False, attempts=[
        {"voce": "opus low", "esito": "failed", "motivo": expired},
        {"voce": "sonnet", "esito": "failed", "motivo": "timeout dopo 20 min"},
    ])
    assert not wake_routing.nothing_runs_here(real)


def test_an_engine_that_does_not_start_is_out_of_reach_but_a_timeout_is_not():
    broken = wake_routing.FallbackResult(False, error="wrapper exit 1: ImportError alembic_helper")
    assert wake_routing.nothing_runs_here(broken)
    slow = wake_routing.FallbackResult(False, error="servizio coordinator.taskgen oltre 1080 s")
    assert not wake_routing.nothing_runs_here(slow)


def test_life_python_skips_a_venv_that_exists_but_cannot_import_the_engine(tmp_path, monkeypatch):
    """#95: packages/shared/.venv existed first and had no alembic -> ModuleNotFoundError."""
    base = tmp_path / "Life"
    (base / "packages" / "backend-core" / "suite_backend_core").mkdir(parents=True)
    shared = base / "packages" / "shared" / ".venv" / "bin" / "python"
    good = base / "backend" / ".venv" / "bin" / "python"
    for py in (shared, good):
        py.parent.mkdir(parents=True)
        py.write_text("")
    monkeypatch.delenv("LLM_ROUTING_PYTHON", raising=False)
    monkeypatch.setenv("LIFE_FRAMEWORK_DIR", str(base))
    monkeypatch.setattr(host_llm_route, "_imports_engine", lambda py, b: py == str(good))
    assert host_llm_route.life_python() == str(good)
    monkeypatch.setattr(host_llm_route, "_imports_engine", lambda py, b: False)
    with pytest.raises(ValueError):
        host_llm_route.life_python()
