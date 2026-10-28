"""Turni Telegram sull'host per i progetti in HOST_EXEC_TOPIC_PROJECTS.

Il 2026-09-30 il gruppo ComUnity doveva poter fare quello che fa una sessione
aperta sul PC — correggere, pushare, ridistribuire — ma i turni dei topic
giravano solo nel container lean. La strada sull'host (#46) esisteva gia' per
i wake del bus: questi test guardano che il turno del topic la prenda davvero,
e che un progetto non opt-in resti dov'era.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bot
import claude_runner
import model_prefs
from claude_runner import RunResult


def _stub(monkeypatch, calls):
    async def fake_run_claude(prompt, **kw):
        calls.append(kw)
        return RunResult(("ok", "sid-1"))

    async def live_must_not_run(*a, **kw):  # pragma: no cover - asserted below
        raise AssertionError("host turn went through the warm container process")

    monkeypatch.setattr(bot, "run_claude", fake_run_claude)
    monkeypatch.setattr(bot, "CLAUDE_LIVE_SESSIONS", True)
    monkeypatch.setattr(
        bot, "_live_manager", lambda: SimpleNamespace(run_turn=live_must_not_run)
    )
    monkeypatch.setattr(bot, "_get_session", lambda c, t: None)
    monkeypatch.setattr(bot, "_set_session", lambda c, t, s: None)
    monkeypatch.setattr(bot, "_log_turn", lambda **kw: None)
    monkeypatch.setattr(bot, "_resolve_turn", lambda key, prefs: ("claude", None, {}))
    monkeypatch.setattr(bot.indicazioni, "pubblica", lambda *a, **kw: None)
    monkeypatch.setattr(bot.llm_usage, "record", lambda *a, **kw: None)
    monkeypatch.setattr(claude_runner, "HOST_EXEC_SSH", "gab@host")


def _turn(project):
    agent = SimpleNamespace(name="settore-dev", persona="Sei il capocantiere.")
    return asyncio.run(
        bot._claude_turn(
            -100999, 6, project, agent, "fallo", None, prefs=model_prefs.Prefs()
        )
    )


def test_progetto_opt_in_gira_sull_host(monkeypatch):
    calls: list[dict] = []
    _stub(monkeypatch, calls)
    monkeypatch.setattr(bot, "HOST_EXEC_TOPIC_PROJECTS", {"comunity"})
    project = SimpleNamespace(
        key="comunity", workspace="/workspaces/comunity",
        host_path="./workspaces/community",
    )
    _turn(project)
    assert calls[0]["host_cwd"] == "./workspaces/community"
    # The session is told where it runs, not left to guess from the container.
    assert "./workspaces/community" in calls[0]["persona"]


def test_senza_scelta_esplicita_l_host_forza_claude(monkeypatch):
    """Sul minipc il default di deployment puo' essere opencode: senza questo
    il turno tornerebbe nel container su DeepSeek, e l'opt-in non varrebbe nulla."""
    calls: list[dict] = []
    _stub(monkeypatch, calls)
    monkeypatch.setattr(bot, "_resolve_turn", lambda key, prefs: (None, None, None))
    monkeypatch.setattr(bot, "HOST_EXEC_TOPIC_PROJECTS", {"comunity"})
    project = SimpleNamespace(
        key="comunity", workspace="/workspaces/comunity",
        host_path="./workspaces/community",
    )
    _turn(project)
    assert calls[0]["backend"] == "claude"
    assert calls[0]["host_cwd"] == "./workspaces/community"


def test_scelta_esplicita_opencode_resta_nel_container(monkeypatch):
    calls: list[dict] = []
    _stub(monkeypatch, calls)
    monkeypatch.setattr(bot, "CLAUDE_LIVE_SESSIONS", False)
    monkeypatch.setattr(bot, "_resolve_turn", lambda key, prefs: ("opencode", "ds", None))
    monkeypatch.setattr(bot, "HOST_EXEC_TOPIC_PROJECTS", {"comunity"})
    project = SimpleNamespace(
        key="comunity", workspace="/workspaces/comunity",
        host_path="./workspaces/community",
    )
    _turn(project)
    assert calls[0]["backend"] == "opencode"
    assert calls[0]["host_cwd"] is None
    assert "Giri sull'host" not in (calls[0]["persona"] or "")


def test_progetto_non_opt_in_resta_nel_container(monkeypatch):
    calls: list[dict] = []
    _stub(monkeypatch, calls)
    monkeypatch.setattr(bot, "CLAUDE_LIVE_SESSIONS", False)
    monkeypatch.setattr(bot, "HOST_EXEC_TOPIC_PROJECTS", set())
    project = SimpleNamespace(
        key="comunity", workspace="/workspaces/comunity",
        host_path="./workspaces/community",
    )
    _turn(project)
    assert calls[0]["host_cwd"] is None


def test_senza_host_path_resta_nel_container(monkeypatch):
    calls: list[dict] = []
    _stub(monkeypatch, calls)
    monkeypatch.setattr(bot, "CLAUDE_LIVE_SESSIONS", False)
    monkeypatch.setattr(bot, "HOST_EXEC_TOPIC_PROJECTS", {"comunity"})
    project = SimpleNamespace(key="comunity", workspace="/workspaces/comunity", host_path="")
    _turn(project)
    assert calls[0]["host_cwd"] is None
